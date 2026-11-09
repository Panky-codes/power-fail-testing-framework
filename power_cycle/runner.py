"""Remote SSH (Fabric) and local command execution primitives."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time

from fabric import Config as FabricConfig, Connection
import invoke
from invoke.exceptions import CommandTimedOut, UnexpectedExit
from paramiko.ssh_exception import NoValidConnectionsError, SSHException

from power_cycle.config import ConnectionConfig

logger = logging.getLogger(__name__)


class CommandExecutionError(RuntimeError):
    """Raised when a remote or local command times out or loses transport unexpectedly."""


@dataclass(frozen=True)
class CommandResult:
    command: str
    return_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.return_code == 0


def _safe_close(conn: Connection | None) -> None:
    """Close a Fabric connection, ignoring errors on dead/half-open sockets."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


class WorkloadSession:
    """Manages an asynchronous remote workload that will be interrupted by a power cut."""

    def __init__(
        self,
        conn: Connection,
        command: str,
        name: str = "remote-workload",
        on_finish: threading.Event | None = None,
    ) -> None:
        self._conn = conn
        self._command = command
        self._finished = threading.Event()
        self._on_finish = on_finish
        self._aborted = False
        self.result: CommandResult | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run_worker,
            name=name,
            daemon=True,
        )

    @property
    def is_finished(self) -> bool:
        return self._finished.is_set()

    def start(self) -> None:
        self._thread.start()

    def _run_worker(self) -> None:
        try:
            res = self._conn.run(
                self._command,
                hide=True,
                warn=True,
                in_stream=False,
            )
            if res is not None:
                self.result = CommandResult(
                    command=self._command,
                    return_code=res.exited,
                    stdout=res.stdout,
                    stderr=res.stderr,
                )
        except BaseException as exc:
            if not self._aborted:
                self.error = exc
        finally:
            self._finished.set()
            if self._on_finish is not None:
                self._on_finish.set()

    def wait_before_power_cut(self, duration_sec: float) -> bool:
        """Wait for duration_sec before cutting power.

        Returns True if the workload exited prematurely before the timer elapsed,
        or False if it is still running when the timer expires.
        """
        return self._finished.wait(timeout=duration_sec)

    def mark_power_cutting(self) -> None:
        """Mark session so SSH disconnects caused by power loss are treated as expected."""
        self._aborted = True

    def abort(self) -> None:
        """Force-close the local SSH socket after power cut so the worker thread unblocks."""
        self._aborted = True
        _safe_close(self._conn)
        self._thread.join(timeout=2.0)


class CommandRunner:
    """Executes remote commands via Fabric and local IPMI commands via Invoke."""

    def __init__(
        self,
        connection_cfg: ConnectionConfig,
        default_timeout_sec: int = 120,
    ) -> None:
        self._cfg = connection_cfg
        self._default_timeout_sec = default_timeout_sec
        self._conn: Connection | None = None

    def _new_connection(self) -> Connection:
        timeout = self._cfg.connect_timeout_sec
        connect_kwargs: dict[str, object] = {
            "banner_timeout": timeout,
            "auth_timeout": timeout,
        }
        if self._cfg.ssh_key:
            connect_kwargs["key_filename"] = self._cfg.ssh_key

        kwargs: dict[str, object] = {
            "host": self._cfg.target,
            "connect_timeout": timeout,
            "connect_kwargs": connect_kwargs,
        }
        if self._cfg.user is not None:
            kwargs["user"] = self._cfg.user
        if self._cfg.port is not None:
            kwargs["port"] = self._cfg.port
        if self._cfg.ssh_config:
            kwargs["config"] = FabricConfig(runtime_ssh_path=self._cfg.ssh_config)

        return Connection(**kwargs)

    def describe_target(self) -> str:
        """Return a human-readable summary of the resolved SSH target."""
        conn = self._new_connection()
        resolved = f"{conn.user}@{conn.host}:{conn.port}"
        if self._cfg.ssh_alias:
            return f"{self._cfg.ssh_alias} ({resolved})"
        return resolved

    def _open_with_keepalive(self, conn: Connection) -> None:
        conn.open()
        if conn.transport is not None:
            conn.transport.set_keepalive(5)

    def wait_for_ssh(self, timeout_sec: int, poll_interval_sec: int = 5) -> None:
        """Poll the remote server until SSH connects and executes a probe command."""
        self.close()
        target_desc = self.describe_target()
        deadline = time.monotonic() + timeout_sec
        attempt = 0

        while True:
            attempt += 1
            conn = self._new_connection()
            try:
                self._open_with_keepalive(conn)
                probe = conn.run(
                    "true",
                    hide=True,
                    warn=True,
                    timeout=self._cfg.connect_timeout_sec,
                    in_stream=False,
                )
                if probe is not None and probe.ok:
                    self._conn = conn
                    logger.info(
                        "SSH connection established to %s (attempt %d).",
                        target_desc,
                        attempt,
                    )
                    return
                _safe_close(conn)
            except (
                SSHException,
                NoValidConnectionsError,
                CommandTimedOut,
                UnexpectedExit,
                OSError,
                EOFError,
            ) as exc:
                _safe_close(conn)
                logger.debug(
                    "SSH probe attempt %d to %s failed: %s",
                    attempt,
                    target_desc,
                    exc,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandExecutionError(
                    f"Timed out after {timeout_sec}s waiting for SSH on {target_desc}."
                )
            time.sleep(min(poll_interval_sec, remaining))

    def _ensure_connected(self) -> Connection:
        if self._conn is None or not self._conn.is_connected:
            conn = self._new_connection()
            self._open_with_keepalive(conn)
            self._conn = conn
        return self._conn

    def run_remote(
        self,
        command: str,
        timeout_sec: int | None = None,
    ) -> CommandResult:
        """Execute a command synchronously on the remote database server."""
        effective_timeout = (
            self._default_timeout_sec if timeout_sec is None else timeout_sec
        )
        logger.debug("Running remote command: %s", command)
        try:
            conn = self._ensure_connected()
            res = conn.run(
                command,
                hide=True,
                warn=True,
                timeout=effective_timeout,
                in_stream=False,
            )
            assert res is not None
            return CommandResult(
                command=command,
                return_code=res.exited,
                stdout=res.stdout,
                stderr=res.stderr,
            )
        except (
            CommandTimedOut,
            SSHException,
            NoValidConnectionsError,
            UnexpectedExit,
            OSError,
            EOFError,
        ) as exc:
            self.close()
            raise CommandExecutionError(
                f"Remote command failed or timed out ({command!r}): {exc}"
            ) from exc

    def start_workload(
        self,
        command: str,
        name: str = "remote-workload",
        on_finish: threading.Event | None = None,
    ) -> WorkloadSession:
        """Start a workload asynchronously over a dedicated SSH connection."""
        logger.debug("Starting async remote workload (%s): %s", name, command)
        try:
            workload_conn = self._new_connection()
            self._open_with_keepalive(workload_conn)
        except (
            SSHException,
            NoValidConnectionsError,
            OSError,
            EOFError,
        ) as exc:
            raise CommandExecutionError(
                f"Failed to open SSH session for {name} ({command!r}): {exc}"
            ) from exc

        session = WorkloadSession(
            workload_conn,
            command,
            name=name,
            on_finish=on_finish,
        )
        session.start()
        return session

    def run_local(
        self,
        command: str,
        timeout_sec: int | None = None,
    ) -> CommandResult:
        """Execute a command locally on the orchestrator machine (e.g., ipmitool)."""
        effective_timeout = (
            self._default_timeout_sec if timeout_sec is None else timeout_sec
        )
        logger.debug("Running local command: %s", command)
        try:
            res = invoke.run(
                command,
                hide=True,
                warn=True,
                timeout=effective_timeout,
                in_stream=False,
            )
            assert res is not None
            return CommandResult(
                command=command,
                return_code=res.exited,
                stdout=res.stdout,
                stderr=res.stderr,
            )
        except (CommandTimedOut, UnexpectedExit, OSError) as exc:
            raise CommandExecutionError(
                f"Local command failed or timed out ({command!r}): {exc}"
            ) from exc

    def close(self) -> None:
        """Close the active SSH connection."""
        _safe_close(self._conn)
        self._conn = None

"""Configuration schema and TOML loader for power-cycle testing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any


class ConfigError(ValueError):
    """Raised when the TOML configuration file is missing or invalid."""


@dataclass(frozen=True)
class ConnectionConfig:
    ssh_alias: str = ""
    host: str = ""
    user: str | None = None
    port: int | None = None
    ssh_key: str = ""
    ssh_config: str = ""
    connect_timeout_sec: int = 10

    @property
    def target(self) -> str:
        """Return the SSH config alias or hostname used to open connections."""
        return self.ssh_alias or self.host


@dataclass(frozen=True)
class LoopConfig:
    max_iterations: int = 0
    boot_wait_timeout_sec: int = 300
    ssh_poll_interval_sec: int = 5
    command_timeout_sec: int = 120
    post_start_settle_sec: float = 2.0
    stability_retries: int = 1
    stability_retry_delay_sec: float = 5.0
    workload_run_sec_min: float = 10.0
    workload_run_sec_max: float = 45.0
    power_off_wait_sec: float = 15.0
    log_dir: Path = Path("logs")


@dataclass(frozen=True)
class RemoteCommands:
    check_db_running: str
    start_db: str
    check_db_stable: str
    run_workload: str
    run_parallel_workload: str = ""
    check_db_stable_fail_pattern: str = ""
    collect_failure_logs: str = ""


@dataclass(frozen=True)
class LocalCommands:
    power_cut: str
    power_on: str
    verify_power_off: str = ""


@dataclass(frozen=True)
class CommandsConfig:
    remote: RemoteCommands
    local: LocalCommands


@dataclass(frozen=True)
class AppConfig:
    connection: ConnectionConfig
    loop: LoopConfig
    commands: CommandsConfig


def _require_non_empty_str(section: dict[str, Any], key: str, path: str) -> str:
    val = section.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ConfigError(f"'{path}.{key}' must be a non-empty string.")
    return val.strip()


def load_config(config_path: str | Path) -> AppConfig:
    """Load and validate an AppConfig from a TOML file."""
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML syntax in {path}: {exc}") from exc

    conn_raw = raw.get("connection")
    if not isinstance(conn_raw, dict):
        raise ConfigError("Missing [connection] section in configuration.")

    ssh_alias = str(conn_raw.get("ssh_alias", "")).strip()
    host = str(conn_raw.get("host", "")).strip()
    if not ssh_alias and not host:
        raise ConfigError(
            "Specify either 'connection.ssh_alias' (from ~/.ssh/config) or 'connection.host'."
        )
    if ssh_alias and host:
        raise ConfigError(
            "Specify either 'connection.ssh_alias' or 'connection.host', not both."
        )

    user_raw = conn_raw.get("user")
    user = (
        str(user_raw).strip()
        if user_raw is not None and str(user_raw).strip()
        else None
    )

    port_raw = conn_raw.get("port")
    port: int | None = None
    if port_raw is not None and str(port_raw).strip() != "":
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ConfigError("'connection.port' must be between 1 and 65535.")

    ssh_key_raw = str(conn_raw.get("ssh_key", "")).strip()
    ssh_key = str(Path(ssh_key_raw).expanduser()) if ssh_key_raw else ""

    ssh_config_raw = str(conn_raw.get("ssh_config", "")).strip()
    ssh_config = ""
    if ssh_config_raw:
        ssh_config_path = Path(ssh_config_raw).expanduser()
        if not ssh_config_path.is_file():
            raise ConfigError(f"SSH config file not found: {ssh_config_path}")
        ssh_config = str(ssh_config_path)

    connect_timeout_sec = int(conn_raw.get("connect_timeout_sec", 10))
    if connect_timeout_sec <= 0:
        raise ConfigError("'connection.connect_timeout_sec' must be > 0.")

    connection = ConnectionConfig(
        ssh_alias=ssh_alias,
        host=host,
        user=user,
        port=port,
        ssh_key=ssh_key,
        ssh_config=ssh_config,
        connect_timeout_sec=connect_timeout_sec,
    )

    loop_raw = raw.get("loop", {})
    if not isinstance(loop_raw, dict):
        raise ConfigError("[loop] must be a TOML table.")

    log_dir_raw = str(loop_raw.get("log_dir", "logs")).strip() or "logs"
    loop = LoopConfig(
        max_iterations=int(loop_raw.get("max_iterations", 0)),
        boot_wait_timeout_sec=int(loop_raw.get("boot_wait_timeout_sec", 300)),
        ssh_poll_interval_sec=int(loop_raw.get("ssh_poll_interval_sec", 5)),
        command_timeout_sec=int(loop_raw.get("command_timeout_sec", 120)),
        post_start_settle_sec=float(loop_raw.get("post_start_settle_sec", 2.0)),
        stability_retries=int(loop_raw.get("stability_retries", 1)),
        stability_retry_delay_sec=float(loop_raw.get("stability_retry_delay_sec", 5.0)),
        workload_run_sec_min=float(loop_raw.get("workload_run_sec_min", 10.0)),
        workload_run_sec_max=float(loop_raw.get("workload_run_sec_max", 45.0)),
        power_off_wait_sec=float(loop_raw.get("power_off_wait_sec", 15.0)),
        log_dir=Path(log_dir_raw).expanduser(),
    )

    if loop.max_iterations < 0:
        raise ConfigError("'loop.max_iterations' must be >= 0.")
    if loop.boot_wait_timeout_sec <= 0 or loop.command_timeout_sec <= 0:
        raise ConfigError("Timeout settings in [loop] must be > 0.")
    if loop.stability_retries < 1:
        raise ConfigError("'loop.stability_retries' must be >= 1.")
    if loop.workload_run_sec_min <= 0 or loop.workload_run_sec_max < loop.workload_run_sec_min:
        raise ConfigError(
            "'loop.workload_run_sec_min' must be > 0 and <= 'loop.workload_run_sec_max'."
        )
    if loop.power_off_wait_sec < 0:
        raise ConfigError("'loop.power_off_wait_sec' must be >= 0.")

    cmds_raw = raw.get("commands")
    if not isinstance(cmds_raw, dict):
        raise ConfigError("Missing [commands] section in configuration.")

    remote_raw = cmds_raw.get("remote")
    if not isinstance(remote_raw, dict):
        raise ConfigError("Missing [commands.remote] section in configuration.")

    local_raw = cmds_raw.get("local")
    if not isinstance(local_raw, dict):
        raise ConfigError("Missing [commands.local] section in configuration.")

    fail_pattern = str(remote_raw.get("check_db_stable_fail_pattern", "")).strip()
    if fail_pattern:
        try:
            re.compile(fail_pattern)
        except re.error as exc:
            raise ConfigError(
                f"Invalid regex in 'commands.remote.check_db_stable_fail_pattern': {exc}"
            ) from exc

    remote = RemoteCommands(
        check_db_running=_require_non_empty_str(
            remote_raw, "check_db_running", "commands.remote"
        ),
        start_db=_require_non_empty_str(remote_raw, "start_db", "commands.remote"),
        check_db_stable=_require_non_empty_str(
            remote_raw, "check_db_stable", "commands.remote"
        ),
        run_workload=_require_non_empty_str(
            remote_raw, "run_workload", "commands.remote"
        ),
        run_parallel_workload=str(remote_raw.get("run_parallel_workload", "")).strip(),
        check_db_stable_fail_pattern=fail_pattern,
        collect_failure_logs=str(remote_raw.get("collect_failure_logs", "")).strip(),
    )

    local = LocalCommands(
        power_cut=_require_non_empty_str(local_raw, "power_cut", "commands.local"),
        power_on=_require_non_empty_str(local_raw, "power_on", "commands.local"),
        verify_power_off=str(local_raw.get("verify_power_off", "")).strip(),
    )

    return AppConfig(
        connection=connection,
        loop=loop,
        commands=CommandsConfig(remote=remote, local=local),
    )

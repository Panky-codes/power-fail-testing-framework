"""Main orchestration state machine and CLI entrypoint."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import logging
from pathlib import Path
import random
import re
import sys
import threading
import time

from power_cycle.config import AppConfig, ConfigError, load_config
from power_cycle.runner import (
    CommandExecutionError,
    CommandResult,
    CommandRunner,
    WorkloadSession,
)

logger = logging.getLogger("power_cycle")


class OrchestrationError(RuntimeError):
    """Base error for unrecoverable orchestration failures."""


class RecoveryFailureError(OrchestrationError):
    """Raised when database startup or post-crash stability verification fails."""


def _setup_logging(log_dir: Path, verbose: bool) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"power_cycle_{timestamp}.log"

    root_logger = logging.getLogger("power_cycle")
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    return log_file


def _log_command_output(label: str, result: CommandResult, level: int = logging.DEBUG) -> None:
    logger.log(
        level,
        "%s finished with exit code %d (cmd: %s)",
        label,
        result.return_code,
        result.command,
    )
    if result.stdout.strip():
        logger.log(level, "%s stdout:\n%s", label, result.stdout.rstrip())
    if result.stderr.strip():
        logger.log(level, "%s stderr:\n%s", label, result.stderr.rstrip())


class PowerCycleOrchestrator:
    """Executes the database power-cycle test loop defined by AppConfig."""

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self.runner = CommandRunner(
            connection_cfg=config.connection,
            default_timeout_sec=config.loop.command_timeout_sec,
        )

    def _collect_failure_diagnostics(
        self,
        iteration: int,
        reason: str,
        failed_result: CommandResult | None = None,
    ) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        artifact_path = (
            self.cfg.loop.log_dir / f"failure_iter_{iteration}_{timestamp}.log"
        )
        sections: list[str] = [
            f"Iteration: {iteration}",
            f"Timestamp: {timestamp}",
            f"Failure Reason: {reason}",
        ]

        if failed_result is not None:
            sections.extend(
                [
                    "",
                    f"--- Failed Command ({failed_result.command}) "
                    f"[exit={failed_result.return_code}] ---",
                    "STDOUT:",
                    failed_result.stdout or "<empty>",
                    "STDERR:",
                    failed_result.stderr or "<empty>",
                ]
            )

        diag_cmd = self.cfg.commands.remote.collect_failure_logs
        if diag_cmd:
            logger.info("Collecting remote failure logs via: %s", diag_cmd)
            try:
                diag_res = self.runner.run_remote(diag_cmd)
                sections.extend(
                    [
                        "",
                        f"--- Remote Diagnostics ({diag_cmd}) "
                        f"[exit={diag_res.return_code}] ---",
                        "STDOUT:",
                        diag_res.stdout or "<empty>",
                        "STDERR:",
                        diag_res.stderr or "<empty>",
                    ]
                )
            except CommandExecutionError as exc:
                logger.warning("Could not collect remote failure logs: %s", exc)
                sections.extend(["", f"Remote diagnostics failed: {exc}"])

        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
        logger.error("Saved failure diagnostics to %s", artifact_path)

    def _ensure_db_running(self, iteration: int) -> None:
        cmds = self.cfg.commands.remote
        logger.info("[Iter %d] Checking if database is running...", iteration)
        check_res = self.runner.run_remote(cmds.check_db_running)
        _log_command_output("check_db_running", check_res)

        if check_res.ok:
            logger.info("[Iter %d] Database is running.", iteration)
            return

        logger.info(
            "[Iter %d] Database is not running (exit=%d). Starting instance...",
            iteration,
            check_res.return_code,
        )
        try:
            start_res = self.runner.run_remote(cmds.start_db)
        except CommandExecutionError as exc:
            self._collect_failure_diagnostics(
                iteration, f"start_db execution failed: {exc}"
            )
            raise RecoveryFailureError(
                f"Failed to start database on iteration {iteration}: {exc}"
            ) from exc

        if not start_res.ok:
            _log_command_output("start_db", start_res, level=logging.ERROR)
            self._collect_failure_diagnostics(
                iteration,
                f"Database failed to start (exit={start_res.return_code})",
                start_res,
            )
            raise RecoveryFailureError(
                f"Database failed to start on iteration {iteration} "
                f"(exit={start_res.return_code})."
            )

        _log_command_output("start_db", start_res)
        logger.info("[Iter %d] Database start command succeeded.", iteration)

        if self.cfg.loop.post_start_settle_sec > 0:
            time.sleep(self.cfg.loop.post_start_settle_sec)

    def _verify_db_stable(self, iteration: int) -> None:
        cmd = self.cfg.commands.remote.check_db_stable
        fail_pattern = self.cfg.commands.remote.check_db_stable_fail_pattern
        retries = self.cfg.loop.stability_retries
        delay = self.cfg.loop.stability_retry_delay_sec

        logger.info("[Iter %d] Verifying database stability/recovery...", iteration)
        last_res: CommandResult | None = None

        for attempt in range(1, retries + 1):
            try:
                res = self.runner.run_remote(cmd)
            except CommandExecutionError as exc:
                if attempt < retries:
                    logger.warning(
                        "[Iter %d] Stability check attempt %d/%d errored (%s); retrying in %.1fs...",
                        iteration,
                        attempt,
                        retries,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                self._collect_failure_diagnostics(
                    iteration, f"check_db_stable execution failed: {exc}"
                )
                raise RecoveryFailureError(
                    f"Database stability check timed out/errored on iteration {iteration}: {exc}"
                ) from exc

            last_res = res
            if res.ok:
                if fail_pattern:
                    combined_output = f"{res.stdout}\n{res.stderr}"
                    match = re.search(fail_pattern, combined_output)
                    if match:
                        matched_text = match.group(0).strip()
                        _log_command_output("check_db_stable", res, level=logging.ERROR)
                        self._collect_failure_diagnostics(
                            iteration,
                            f"check_db_stable output matched fail pattern "
                            f"{fail_pattern!r} ({matched_text!r})",
                            res,
                        )
                        raise RecoveryFailureError(
                            f"Database corruption/error detected in stability check output "
                            f"on iteration {iteration} (matched {matched_text!r}). Halting test."
                        )

                _log_command_output("check_db_stable", res)
                logger.info("[Iter %d] Database is stable.", iteration)
                return

            if attempt < retries:
                logger.warning(
                    "[Iter %d] Stability check attempt %d/%d returned exit=%d; retrying in %.1fs...",
                    iteration,
                    attempt,
                    retries,
                    res.return_code,
                    delay,
                )
                time.sleep(delay)

        assert last_res is not None
        _log_command_output("check_db_stable", last_res, level=logging.ERROR)
        self._collect_failure_diagnostics(
            iteration,
            f"Database recovery/stability check failed (exit={last_res.return_code})",
            last_res,
        )
        raise RecoveryFailureError(
            f"Database stability check failed on iteration {iteration} "
            f"(exit={last_res.return_code}). Halting test."
        )

    def _verify_chassis_off(self, iteration: int) -> None:
        verify_cmd = self.cfg.commands.local.verify_power_off
        if not verify_cmd:
            return

        for attempt in range(1, 7):
            res = self.runner.run_local(verify_cmd)
            _log_command_output("verify_power_off", res)
            if res.ok:
                logger.info(
                    "[Iter %d] Confirmed remote server chassis power is OFF.",
                    iteration,
                )
                return
            if attempt < 6:
                time.sleep(3.0)

        raise OrchestrationError(
            f"Chassis power still reported ON after power_cut on iteration {iteration}."
        )

    def _wait_for_workloads(
        self,
        iteration: int,
        sessions: list[tuple[str, WorkloadSession]],
        any_finished: threading.Event,
        run_duration: float,
    ) -> None:
        deadline = time.monotonic() + run_duration
        warned_clean_exit: set[str] = set()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return

            any_finished.clear()

            for label, session in sessions:
                if not session.is_finished:
                    continue
                if session.error is not None:
                    self._collect_failure_diagnostics(
                        iteration,
                        f"{label} session aborted unexpectedly: {session.error}",
                    )
                    raise OrchestrationError(
                        f"{label} errored before power cut on iteration {iteration}: "
                        f"{session.error}"
                    )
                if session.result is not None and not session.result.ok:
                    _log_command_output(label, session.result, level=logging.ERROR)
                    self._collect_failure_diagnostics(
                        iteration,
                        f"{label} exited with error (exit={session.result.return_code}) before power cut",
                        session.result,
                    )
                    raise OrchestrationError(
                        f"{label} failed before power cut on iteration {iteration} "
                        f"(exit={session.result.return_code})."
                    )
                if label not in warned_clean_exit:
                    warned_clean_exit.add(label)
                    logger.warning(
                        "[Iter %d] %s finished cleanly (exit=0) before %.1fs elapsed. "
                        "Increase its runtime duration so power is cut while it is still active.",
                        iteration,
                        label,
                        run_duration,
                    )

            if all(s.is_finished for _, s in sessions):
                return

            if any(
                s.is_finished and (label not in warned_clean_exit)
                for label, s in sessions
            ):
                continue

            remaining = deadline - time.monotonic()
            if remaining > 0:
                any_finished.wait(timeout=remaining)

    def run_iteration(self, iteration: int) -> None:
        logger.info("=== Starting Iteration %d ===", iteration)

        # 1. Wait for remote SSH connectivity.
        logger.info(
            "[Iter %d] Waiting for SSH on %s (timeout=%ds)...",
            iteration,
            self.runner.describe_target(),
            self.cfg.loop.boot_wait_timeout_sec,
        )
        self.runner.wait_for_ssh(
            timeout_sec=self.cfg.loop.boot_wait_timeout_sec,
            poll_interval_sec=self.cfg.loop.ssh_poll_interval_sec,
        )

        # 2. Check if DB is running; start if not.
        self._ensure_db_running(iteration)

        # 3. Verify DB stability / crash recovery.
        self._verify_db_stable(iteration)

        # 4. Run workload(s) on remote server and cut power mid-flight.
        run_duration = random.uniform(
            self.cfg.loop.workload_run_sec_min,
            self.cfg.loop.workload_run_sec_max,
        )
        any_finished = threading.Event()
        sessions: list[tuple[str, WorkloadSession]] = []

        try:
            logger.info(
                "[Iter %d] Starting remote workload; cutting power in %.1fs...",
                iteration,
                run_duration,
            )
            sessions.append(
                (
                    "run_workload",
                    self.runner.start_workload(
                        self.cfg.commands.remote.run_workload,
                        name="remote-workload",
                        on_finish=any_finished,
                    ),
                )
            )

            parallel_cmd = self.cfg.commands.remote.run_parallel_workload
            if parallel_cmd:
                logger.info(
                    "[Iter %d] Starting parallel remote workload...",
                    iteration,
                )
                sessions.append(
                    (
                        "run_parallel_workload",
                        self.runner.start_workload(
                            parallel_cmd,
                            name="remote-parallel-workload",
                            on_finish=any_finished,
                        ),
                    )
                )

            self._wait_for_workloads(iteration, sessions, any_finished, run_duration)

            # 5. Cut power via ipmitool.
            logger.info("[Iter %d] Cutting power to remote server...", iteration)
            for _, session in sessions:
                session.mark_power_cutting()
            cut_res = self.runner.run_local(self.cfg.commands.local.power_cut)
        finally:
            for _, session in sessions:
                session.abort()
            self.runner.close()

        _log_command_output("power_cut", cut_res)
        if not cut_res.ok:
            raise OrchestrationError(
                f"Power cut command failed on iteration {iteration} "
                f"(exit={cut_res.return_code}): {cut_res.stderr.strip()}"
            )

        if self.cfg.loop.power_off_wait_sec > 0:
            logger.info(
                "[Iter %d] Waiting %.1fs with power off...",
                iteration,
                self.cfg.loop.power_off_wait_sec,
            )
            time.sleep(self.cfg.loop.power_off_wait_sec)

        self._verify_chassis_off(iteration)

        # 6. Restart the remote server.
        logger.info("[Iter %d] Powering on remote server...", iteration)
        on_res = self.runner.run_local(self.cfg.commands.local.power_on)
        _log_command_output("power_on", on_res)
        if not on_res.ok:
            raise OrchestrationError(
                f"Power on command failed on iteration {iteration} "
                f"(exit={on_res.return_code}): {on_res.stderr.strip()}"
            )

        # Brief grace period before polling SSH on the next iteration.
        time.sleep(5.0)
        logger.info("=== Completed Iteration %d ===", iteration)

    def run(self) -> None:
        max_iter = self.cfg.loop.max_iterations
        iteration = 0
        try:
            while max_iter == 0 or iteration < max_iter:
                iteration += 1
                self.run_iteration(iteration)
            logger.info("Completed all %d configured iterations successfully.", iteration)
        finally:
            self.runner.close()


def _print_dry_run(cfg: AppConfig) -> None:
    runner = CommandRunner(
        connection_cfg=cfg.connection,
        default_timeout_sec=cfg.loop.command_timeout_sec,
    )
    max_label = (
        "infinite (until failure or Ctrl+C)"
        if cfg.loop.max_iterations == 0
        else str(cfg.loop.max_iterations)
    )
    print("Configuration validated successfully (dry-run mode).\n")
    print(f"Target SSH:      {runner.describe_target()}")
    print(f"Max Iterations:  {max_label}")
    print(
        f"Workload Window: {cfg.loop.workload_run_sec_min:.1f}s - "
        f"{cfg.loop.workload_run_sec_max:.1f}s"
    )
    print(f"Power-Off Wait:  {cfg.loop.power_off_wait_sec:.1f}s")
    print(f"Log Directory:   {cfg.loop.log_dir}\n")
    print("Execution sequence per iteration:")
    print(f"  1. [SSH]   Wait up to {cfg.loop.boot_wait_timeout_sec}s for remote host")
    print(f"  2. [SSH]   Check DB running:  {cfg.commands.remote.check_db_running}")
    print(f"     [SSH]   Start DB (if down): {cfg.commands.remote.start_db}")
    print(f"  3. [SSH]   Verify DB stable:  {cfg.commands.remote.check_db_stable}")
    if cfg.commands.remote.check_db_stable_fail_pattern:
        print(
            f"             Fail output regex: {cfg.commands.remote.check_db_stable_fail_pattern}"
        )
    print(f"  4. [SSH]   Run workload:      {cfg.commands.remote.run_workload}")
    if cfg.commands.remote.run_parallel_workload:
        print(
            f"     [SSH]   Parallel workload: {cfg.commands.remote.run_parallel_workload}"
        )
    print(f"  5. [LOCAL] Cut power:         {cfg.commands.local.power_cut}")
    if cfg.commands.local.verify_power_off:
        print(f"     [LOCAL] Verify power off:  {cfg.commands.local.verify_power_off}")
    print(f"  6. [LOCAL] Power on system:   {cfg.commands.local.power_on}")
    if cfg.commands.remote.collect_failure_logs:
        print(f"  *  [SSH]   On failure logs:   {cfg.commands.remote.collect_failure_logs}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="power-cycle",
        description="Database atomic-support power-cycle test orchestrator.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.toml",
        help="Path to the TOML configuration file (default: config.toml).",
    )
    parser.add_argument(
        "-n",
        "--max-iterations",
        type=int,
        default=None,
        help="Override loop.max_iterations from the config file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and print the execution plan without running commands.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.max_iterations is not None:
            if args.max_iterations < 0:
                raise ConfigError("--max-iterations must be >= 0.")
            cfg = replace(cfg, loop=replace(cfg.loop, max_iterations=args.max_iterations))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        _print_dry_run(cfg)
        return 0

    log_file = _setup_logging(cfg.loop.log_dir, verbose=args.verbose)
    logger.info("Loaded configuration from %s", args.config)
    logger.info("Logging detailed output to %s", log_file)

    orchestrator = PowerCycleOrchestrator(cfg)
    try:
        orchestrator.run()
        return 0
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C). Exiting cleanly.")
        return 130
    except RecoveryFailureError as exc:
        logger.critical("STOPPING TEST — RECOVERY FAILURE: %s", exc)
        return 1
    except (OrchestrationError, CommandExecutionError) as exc:
        logger.error("Orchestration halted due to error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

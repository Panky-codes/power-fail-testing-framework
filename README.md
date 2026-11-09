# power-cycle

Automated bare-metal power-cycle orchestrator for testing crash recovery and atomic write guarantees in databases.

`power-cycle` runs on a separate orchestrator machine, verifies and starts a database on a remote target server over SSH (via [Fabric](https://www.fabfile.org/)), drives a remote workload such as `sysbench`, cuts physical chassis power mid-workload via `ipmitool`, boots the server back up, and repeats until recovery fails or the configured iteration count completes.

For internal design details and state diagrams, see [power-cycle.MD](./power-cycle.MD).

---

## Prerequisites

1. **Orchestrator Machine:**
   - [Nix](https://nixos.org/) with Flakes enabled (`experimental-features = nix-command flakes`).
   - Network access to both the **remote server's SSH port** and its **BMC/IPMI interface**.
2. **Remote Target Server:**
   - Passwordless SSH key authentication configured for the user specified in `config.toml`.
   - Your target database and workload tool (e.g., `sysbench`) installed and initialized.

---

## Quick Start (Nix Flake)

The Nix flake automatically provides Python 3, `fabric`, `invoke`, `paramiko`, `ipmitool`, and `openssh`—no manual `pip` or system package installation is required.

> **Note:** If working inside a Git repository, ensure new files are staged (`git add .`) so Nix flakes include them in the build context.

1. **Edit `config.toml`** with your target SSH host, IPMI credentials, and database/workload commands.

2. **Validate configuration with `--dry-run`** (prints the full execution plan without running any remote or IPMI commands):
   ```bash
   nix run . -- --config config.toml --dry-run
   ```

3. **Start the power-cycle test loop:**
   ```bash
   nix run . -- --config config.toml
   ```

### Development Shell

If you are iterating on the Python code and want to run without rebuilding the Nix package on each edit:

```bash
nix develop
python -m power_cycle.orchestrator --config config.toml --dry-run
```

---

## CLI Usage

```text
usage: power-cycle [-h] [-c CONFIG] [-n MAX_ITERATIONS] [--dry-run] [-v]

options:
  -h, --help            Show help message and exit
  -c, --config CONFIG   Path to the TOML configuration file (default: config.toml)
  -n, --max-iterations MAX_ITERATIONS
                        Override loop.max_iterations from the config file (0 = infinite)
  --dry-run             Validate configuration and print the execution plan without running commands
  -v, --verbose         Enable debug logging to stdout
```

### Exit Codes

| Exit Code | Meaning |
| :--- | :--- |
| `0` | All configured iterations completed with healthy recovery (or `--dry-run` succeeded) |
| `1` | Database recovery/stability check failed, or an SSH/IPMI execution error halted the test |
| `2` | Invalid or missing `config.toml` |
| `130` | Interrupted by user (`Ctrl+C`) |

---

## Configuration (`config.toml`)

All database-specific and hardware-specific commands live in `config.toml` so the Python orchestrator remains agnostic to the database engine.

### Key Sections

- **`[connection]`** — Supports two ways to specify the SSH target:
  - **SSH Config Alias (`ssh_alias`):** Set `ssh_alias = "your-host-alias"` to automatically pull `HostName`, `User`, `Port`, `IdentityFile`, `ProxyJump`, etc. from `~/.ssh/config` (or `/etc/ssh/ssh_config`, or a custom file via `ssh_config = "..."`).
  - **Explicit Host (`host`):** Set `host = "192.168.1.50"` along with optional `user`, `port`, and `ssh_key`.
  - Also accepts `connect_timeout_sec` (default `10`).
- **`[loop]`** — Controls iteration limits (`max_iterations = 0` runs until failure), boot SSH timeout (`boot_wait_timeout_sec`), randomized pre-cut workload duration (`workload_run_sec_min` / `workload_run_sec_max`), power-off dwell time (`power_off_wait_sec`), and `log_dir`.
- **`[commands.remote]`** — Commands executed on the target server via SSH (`0` exit code = pass):
  - `check_db_running`: Checks whether the DB daemon is currently active.
  - `start_db`: Starts the DB instance if `check_db_running` returned non-zero.
  - `check_db_stable`: Verifies crash recovery and table/data integrity. Non-zero halts the test.
  - `check_db_stable_fail_pattern`: *(Optional)* Regex checked against `check_db_stable` output; halts the test if matched even when exit code is `0` (useful for tools like `mysqlcheck` that exit `0` on corrupt tables).
  - `run_workload`: Starts the workload on the remote server (set workload `--time` higher than `workload_run_sec_max` so power is cut while writes are in flight).
  - `run_parallel_workload`: *(Optional)* Runs an additional command on the remote server over a separate SSH session in parallel with `run_workload` (for example, `stress-ng --cpu 0 --timeout 600s` to stress the CPU and increase PSU load).
  - `collect_failure_logs`: *(Optional)* Command run to capture DB logs when startup or stability fails.
- **`[commands.local]`** — Commands executed locally on the orchestrator machine:
  - `power_cut`: Cuts chassis power (e.g., `ipmitool ... chassis power off`).
  - `verify_power_off`: *(Optional)* Returns exit code `0` once chassis power is confirmed off.
  - `power_on`: Boots the server back up (e.g., `ipmitool ... chassis power on`).

### Example: PostgreSQL `[commands.remote]`

```toml
[commands.remote]
check_db_running = "systemctl is-active --quiet postgresql"
start_db = "systemctl start postgresql"
check_db_stable = "pg_isready -q && psql -U postgres -d sbtest -c 'SELECT count(*) FROM sbtest1;'"
run_workload = "sysbench oltp_read_write --db-driver=pgsql --pgsql-user=postgres --pgsql-db=sbtest --threads=16 --time=300 run"
collect_failure_logs = "journalctl -u postgresql -n 200 --no-pager"
```

---

## Logs & Crash Artifacts

By default, logs are written to `./logs/` (configurable via `loop.log_dir`):

- **`logs/power_cycle_<YYYYMMDD_HHMMSS>.log`** — Complete debug log of every iteration, including timestamps, exit codes, and command outputs.
- **`logs/failure_iter_<N>_<YYYYMMDD_HHMMSS>.log`** — Generated automatically when a database startup or stability check fails, capturing the failing command's `stdout`/`stderr` alongside the output of `collect_failure_logs`.

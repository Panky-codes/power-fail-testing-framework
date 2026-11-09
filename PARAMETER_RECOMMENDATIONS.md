# Parameter Recommendations for Database Torn-Write Power-Cycle Testing

This document summarizes the MySQL/InnoDB, `sysbench`, filesystem, and system-level settings used to maximize the probability of triggering—and reliably detecting—torn `16 KiB` writes during abrupt power-loss testing.

---

## 1. Why Default Settings Rarely Trigger Torn Writes

1. **InnoDB Caches Dirty Pages in RAM:** By default, commits only append small physiological records to the sequential Redo Log while keeping modified `16 KiB` `.ibd` pages in the Buffer Pool (`innodb_max_dirty_pages_pct = 90`). If no `.ibd` flush is in flight when power is cut, recovery simply replays Redo Logs onto intact older pages.
2. **Read-Heavy & Skewed Default Workloads:** `sysbench oltp_read_write` is ~78% `SELECT` queries, and its default `special` distribution repeatedly hits the same ~5% hot rows in RAM rather than forcing disk evictions.
3. **Contiguous Extent Alignment:** A freshly prepared `.ibd` file on an unfragmented filesystem maps every `16 KiB` InnoDB page to four contiguous, `16 KiB`-aligned `4 KiB` blocks, allowing Linux to submit a single `16 KiB` NVMe Write command (`~2.5 µs` over PCIe 4.0).
4. **ATX PSU Capacitor Hold-Up (~15–30 ms):** When `ipmitool chassis power off` fires, PCIe `PERST#` stops new host transfers immediately while PSU bulk capacitors keep the M.2 `3.3V` rail alive long enough for an NVMe SSD to flush complete commands already sitting in its DRAM cache.

---

## 2. InnoDB (`mysqld`) Parameters

Used in `commands.remote.start_db` (see [config_dwb_on_nonatomic.toml](./config_dwb_on_nonatomic.toml) and [config_dwb_off_nonatomic.toml](./config_dwb_off_nonatomic.toml)):

| Parameter | Recommended Value | Default | Purpose in Power-Fail Testing |
| :--- | :--- | :--- | :--- |
| `--innodb_flush_method` | `O_DIRECT` | `fsync` | Bypasses the Linux page cache so `16 KiB` `.ibd` writes go directly to the block/NVMe layer. |
| `--innodb_doublewrite` | `OFF` / `ON` | `ON` | `OFF` for non-atomic failure reproduction and for hardware/OS atomic-write validation; `ON` for control runs verifying DWB recovery. |
| `--innodb_buffer_pool_size` | `256M` *(or `< dataset`)* | `128M` | Keeps RAM smaller than the active dataset so reads continuously force LRU dirty-page eviction writes to `.ibd` files. |
| `--innodb_max_dirty_pages_pct_lwm` | `0` | `10` | Disables gradual pre-flushing and lowers the floor so `innodb_max_dirty_pages_pct=0` is accepted without clamping *(place before `pct=0` on CLI)*. |
| `--innodb_max_dirty_pages_pct` | `0` | `90` | Forces background page cleaners to flush dirty `16 KiB` pages continuously at `100%` I/O capacity whenever any dirty page exists (`dirty_pct >= 0`). |
| `--innodb_redo_log_capacity` | `128M` | `100M` | Small circular WAL forces frequent async/sync checkpoint flushing of `.ibd` pages instead of deferring writes. |
| `--innodb_lru_scan_depth` | `4096` | `1024` | Scans `4×` deeper down each Buffer Pool LRU tail per second to aggressively evict and write dirty pages. |
| `--innodb_page_cleaners` | `16` | `4` | Increases cleaner concurrency *(note: capped by `innodb_buffer_pool_instances`, which defaults to `1` when `buffer_pool_size < 1G`)*. |
| `--innodb_write_io_threads` | `16` | `4` | Increases parallel async I/O threads submitting `O_DIRECT` `.ibd` writes simultaneously to keep NVMe queue depth high. |
| `--innodb_flush_neighbors` | `0` | `0` | Prevents merging adjacent pages into `32K`/`64K` sequential writes; keeps writes as discrete `16 KiB` operations. |
| `--innodb_io_capacity` | `20000` | `200` | Raises baseline IOPS budget for background page cleaners to match NVMe throughput. |
| `--innodb_io_capacity_max` | `40000` | `2000` | Raises peak IOPS ceiling when InnoDB flushes aggressively (`dirty_pct >= max_dirty_pages_pct`). |

### Reference `start_db` Snippet

```toml
start_db = '''
podman run --replace -d \
  --name mysql-noatom \
  -v /mnt/nonatomssd/mysql:/var/lib/mysql:Z \
  --network=host \
  mysql-atomic-sw \
  mysqld \
    --user=root \
    --log-error-verbosity=3 \
    --innodb_flush_method=O_DIRECT \
    --innodb_doublewrite=OFF \
    --innodb_buffer_pool_size=256M \
    --innodb_max_dirty_pages_pct_lwm=0 \
    --innodb_max_dirty_pages_pct=0 \
    --innodb_redo_log_capacity=128M \
    --innodb_lru_scan_depth=4096 \
    --innodb_page_cleaners=16 \
    --innodb_write_io_threads=16 \
    --innodb_flush_neighbors=0 \
    --innodb_io_capacity=20000 \
    --innodb_io_capacity_max=40000
'''
```

---

## 3. Workload (`sysbench`) Recommendations

| Setting | Recommended | Why It Matters |
| :--- | :--- | :--- |
| **Workload script** | `oltp_update_non_index` or `oltp_write_only` | Replaces ~78% `SELECT` traffic in `oltp_read_write` with 100% page-dirtying write queries. |
| **`--rand-type`** | `uniform` | Touches all pages across the entire table evenly instead of keeping the same ~5% hot rows cached in RAM (`special`). |
| **`SB_THREADS`** | `64` – `128` | Generates dirty pages faster than background flushers drain them, keeping NVMe Submission Queues saturated at power cut. |
| **`SB_TIME`** | `600` *(> `workload_run_sec_max`)* | Guarantees the workload never exits cleanly before `ipmitool` cuts power. |

---

## 4. System, Filesystem & Verification Settings

### A. Prevent Soft ACPI Shutdowns During IPMI Power Off
Many BMCs implement `ipmitool chassis power off` via a 4-second `PWRBTN#` assertion. Ignore ACPI power-key events on the target server so Linux does not cleanly shut down `mysqld` before the 4-second hardware power cut triggers:

```ini
# /etc/systemd/logind.conf.d/ignore-power-key.conf
[Login]
HandlePowerKey=ignore
HandlePowerKeyLongPress=ignore
```
*(Also disable `acpid` if installed: `sudo systemctl disable --now acpid.service acpid.socket`.)*

### B. Drain ATX PSU Hold-Up Capacitors Faster (`run_parallel_workload`)
Run a CPU stressor alongside `sysbench` in `config.toml`:
```toml
run_parallel_workload = "stress-ng --cpu 0 --timeout 600s"
```
High CPU wattage (`200W–350W+` vs. `~60W` idle) drains the ATX PSU's capacitors several times faster after `PS_ON#` drops, collapsing the M.2 `3.3V` rail faster.

### C. Filesystem Extent Fragmentation (Non-Atomic Baseline)
On aged non-atomic filesystems, `16 KiB` file ranges span discontiguous `4 KiB` / `8 KiB` extents, forcing Linux `O_DIRECT` to split one `16 KiB` `pwrite()` into 2–4 separate NVMe Write commands. Pre-fragmenting free space into `4 KiB` / `8 KiB` extents before `sysbench prepare` reproduces realistic filesystem aging (whereas `RWF_ATOMIC` filesystems enforce aligned `16 KiB` extents to prevent splitting).

### D. Reliable Corruption Detection (`check_db_stable`)
* **Omit `--fast` on `mysqlcheck`:** `--fast` skips tables InnoDB believes were closed cleanly; full `mysqlcheck` scans B-tree pages and verifies `16 KiB` page checksums/LSNs.
* **Match `mysqlcheck` output (`check_db_stable_fail_pattern`):** Because `mysqlcheck` returns exit code `0` even when reporting `Error : ...` / `error : Corrupt`, keep:
  ```toml
  check_db_stable_fail_pattern = '(?im)^\s*error\s*:'
  ```

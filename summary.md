# Page-Cache Fairness Benchmark — How It Works & How To Run It

This document explains, in detail, **what this benchmark does, how the code is
structured, what it measures, and the exact step-by-step commands to run the
experiments on a Linux host.**

It accompanies the research spec and implementation plan in `README.md`. Where that describes *what* to build, this file describes *what was built here* and *how to operate it*.

> **Platform:** the interference experiments require **Linux with cgroup v2**.
> Everything cgroup/PSI/`memory.stat`/`/proc/vmstat`/`iostat`-related is
> Linux-only; on macOS the binary builds and the fio command construction works,
> but the sampler, cgroup setup, and iostat logging all no-op. Optional
> bpftrace entry-count tracing is on by default (needs bpftrace + root +
> BTF/symbols) and is skipped when unavailable; use `--no-bpftrace` to disable.
> Use macOS only to iterate on the code.

---

## 1. The question the benchmark answers

Two tenants share one machine's Linux page cache:

- **Tenant A — the victim (`client1_steady`)**: a latency-sensitive, rate-limited
  sequential reader whose hot set *fits* in cache. We measure its **p99 read
  latency**.
- **Tenant B — the noisy neighbor (`client2_noisy`)**: a workload that pollutes
  the cache and/or dirties pages.

The thesis decomposes A's p99 spike into two mechanisms:

```
A's p99 spike = (A's cache-miss rate) × (baseline_read + writeback_queue_delay(B))
                └─────── Mechanism 1 ───────┘   └────────── Mechanism 2 ──────────┘
```

- **Mechanism 1 — eviction only.** Set `[client2_noisy]` to a large **clean read**
  (`read` sequential or `randread`); B fills the LRU, evicts A's hot pages; A
  re-faults from disk. No writeback involved.
- **Mechanism 2 — eviction + dirty writeback.** Set `[client2_noisy]` to
  **buffered `randwrite`**; B evicts A's pages **and** produces dirty pages whose
  flushes land in the I/O queue ahead of A's read misses, inflating each miss.

**Headline comparison:** A's p99 under Mechanism 1 vs Mechanism 2 *at the same
eviction rate*. If the writer case is worse, dirty writeback amplifies the miss
penalty beyond what memory sizing alone can fix — the core result.

---

## 2. Files in this project

| File | Role |
|---|---|
| `benchmark.c` | The whole harness: config parsing, fio command building, cgroup setup, run orchestration, telemetry sampling. |
| `fairness_configs.ini` | Workload definitions: `client1_steady` + `client2_noisy` only. |
| `cgroup_shared.ini` | Shared-pool cgroup layout (2G parent cap; both tenants under it). |
| `cgroup_isolated.ini` | Isolated layout. Each tenant has `memory.max` and `memory.min` set to 1G. |
| `check_cgroups.sh` | Pre-flight cgroup v2 validation (mirrors harness cgroup setup). |
| `setup_test_files.sh` | Optional one-time creation of dense size-tagged test files. |
| `Makefile` | Build + convenience run targets (`-lm` for the clat percentile math). |
| `benchmark_analysis.py` | Reads a results dir and prints p99, refault deltas, per-phase memory/IO utilization, PSI stalls. |
| `report_io_utilization.py` | Per-second join: p99, `io.stat` throughput, `io.pressure`, page-cache residency. |
| `report_latency_timeseries.py` | Per-second join: Client1 p99 vs Client2 `memory.high`, labeled by phase. |
| `senpai_high.py` | Optional. `--senpai-high` runs upstream Senpai on Client2 `memory.high`. |
| `benchmark_results/` | Output (created on first run; `-o` overrides). |
| `summary.md` | This document. |

---

## 3. How `benchmark.c` is structured

The program is a single C11 file organized into clear sections. Reading
top-to-bottom:

1. **Configuration model** — `PhaseConfig` (one I/O phase), `ClientConfig` (a
   tenant with N phases), `CgroupConfig`/`CgroupSet` (cgroup layout).
2. **INI parsing** — `parse_config()` reads `fairness_configs.ini` into
   `ClientConfig`s. Keys are either bare (`file_size`) or **phase-prefixed**
   (`phase_0_pattern`, `phase_1_rate_iops`, …). `apply_phase_key()` routes each
   key to the right `PhaseConfig` field.
3. **cgroup setup** — `parse_cgroup_config()` reads the cgroup `.ini`;
   `setup_cgroups()` creates the cgroup directories (with `mkdir -p` semantics)
   and writes `memory.max` / `memory.low` / `io.weight`.
   `enable_controllers()` writes `+memory +io` into each parent's
   `cgroup.subtree_control` so nested cgroups actually expose those knobs.
4. **memory.stat / io.stat snapshots** — `record_memstat()` reads
   `workingset_refault_file`, `memory.current`, `pgfault`, `pgmajfault`,
   `file`, and `io.stat` `rios` before and after each phase and writes the
   **deltas** to `memstat/<client>_<mode>.csv`.
5. **Telemetry sampler** — a forked child (`run_sampler()`) that every second
   samples:
   - `/proc/vmstat`: `nr_dirty`, `nr_writeback`, `pgpgin`, `pgscan_kswapd`
     → `dirty/vmstat_<mode>.csv`
   - per-cgroup `memory.stat`: `file_dirty`, `file_writeback`
     → `dirty/<cg>_<mode>_dirty.csv`
   - per-cgroup PSI: `memory.pressure` + `io.pressure` `some` totals
     → `psi/<cg>_<mode>.csv`
   - per-tenant 1 Hz timeseries: `memory.current`, `memory.stat` `file`,
     refaults, memory/io PSI totals, and `io.stat` `rios`/`wios`/`rbytes`/
     `wbytes` (plus `memory.high` for client2)
     → `timeseries/<client>_<mode>.csv`
   - bpftrace (default): cumulative memstall / iowait entry counts
     → `psi_entries/run_<mode>.csv`
6. **iostat logger** — `start_iostat()` forks `iostat -dx 1` into
   `iostat/run_<mode>.iostat`.
6b. **clat aggregators** — one forked child per tenant tails fio's
   `write_hist_log` output in `clat/` (1 s completion-latency histograms,
   `log_hist_msec=1000`), sums the bins across that phase's jobs, and emits
   1 Hz p50/p95/p99/p99.9 + IOPS to `timeseries/<client>_latency_<mode>.csv`.
   Disable with `--no-clat-log` / `--no-timeseries`.

   Histograms rather than per-IO samples are deliberate: fio buffers every
   latency sample in anonymous memory and flushes it to a log file at job end,
   and both are charged to the tenant's own cgroup. At 50k IOPS that added
   hundreds of MiB to `memory.stat:file` — inside the very memory domain
   `memory.low` / `memory.min` are meant to protect — and showed up as a jump
   in cached bytes exactly when a phase ended. A 1 s histogram is ~1216–1856
   bins/s instead of millions of records, so it still yields a true per-second
   p99 without consuming the budget under test.
7. **fio command builder** — `build_fio_cmd()` turns a `PhaseConfig` into an
   `fio` command line, including `--output-format=json+` so the JSON contains
   `clat_ns.percentile` (p99/p999).
8. **Test-file management** — the harness auto-creates
   `test_file_<client>_<file_size>` (e.g. `test_file_client1_steady_512M`,
   `test_file_client2_noisy_32G`) via `ensure_test_file()`.
   `./setup_test_files.sh` / `make setup-files` is optional and uses a
   different `test_file_<size>` scheme. `drop_caches()` flushes the page cache
   before each cached-mode phase (or only before phase 0 with `--drop-once`).
9. **Run orchestration** — `run_clients()` pre-creates files, starts the
   sampler + iostat + clat aggregators (+ bpftrace unless `--no-bpftrace`),
   then for each phase:
   drops caches, takes a "before" memstat snapshot, forks all clients' fio for
   that phase concurrently (each joining its cgroup via `cgroup.procs`), waits,
   takes an "after" snapshot (computing refault / `rios` / `file` deltas), and
   finally writes a line to `summary.txt`.
10. **CLI** — `main()` parses options and dispatches `dual` / `all` / a single
    named workload.

### The three previously-planned features (now implemented)

These were listed as TODOs ("planned fields") in the spec and are wired end to
end. Search `benchmark.c` for the tags:

- **`[TODO-1]` `phase_N_random_distribution`** — e.g. `zipf:1.2`. Emitted as
  `--random_distribution=zipf:1.2`. Skews the victim's hot set so refaults
  actually hurt (uniform-random over a huge file never reuses pages, making
  eviction "free" and hiding the effect).
- **`[TODO-2]` `phase_N_fdatasync` / `phase_N_fsync`** — flush cadence on
  `[client2_noisy]`. Emitted as `--fdatasync=N` / `--fsync=N` (e.g. checkpoint
  or WAL-style periodic flushes).
- **`[TODO-3]` `/proc/vmstat` + per-cgroup `file_dirty` sampling** — the 1 Hz
  sampler above. Directly measures dirty-page accumulation and writeback, the
  observable signature of Mechanism 2.

---

## 4. What gets measured (and where it lands)

After a run, `benchmark_results/` (or your `-o <dir>`) contains:

```
benchmark_results/
├── <client>_<mode>_p<phase>.json   # raw fio JSON+ (has clat_ns.percentile.99)
├── summary.txt                     # one line per (mode, client-set) run
├── iostat/run_<mode>.iostat        # iostat -dx 1 device stats
├── psi/<cg>_<mode>.csv             # ts, wall_time, mem_some_total_us, io_some_total_us
├── psi_entries/run_<mode>.csv      # bpftrace entry counts (default)
├── psi_entries/probe_<mode>.bt     # generated bpftrace script
├── psi_entries/cgroup_ids_<mode>.txt
├── memstat/<client>_<mode>.csv     # phase, when, refault/memory.current/pgfault/rios/file + deltas
├── clat/                           # fio write_hist_log 1 s histograms + generated .fio jobs
├── timeseries/                     # 1 Hz latency + cgroup/io.stat CSVs (see §14)
├── senpai/senpai_<client>_<mode>.log  # only with --senpai-high
└── dirty/
    ├── vmstat_<mode>.csv           # ts, nr_dirty, nr_writeback, pgpgin, pgscan_kswapd
    └── <cg>_<mode>_dirty.csv       # ts, file_dirty, file_writeback
```

| Metric | Meaning | Source |
|---|---|---|
| **p99 / p999 read latency (µs)** | The objective. Lower & flatter under interference = better isolation. | fio `clat_ns.percentile` |
| **Per-second p99** | 1 Hz p50/p95/p99/p99.9 + IOPS per tenant, for aligning damage with phases. | `timeseries/<client>_latency_<mode>.csv` |
| **`workingset_refault_file_delta`** | Pages a tenant evicted and had to re-fault per phase (× 4 KiB ≈ bytes re-read). Direct eviction-churn measure. | `memstat/` |
| **`file` (page cache held)** | Per-cgroup page-cache residency; per phase and at 1 Hz. Counts *all* cache charged to the tenant, harness files included. | `memstat/`, `timeseries/` |
| **`target_file_resident_bytes`** | mincore residency of the tenant's own test file — the working set `memory.low`/`memory.min` is supposed to protect, free of harness overhead. `file - target` is that overhead. | `memstat/`, `timeseries/` |
| **`io.stat` `rios`/`rbytes`** | Completed device reads and **actual bytes** → per-second MiB/s. | `memstat/`, `timeseries/` |
| **PSI `some` stall (memory, io)** | Per-cgroup time stalled on memory / io (cumulative µs; diff for per-second). | `psi/`, `timeseries/` |
| **PSI entry counts (debug)** | memstall / iowait entries per logical read (not misses). | `psi_entries/` |
| **`memory.high`** | Client2 limit, sampled at 1 Hz and logged on every change (Senpai). | `timeseries/` |
| **`nr_dirty` / `nr_writeback`, `file_dirty`** | Dirty-page accumulation & writeback — Mechanism 2's fingerprint. | `dirty/` |
| **iostat r/w latency & queue depth** | Surfaces writeback contention at the device. | `iostat/` |
| **IOPS / BW** | Secondary throughput context, not the objective. | fio JSON |

---

## 5. Prerequisites

```bash
# Ubuntu / Debian
sudo apt-get install fio gcc make sysstat   # sysstat provides iostat

# Confirm cgroup v2 is the active hierarchy (should print "cgroup2fs")
stat -fc %T /sys/fs/cgroup

# Confirm PSI is enabled (file should exist)
cat /proc/pressure/memory
```

Also ensure enough disk: current defaults are **512M (A) + 32G (B) ≈ 33 GB**.
`df -h .` before running. The harness creates
`test_file_<client>_<file_size>` automatically on first use; `make setup-files`
is optional and uses a different `test_file_<size>` naming scheme.

### cgroup pre-flight check

```bash
# Validate cgroup v2 and ini layout before running experiments
sudo ./check_cgroups.sh --cgroup-config cgroup_shared.ini
sudo ./check_cgroups.sh --cgroup-config cgroup_isolated.ini
```

---

## 6. Build

```bash
make              # produces ./benchmark (links -lm for clat percentiles)
make setup-files  # optional dense size-tagged test files
make clean        # remove the binary
```

> **Makefile note:** the run-everything target is `make run-all` (not
> `make benchmark`) because `benchmark` is also the binary's filename and Make
> cannot have a phony target collide with a real file of the same name.

---

## 7. Quickstart (the primary experiment)

```bash
# A + B interference in cached mode, shared 2G cache pool.
# sudo is required for drop_caches + cgroup setup.
sudo ./check_cgroups.sh --cgroup-config cgroup_shared.ini
sudo ./benchmark -v --drop-once --cgroup-config cgroup_shared.ini \
  -m cached -o results/cached_dual dual

# Analyze
./benchmark_analysis.py results/cached_dual
./report_io_utilization.py results/cached_dual        # per-second p99 vs io.stat / io.pressure
./report_latency_timeseries.py results/cached_dual    # per-second p99 vs memory.high
cat results/cached_dual/summary.txt
```

> The results directory is created by `sudo`, so the report scripts may not be
> able to write their CSVs into it. Either `sudo chown -R $USER results/<dir>`
> or pass `-o ~/some.csv`; the table still prints either way.

> **Always run writer-B pairings with `-m cached`.** In `direct` mode fio
> bypasses the page cache, produces no dirty pages, and Mechanism 2 vanishes.

---

## 8. Step-by-step: the full experiment progression

Each step changes **one** variable from the previous, so any change in A's p99
or refault delta is attributable to that knob. Send each to its own results dir
with `-o` so the analysis script can compare them side by side.

### Step 0 — Baseline: A alone (optional; same as Case 1a)

```bash
sudo ./benchmark --cgroup-config cgroup_isolated.ini -m cached -o results/case1a client1_steady
```
*Expect:* flat p99, `workingset_refault_file_delta ≈ 0`. Reference for Δp99.

### Step 1 — Isolated baselines (each client alone)

```bash
sudo ./benchmark -m cached -o results/case1_a client1_steady
sudo ./benchmark -m cached -o results/case1_b client2_noisy
```
*Isolates:* standalone p99 / throughput per client; confirms B isn't
self-throttled.

### Step 2 — Concurrent, WITH isolation

```bash
sudo ./benchmark --cgroup-config cgroup_isolated.ini -m cached -o results/case2 dual
```
*Isolates:* whether separate per-tenant cgroups keep A's p99 ≈ baseline when
both run together. The checked-in file sets `memory.max` and `memory.min` to
1G for each tenant. *Expect:* p99 stays near baseline with that protection.

### Step 3 — Concurrent, NO memory limits (raw interference)

```bash
sudo ./benchmark --no-cgroup -m cached -o results/case3 dual
```
*Isolates:* interference with nothing protecting A. *Expect:* A's p99 spikes.

### Step 4 — Heavy eviction pressure (B ≫ cache under a tiny cap)

Edit `cgroup_shared.ini` so the parent cap is `512M`, and set
`client2_noisy`'s `file_size = 48G` with `phase_0_pattern = randread` in
`fairness_configs.ini`. (For strict single-variable rigor, split into 4a =
file-size change only, 4b = add the 512M cap.)

```bash
sudo ./benchmark --cgroup-config cgroup_shared.ini -m cached -o results/case4 dual
```
*Expect:* maximal eviction of A; large `workingset_refault_file_delta` for A.

### Step 5 — Cap only the victim

Set `[client1_steady] memory.max = 1G` in `cgroup_shared.ini`.

```bash
sudo ./benchmark --cgroup-config cgroup_shared.ini -m cached -o results/case5 dual
```
*Isolates:* whether limiting **A** helps or hurts A's p99.

### Step 6 — Cap only the aggressor (the standard mitigation)

Set `[client2_noisy] memory.max = 1G` in `cgroup_shared.ini`.

```bash
sudo ./benchmark --cgroup-config cgroup_shared.ini -m cached -o results/case6 dual
```
*Isolates:* whether capping **B** bounds A's p99.

### Step 7 — Mechanism 1 vs Mechanism 2 at equal eviction

Edit `[client2_noisy]` for read vs write B (match `phase_0_rate_iops`), then:

```bash
# B = randread (or sequential read) — Mechanism 1
sudo ./benchmark --cgroup-config cgroup_shared.ini -m cached -o results/scan dual

# B = randwrite — Mechanism 2
sudo ./benchmark --cgroup-config cgroup_shared.ini -m cached -o results/write dual

./benchmark_analysis.py results/scan
./benchmark_analysis.py results/write
```
*Expect:* at the same rate, `results/write` shows **higher** A p99 and elevated
`nr_writeback` — dirty-writeback amplification.

### Intensity sweep (Fig 1)

Sweep `client2_noisy` `phase_0_rate_iops` (1k / 5k / 20k / 50k / unlimited) for
read vs write B; plot B intensity (X) vs A p99 (Y).

---

## 9. Command-line reference

```
./benchmark [options] <workload | dual | all>

  <workload>   Run a single [section] from the config (baseline/characterization)
  dual         Run client1_steady (A) + client2_noisy (B) concurrently (primary)
  all          Run every [section] in the config

  -c, --config PATH        Workload ini            (default: fairness_configs.ini)
      --cgroup-config PATH  cgroup layout ini (enables cgroup setup)
      --no-cgroup          Disable cgroup setup (shared page-cache pool)
      --no-psi             Disable PSI sampling
      --no-bpftrace        Disable bpftrace memstall/iowait entry counts
      --drop-once          Drop caches before phase 0 only (cache persists after)
      --clat-log           1 s fio clat histograms for client1+client2 (default on)
      --no-clat-log        Disable clat histogram logs (no per-second p99 series)
      --no-timeseries      Disable timeseries/ CSVs
      --senpai-high        After phase 0, run senpai_high.py on Client2 memory.high
      --senpai-script PATH Path to senpai_high.py  (default: ./senpai_high.py)
      --senpai-client NAME Section to size         (default: client2_noisy)
      --senpai-min-size N  memory.high floor       (default: 100M)
      --senpai-max-size N  memory.high ceiling     (default: 2G)
      --senpai-interval N  Probe interval seconds  (default: 6)
      --senpai-pressure N  Stall budget in µs      (default: 10000)
  -m, --mode MODE          cached | direct | both  (default: both)
  -o, --output DIR         Results dir             (default: benchmark_results)
  -v, --verbose            Verbose logging (prints each fio command)
  -h, --help               Help
```

---

## 10. Configuring workloads

Sections in `fairness_configs.ini`. Sizes are chosen **relative to the cgroup
cap** (2G shared), not as fixed absolutes. Keys are phase-prefixed
(`phase_N_...`) so a section can define multiple phases. The block below matches
the checked-in defaults (`read` + `randread`, Mechanism 1 pairing):

```ini
[client1_steady]                       ; Tenant A — the victim
description = Victim: 512M randread at 50k IOPS for 60s on every phase
file_size = 512M                       ; ~0.25x the 2G cap (fits in cache)
phase_0_pattern = randread
phase_0_block_size = 4k
phase_0_rate_iops = 50000              ; rate-limit keeps p99 an SLO signal
phase_0_iodepth = 8
phase_0_numjobs = 1
phase_0_runtime = 60
phase_0_ioengine = libaio
; phases 1..3 repeat the same shape (contended window, then recovery)

[client2_noisy]                       ; Tenant B — sequential reader (Mech 1 default)
description = Aggressor: 32G sequential read, 1k -> 73k x4 -> 1k -> 1k
file_size = 32G                        ; ~16x the cap
phase_0_pattern = read
phase_0_block_size = 4k
phase_0_rate_iops = 1024               ; quiet
phase_0_iodepth = 8
phase_0_numjobs = 1
phase_0_runtime = 60
phase_0_ioengine = libaio

phase_1_rate_iops = 73000              ; burst (rate_iops is PER JOB)
phase_1_numjobs = 4
```

Edit `phase_0_pattern`, `phase_0_block_size`, and related keys on `[client2_noisy]`
for Mechanism 1 (`read` / `randread`) vs Mechanism 2 (`randwrite`).

**Recognized phase keys:** `pattern`, `block_size`, `ioengine`, `rate_iops`,
`iodepth`, `numjobs`, `runtime`, `rwmixread`, `random_distribution`,
`fdatasync`, `fsync`. Bare (non-phased) keys are applied to phase 0.

---

## 11. Interpreting the analysis output

`./benchmark_analysis.py <dir>` prints the per-phase sections:

- **READ LATENCY** — per-JSON p99/p999 + IOPS/BW. Compare A's p99 across cases.
- **WORKING SET REFAULTS** — per-phase refault delta per cgroup, pages + MiB.
  Bigger delta = more eviction churn inflicted.
- **MEMORY** — per-cgroup `memory.current` before/after each phase plus
  `pgfault_delta` / `pgmajfault_delta`.
- **MEMORY + IO UTILIZATION** — per phase: `memory.stat` `file` (page cache
  held), Δ`file`, and `io.stat` `rios/s` + Δ`rios`.
- **PSI** — one table: memory/io `some` stall **duration** (ms) plus bpftrace
  memstall / I/O-wait **entry** counts and total reads, per phase. Not miss rates.

Per-second views live in two standalone scripts (they read `timeseries/`, write
a CSV, and print the full table):

```bash
./report_io_utilization.py <dir> [mode] [-o PATH] [--no-write]
./report_latency_timeseries.py <dir> [mode] [-o PATH] [--no-write]
```

- **`report_io_utilization.py`** → `timeseries/per_second_io_utilization.csv`
  with `wall_time`, `phase`, `c1_p99_us`, `c2_p99_us`, `c1_mibps`/`c2_mibps`
  (Δ`rbytes` → MiB/s; falls back to `rios × 4 KiB` on pre-`rbytes` runs),
  `c1_rios`/`c2_rios`, `c1_file_mib`/`c2_file_mib`, `c1_io_us`/`c2_io_us`
  (Δ `io_psi_some_total_us`).
- **`report_latency_timeseries.py`** → `timeseries/per_second_latency_timeseries.csv`
  with `wall_time`, `phase`, `c1_p99_us`, `c2_memory_high_bytes`
  from the 1 Hz client2 timeseries.

`phase` comes from the memstat before/after wall-clock windows; seconds outside
any phase (drop-caches, spawn, teardown) are labeled `gap`. All cumulative
counters are reported as **per-second deltas**.

**What the thesis predicts you'll see:**
1. `client1_steady` alone → flat p99, ~100% cache hit.
2. A + B (`randread` / sequential read) → p99 rises from refaults (Mechanism 1).
3. A + B (`randwrite`) → p99 rises **further** at the same eviction rate
   (Mechanism 2), with elevated `nr_writeback`.
4. A + B under `cgroup_isolated.ini` (`memory.max` and `memory.min` are 1G) →
   partial recovery for read B; **residual** p99 elevation for write B —
   motivating cross-layer memory↔I/O coordination.

---

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `warning: open .../memory.max: ...` | Run under `sudo`; ensure cgroup v2 (`stat -fc %T /sys/fs/cgroup` = `cgroup2fs`). |
| cgroup knobs don't stick / files missing | Parent's `cgroup.subtree_control` lacks the controller. The harness writes `+memory +io`, but a restrictive systemd delegation can still block it; run `sudo ./check_cgroups.sh` first, or run inside a delegated scope (`systemd-run --user -p Delegate=yes ...`) or as root. |
| Empty `psi/` dir | PSI disabled in kernel, or you passed `--no-psi`. Check `cat /proc/pressure/memory`. |
| Empty `psi_entries/` | Passed `--no-bpftrace`, or bpftrace/root/BTF unavailable. Check `psi_entries/*.err`. |
| Empty `iostat/` | `sysstat` not installed (`apt-get install sysstat`). |
| `drop_caches` warning | Needs root; run with `sudo`. |
| `PermissionError` writing `per_second_*.csv` | The results dir was created by `sudo`. Run `sudo chown -R $USER results/<dir>` or pass `-o ~/out.csv`. |
| `Permission denied: ./report_*.py` | Lost the exec bit on this host: `chmod +x report_*.py`, or run `python3 report_*.py`. |
| No `rbytes` / `rios` columns | Results predate the 1 Hz `io.stat` sampler; re-run the benchmark. |
| Missing per-second rows / `-` p99 | No latency sample that second (phase edges, aggregator flush, or no IO). |
| p99 looks like saturation, not SLO | The victim must be rate-limited (`rate_iops`); an unlimited victim measures throughput, not an SLO tail. |
| Refault delta ≈ 0 despite B thrashing | Victim using uniform random over a huge file — set `random_distribution = zipf:1.2` and size its file to ~0.5× cap. |

---

## 13. Relationship to the repo's own docs

- `README.md` — research spec (what to study and why).
- `BENCHMARK.md` — how to build and run the harness, workloads, and expected outcomes.
- The harness is a single `benchmark.c` plus `benchmark_analysis.py`. Config keys, workloads, telemetry, and experiment cases follow that plan.
- This file — operational detail: the 1 Hz timeseries and the
  per-second report scripts (§14).

---

## 14. 1 Hz timeseries

The harness records **1 Hz timeseries** by default (Linux + cgroups): per-tenant
read latency percentiles and cgroup memory, PSI, and `io.stat`. Client2's
series also includes a once-a-second `memory.high` sample.

> **Platform:** cgroups, PSI, and timeseries require **Linux (cgroup v2)** and
> `sudo`. macOS is build-only.

All CSVs use **`wall_time`** (unix seconds) for alignment.

| File | Rate | Contents |
|------|------|----------|
| `client1_steady_latency_<mode>.csv` | 1 Hz | `p50_us`, `p95_us`, `p99_us`, `p999_us`, `iops` |
| `client2_noisy_latency_<mode>.csv` | 1 Hz | same for Tenant B |
| `client1_steady_<mode>.csv` | 1 Hz | `memory.current`, `file`, `workingset_refault_file`, memory/io PSI, **`rios`/`wios`/`rbytes`/`wbytes`** (cumulative) |
| `client2_noisy_<mode>.csv` | 1 Hz | same + sampled `memory_high_bytes` |
| `per_second_io_utilization.csv` | derived | `report_io_utilization.py`: phase, p99, Δ`rios`, Δ IO PSI, Δ`rbytes`→MiB/s, `file` MiB |
| `per_second_latency_timeseries.csv` | derived | `report_latency_timeseries.py`: phase, c1 p99, c2 `memory.high` |

Cumulative columns (`*_psi_some_total_us`, `rios`, `wios`, `rbytes`, `wbytes`)
must be **differenced between consecutive rows** for per-second rates — that is
what the report scripts do.

Raw series (optional):

```bash
cat results/cached_dual/timeseries/client1_steady_latency_cached.csv
cat results/cached_dual/timeseries/client2_noisy_latency_cached.csv
```

Raw 1 s clat histograms land in `clat/` (small; see 6b for why these replaced
per-IO sample logs).

Disable recording if needed:

- `--no-timeseries` — skip all `timeseries/` CSVs
- `--no-clat-log` — skip client1/client2 fio clat histogram logs (no per-second latency series)

### Reading the series

- Compare A's phase-1 p99 / refaults against an unrestricted `dual` baseline in
  another `-o` directory.
- In the per-second table, watch for A's `c1_file_mib` falling while B's
  `c2_mibps` is high — that is eviction in progress, usually just before
  `c1_p99_us` degrades.

### Upstream Senpai (`--senpai-high`)

`--senpai-high` forks `senpai_high.py` after phase 0 and sizes only Client2's
`memory.high` from memory PSI. It does not set Client1 `memory.low`. The
process is stopped when the run finishes. Stdout is
`senpai/senpai_<client>_<mode>.log`.

```bash
sudo ./benchmark -v --drop-once --senpai-high \
  --cgroup-config cgroup_shared.ini \
  -m cached -o results/senpai_high dual
```

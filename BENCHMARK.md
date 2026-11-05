# Pagecache Fairness Benchmark (C)

This repository's benchmarking suite measures **page-cache performance
isolation failures** between co-located tenants — specifically the **p99
read-latency spike** a latency-sensitive reader (Tenant A) suffers when a noisy
neighbor (Tenant B) shares the page cache. The research writeup is in
[`README.md`](README.md); operational detail is in [`summary.md`](summary.md).

## 🖼️ Poster

For a visual overview of the problem, benchmark, Senpai-based controller, and results, see the [Page-Cache Fairness Under Multi-Tenant Memory Contention poster](https://docs.google.com/presentation/d/1arQ_6M-1S_GMU1uC8SFhAfQ46Eg2nBBoc3mx7sdDDEs/edit?usp=sharing).

## 🎯 Purpose

The benchmark exists to validate the two-component model of the p99 spike

```
A's p99 spike = (A's cache-miss rate) × (baseline_read + writeback_queue_delay(B))
```

The experiments isolate the **two interference mechanisms**:

- **Mechanism 1 — eviction only:** B runs a large *clean* sequential scan, fills
  the LRU, and evicts A's hot pages. A re-faults them from disk. No writeback.
- **Mechanism 2 — eviction + dirty writeback:** B runs *buffered writes*, so it
  both evicts A's pages **and** generates dirty pages whose flushes land in the
  I/O queue ahead of A's reads, inflating A's per-miss latency.

The headline result compares A's p99 under Mechanism 1 vs Mechanism 2 **at the
same eviction rate** — if the writer case is worse, that confirms dirty
writeback amplifies the miss penalty beyond what memory sizing alone can fix.

> The old framing ("small sequential workloads win, large random workloads lose")
> is a side effect, not the object of study. Workloads below are organized around
> **victim + noisy-neighbor pairings**, not around a single-workload sweep.

## 📁 Project Structure

```
.
├── fairness_configs.ini           # Workload definitions (victim + aggressor)
├── cgroup_shared.ini              # Shared-cache cgroup layout (2G pool)
├── cgroup_isolated.ini            # Isolated per-tenant cgroup layout
├── check_cgroups.sh               # Pre-flight cgroup v2 validation script
├── benchmark.c                    # C benchmark implementation
├── benchmark                      # Compiled C binary
├── Makefile                       # Build configuration
├── benchmark_analysis.py          # Results analysis (fio + PSI + refaults)
├── report_latency_timeseries.py   # Per-second c1 p99 vs c2 memory.high
├── report_io_utilization.py       # Per-second p99 / io.stat / PSI / throughput CSV
├── senpai_high.py                 # --senpai-high: upstream Senpai, memory.high only
├── summary.md                     # How the harness runs
├── results/                       # Per-run output (`-o` overrides; gitignored)
│   ├── *.json                     # Raw fio output (per client per phase)
│   ├── clat/                      # 1 s fio clat histograms (client1+2, default on)
│   ├── timeseries/                # 1 Hz latency + cgroup CSVs
│   ├── senpai/                    # senpai_high.py stdout when --senpai-high is used
│   ├── iostat/                    # iostat -dx logs
│   ├── psi/                       # Per-cgroup PSI time series (CSV)
│   ├── psi_entries/               # bpftrace entry-count CSVs (default on)
│   ├── dirty/                     # vmstat + per-cgroup file_dirty samples
│   └── memstat/                   # Per-cgroup memory.stat snapshots (CSV)
├── setup_test_files.sh            # Optional pre-create helpers (size-tagged)
├── test_file_<client>_<size>      # Auto-created by harness (e.g. …_512M, …_32G)
├── BENCHMARK.md                   # This file
└── README.md                      # Research writeup
```

## Getting Started

### Prerequisites
- `fio` (I/O benchmark tool)
- `gcc` with C11 support
- `make` (build tool)
- `iostat` / `sysstat` (I/O monitoring)
- `bpftrace` (for default memstall/iowait entry counts; needs root + BTF.
  Install it, or pass `--no-bpftrace` to skip)
- Sufficient disk space for test files (**~33 GB** with current defaults:
  victim `512M` + aggressor `32G`)

### Install Dependencies (macOS)
```bash
brew install fio gcc make
```

### Install Dependencies (Ubuntu/Debian)
```bash
sudo apt-get update
sudo apt-get install fio gcc make sysstat bpftrace
```

### Build the Benchmark
```bash
make
```

### Test files
The harness auto-creates missing files named `test_file_<client>_<file_size>`
(e.g. `test_file_client1_steady_512M`, `test_file_client2_noisy_32G`) on first
use via `fio --create_only`. Pre-creating with `make setup-files` is optional
and uses a different `test_file_<size>` naming scheme — the dual run does **not**
require it.

> **Disk space:** a 32G aggressor file needs a filesystem with room to spare.
> `fio: err=28 … No space left on device` during phase 0 usually means ENOSPC;
> check `df -h .` before running.

## 📊 Workload Design

Workloads are grouped by the **role** they play in an experiment. Sizes are
expressed **relative to the cgroup memory cap** (`memory.max = 2G` in
`cgroup_shared.ini`): ~0.25× cap for the victim hot set (`512M`), ~16× for B
(`32G`).

Default phases (see `fairness_configs.ini`): **four phases of 60s** (0–3).
Pass `--drop-once` so the cache persists after phase 0. B ramps
quiet → burst → quiet → quiet to drive eviction.

### Tenant A — the victim (what we measure)

`client1_steady`: `file_size = 512M`, cached mode, rate-limited
(`rate_iops=50000`, `iodepth=8`, `numjobs=1`).

| Phase | Pattern    | Role |
|-------|------------|------|
| 0 | `randread` | Warm / best-case reference |
| 1 | `randread` | Contended window (measure p99 while B bursts) |
| 2 | `randread` | Recovery after B goes quiet again |
| 3 | `randread` | Recovery continues (same shape as phase 2) |

Report clat **avg / p50 / p99 / p999**. Optional
`phase_N_random_distribution = zipf:1.2` skews the hot set so refaults hurt.

### Tenant B — the noisy neighbor (Mechanism 1 default)

`client2_noisy`: `file_size = 32G`, sequential `read`, buffered.

| Phase | `rate_iops` | `iodepth` × `numjobs` | Intent |
|-------|-------------|-----------------------|--------|
| 0 | 1024        | 8 × 1                 | Quiet — little eviction |
| 1 | 73000       | 8 × 4                 | Burst — flood cache / device (note: `rate_iops` is **per job**) |
| 2 | 1024        | 8 × 1                 | Quiet again |
| 3 | 1024        | 8 × 1                 | Still quiet (same shape as phase 2) |

Edit `[client2_noisy]` for other B behaviors:

| Pattern | bs | Mechanism |
|---------|-----|-----------|
| `read` (sequential) | 4k / 1M | **1** — LRU pollution, no writeback |
| `randread` | 4k | **1** — random read pressure |
| `randwrite` | 4k | **2** — eviction + dirty writeback |

### Baselines (Case 1)

| Run | Command | Purpose |
|-----|---------|---------|
| A alone | `./benchmark client1_steady` | Flat-p99 reference; Δp99 = (A+B) − this |
| B alone | `./benchmark client2_noisy` | Confirm B sustains its load without A |

## 🔬 Experiment Structure

The unit of experiment is a **dual-client pairing** (`dual` mode), run under the
four isolation conditions from the thesis:

| Condition | Setup | Expected outcome |
|---|---|---|
| Baseline | A alone (`client1_steady`) | p99 flat; cache-hit ≈ 100% after warm |
| Interference | A + B, shared pool (`cgroup_shared.ini`) | A's p99 / throughput hurt in B's burst phase; writer B worse than reader B at equal eviction |
| cgroup v2 | Separate per-tenant cgroups (`cgroup_isolated.ini`; `memory.max` and `memory.min` are 1G) | Partial recovery with protection; residual p99 elevation under writer B |
| Proposed policy | Not a harness mode | A's p99 within SLO for both B variants |

**Key comparison:** edit `[client2_noisy]` to sequential/`randread` vs `randwrite`,
then run `dual` at the **same rate** → does dirty writeback add p99 beyond clean
eviction?

**Intensity sweep (Fig 1):** sweep B's burst `phase_1_rate_iops` (e.g. 1k / 5k /
20k / 50k / 100k) for read vs write B; plot B intensity (X) vs A p99 (Y).

### Axes that matter (and ones that don't)

- **Block size** is a first-class variable: large sequential `bs` = clean scan;
  `bs=4k` random / write = different pressure.
- **Access distribution** matters: uniform random over a huge file never reuses
  pages. Use `zipf` for the victim so refaults actually hurt.
- **Buffered vs direct:** writer B must run in **cached** mode — `direct=1`
  generates no dirty page cache and disables Mechanism 2.
- **`numjobs` over `iodepth`** for concurrency in cached mode: buffered I/O
  often behaves closer to sync; `iodepth` matters more for `direct` baselines.
- **`--drop-once`:** required for multi-phase eviction stories so A's warm set
  and B's reads persist across phases (default drops cache every phase).

## 🧪 Experiment Cases (change one variable at a time)

Hold the two tenants (`client1_steady`, `client2_noisy`), patterns, and runtime
fixed, and change **exactly one knob per step**.

| Case | Name | Concurrency | cgroup / memory config | The single variable changed | What it isolates |
|---|---|---|---|---|---|
| 1 | Isolated Baselines | Each client **alone** | isolated or none | — (reference) | Standalone p99 / throughput |
| 2 | Isolated Clients | Concurrent | `cgroup_isolated.ini` | +concurrency with isolation | Whether separate cgroups keep p99 ≈ baseline |
| 3 | Shared Dual (default) | Concurrent | `cgroup_shared.ini` parent `memory.max = 2G` | shared 2G pool; B streams 32G | Interference with nothing protecting A |
| 4 | Shared Client 1 Limited | Concurrent | shared + `client1_steady memory.max = 1G` | cap the **victim** | Whether limiting A helps or hurts A's p99 |
| 5 | Shared Client 2 Limited | Concurrent | shared + `client2_noisy memory.max = 1G` | cap the **aggressor** | Does capping B bound A's p99? |

### Running each case

```bash
# Case 1 — Isolated Baselines
sudo ./benchmark -v --drop-once --cgroup-config cgroup_isolated.ini -m cached -o results/client1 client1_steady
sudo ./benchmark -v --drop-once --cgroup-config cgroup_isolated.ini -m cached -o results/client2 client2_noisy

# Case 2 — Isolated Clients (concurrent, isolated cgroups)
sudo ./benchmark -v --drop-once --cgroup-config cgroup_isolated.ini -m cached -o results/cached_isolated dual

# Cases 3–5 — Shared pool (edit cgroup_shared.ini for Cases 4–5)
sudo ./benchmark -v --drop-once --cgroup-config cgroup_shared.ini -m cached -o results/cached_dual dual
```

For Cases 4–5, change a single `memory.max` line (keep the parent 2G pool):

```ini
# Case 4 — cap only the victim
[client1_steady]
memory.max = 1G

# Case 5 — cap only the aggressor
[client2_noisy]
memory.max = 1G
```

Use `-o <dir>` per case so `benchmark_analysis.py` can compare them side by side.

### Analyze Results
```bash
./benchmark_analysis.py results/cached_dual
```

Per-second tables (p99 vs io.stat / io.pressure / `memory.high`):

```bash
./report_io_utilization.py results/cached_dual
./report_latency_timeseries.py results/cached_dual
```

## 1 Hz timeseries

The harness records **1 Hz timeseries** by default (Linux + cgroups): per-tenant
latency percentiles and cgroup memory, PSI, and `io.stat`. The file layout is in
**[summary.md — 1 Hz timeseries](summary.md#14-1-hz-timeseries)**.

## Upstream Senpai

`--senpai-high` starts [upstream Senpai](https://github.com/facebookincubator/senpai) (`senpai_high.py`) after phase 0. It adjusts only Tenant B's `memory.high` from memory PSI. It does not move Tenant A's `memory.low`.

```bash
sudo ./benchmark -v --drop-once --senpai-high --cgroup-config cgroup_shared.ini -m cached -o results/senpai_high dual
```

Stdout goes to `results/senpai_high/senpai/senpai_client2_noisy_cached.log`. Client2's 1 Hz timeseries still samples `memory.high`. `--senpai-high` requires `--cgroup-config`.

## 📈 Understanding Results

### What to look for

The primary signal is **Tenant A's read latency** (especially **p99**), compared
across conditions and B variants:

- **A alone** → flat p99, near cache-hit latency after warm.
- **A + B (`read` / `randread`)** → p99 and/or IOPS hurt during B's burst phase
  from eviction / device contention (Mechanism 1).
- **A + B (`randwrite`)** → p99 rises **further** at the same eviction rate
  (Mechanism 2 — writeback queue).
- **A + B under `memory.min` / `memory.low`** → partial recovery for reader B;
  residual elevation for writer B = "existing tools insufficient."

With the default quiet → burst → quiet → quiet B schedule, expect damage
concentrated in **phase 1**, with phases 2 and 3 showing recovery if eviction
was the main effect.

### Working-set refault deltas

`benchmark_analysis.py` reports per-phase `workingset_refault_file_delta` from
`memstat/`: file-backed pages re-faulted from disk — a direct measure of cache
churn between tenants.

## 🔍 Key Metrics

- **avg / p50 / p99 / p999 read latency (μs)** — from fio `clat_ns.mean` and
  `clat_ns.percentile`. *p99 is the objective; avg/p50 give shape.*
- **`workingset_refault_file_delta`** — pages re-faulted per phase per cgroup
  (× 4 KiB ≈ bytes re-read).
- **`pgfault_delta` / `pgmajfault_delta`** — cgroup `memory.stat` fault deltas.
- **`memory.current`** — per-cgroup residency before/after each phase.
- **PSI (`some`, memory + io)** — per-cgroup stall **duration**; from `psi/`.
  Primary pressure metric.
- **PSI entry counts (default; `--no-bpftrace` to skip)** — debug-only counts of
  `psi_memstall_enter` and sched_switch-with-`in_iowait`, plus per-logical-read
  ratios in `psi_entries/`. **Not** cache miss rates and **not** stall duration.
- **Memory + IO utilization** — per phase: `memory.stat` `file` (page cache
  held), `target_file_resident_bytes` (mincore residency of the tenant's own
  test file), and `io.stat` `Δrios` / duration (completed device reads).
  `file` counts every page charged to the cgroup, harness files included, so
  the `target` column is the one to compare against `memory.low` / `memory.min`;
  the difference is measurement overhead, not working set.
- **iostat** read/write latency & queue depth — writeback contention.
- **IOPS / BW** — secondary throughput context.
- `/proc/vmstat` `nr_dirty` / `nr_writeback` and per-cgroup `file_dirty` at 1s.
- **1 Hz timeseries** (`timeseries/`, default on) — client1/client2 p50/p95/p99/p99.9 +
  IOPS; per-tenant `memory.current`, `file`, `target_file_resident_bytes`, refaults, PSI, **io.stat `rios`/`wios`/`rbytes`/`wbytes`**;
  client2 `memory.high` sampled once a second.

## ⚙️ Configuration

Edit `fairness_configs.ini` to modify per-phase parameters:
- `runtime` (seconds), `block_size`, `numjobs`, `iodepth`, `rate_iops`
- `pattern` — `randread`, `read`, `randwrite` / `write`, `randrw`
- `io_size` — optional fixed byte budget (omit / comment for `--time_based`)
- `file_size` — choose **relative to `memory.max`**
- `random_distribution`, `fdatasync`, `fsync`

Multi-phase configuration (matches checked-in defaults):

```ini
[client1_steady]
description = Victim: 512M randread at 50k IOPS for 60s on every phase
file_size = 512M

phase_0_pattern = randread
phase_0_block_size = 4k
phase_0_rate_iops = 50000
phase_0_iodepth = 8
phase_0_numjobs = 1
phase_0_runtime = 60
phase_0_ioengine = libaio

phase_1_pattern = randread
phase_1_block_size = 4k
phase_1_rate_iops = 50000
phase_1_iodepth = 8
phase_1_numjobs = 1
phase_1_runtime = 60
phase_1_ioengine = libaio

phase_2_pattern = randread
phase_2_block_size = 4k
phase_2_rate_iops = 50000
phase_2_iodepth = 8
phase_2_numjobs = 1
phase_2_runtime = 60
phase_2_ioengine = libaio

phase_3_pattern = randread
phase_3_block_size = 4k
phase_3_rate_iops = 50000
phase_3_iodepth = 8
phase_3_numjobs = 1
phase_3_runtime = 60
phase_3_ioengine = libaio

[client2_noisy]
description = Aggressor: 32G sequential read, 1k -> 73k x4 -> 1k -> 1k over 60s phases
file_size = 32G

phase_0_pattern = read
phase_0_block_size = 4k
phase_0_rate_iops = 1024
phase_0_iodepth = 8
phase_0_numjobs = 1
phase_0_runtime = 60
phase_0_ioengine = libaio

phase_1_pattern = read
phase_1_block_size = 4k
phase_1_rate_iops = 73000
phase_1_iodepth = 8
phase_1_numjobs = 4
phase_1_runtime = 60
phase_1_ioengine = libaio

phase_2_pattern = read
phase_2_block_size = 4k
phase_2_rate_iops = 1024
phase_2_iodepth = 8
phase_2_numjobs = 1
phase_2_runtime = 60
phase_2_ioengine = libaio

phase_3_pattern = read
phase_3_block_size = 4k
phase_3_rate_iops = 1024
phase_3_iodepth = 8
phase_3_numjobs = 1
phase_3_runtime = 60
phase_3_ioengine = libaio
```

> **Reminder:** run writer-B pairings with `-m cached`. In `direct` mode fio
> bypasses the page cache, so B produces no dirty pages and Mechanism 2 vanishes.
>
> **`rate_iops` is per fio job.** With `numjobs=8` and `rate_iops=100000`, the
> aggregate cap is up to ~800k IOPS (device permitting).

## 📋 Test Results

Results are saved under the `-o` directory (e.g. `results/cached_dual/`):
- **JSON**: `<client>_<mode>_pN.json` — raw fio output per phase (`clat_ns`)
- **Summary**: `summary.txt` — run summary
- **iostat**: `iostat/run_<mode>.iostat` (+ `run_<mode>.start_ts`)
- **PSI**: `psi/*.csv` — per-cgroup memory + io pressure time series
  (`ts, wall_time, mem_some_total_us, io_some_total_us`); analysis reports
  run-long and per-phase duration deltas
- **PSI entries**: `psi_entries/run_<mode>.csv` — cumulative
  memstall / iowait entry counts (default; skip with `--no-bpftrace`)
- **dirty**: `dirty/vmstat_<mode>.csv`, `dirty/<cg>_<mode>_dirty.csv`
- **memstat**: `memstat/<client>_<mode>.csv` — before/after each phase with
  `workingset_refault_file_delta`, `memory_current_*`, fault deltas,
  `file_bytes`, `target_file_resident_bytes`
- **timeseries**: `timeseries/client{1,2}_*_latency_<mode>.csv` (1 Hz p50–p99.9
  + IOPS), `timeseries/client{1,2}_*.csv` (cgroup memory + PSI + io.stat;
  client2 also has `memory.high`),
  `timeseries/per_second_io_utilization.csv` (from `report_io_utilization.py`)
- **clat**: `clat/` — fio 1 s clat histograms for client1 + client2 (for timeseries).
  Histograms, not per-IO samples: fio holds every sample in RAM and flushes it to
  a log at job end, both charged to the tenant's cgroup, which inflated
  `memory.stat:file` by hundreds of MiB inside the memory domain under test.

## 🛠 Troubleshooting

### Permission Issues
```bash
# sudo is required for cache clearing (drop_caches) and cgroup setup
sudo ./benchmark -v --drop-once --cgroup-config cgroup_shared.ini -m cached dual
```

### Disk Space / empty fio JSON
```bash
df -h .
# ENOSPC → empty *_pN.json and analysis errors like:
#   Expecting value: line 1 column 1 (char 0)
```

### Dependencies
```bash
# Check if tools are installed
which fio gcc make iostat

# Build the benchmark
make
```

### cgroup pre-flight check
```bash
# Validate cgroup v2 before running experiments (Linux only)
sudo ./check_cgroups.sh --cgroup-config cgroup_shared.ini
```

### Inspect live cgroups during a run
```bash
# shared layout
cat /sys/fs/cgroup/clients/memory.max
cat /sys/fs/cgroup/clients/client1_steady/memory.current
cat /sys/fs/cgroup/clients/client2_noisy/memory.current
cat /sys/fs/cgroup/clients/client1_steady/cgroup.procs
```

## 🎯 What This Benchmark Aims to Show

1. A latency-sensitive reader's **p99 spikes** (and/or IOPS collapses) when it
   shares the page cache with a noisy neighbor — even at equal `io.weight`.
2. A **buffered writer** neighbor inflates that p99 **more** than a clean
   scanner at the same eviction rate — the dirty-writeback component.
3. Standard cgroup memory isolation (`memory.min` / `memory.low`) **partially**
   recovers the reader-neighbor case but can leave **residual** p99 elevation
   for the writer case — motivating cross-layer (memory ↔ I/O) coordination.

## 🏗️ Build and Development

### Building from Source
```bash
# Clone the repository
git clone <repository-url>
cd PageCache-Fairness

# Install dependencies (see prerequisites above)
brew install fio gcc make  # macOS
# OR
sudo apt-get install fio gcc make sysstat # Linux

# Build the benchmark
make
sudo ./check_cgroups.sh --cgroup-config cgroup_shared.ini
sudo ./benchmark -v --drop-once --cgroup-config cgroup_shared.ini -m cached -o results/cached_dual dual
./benchmark_analysis.py results/cached_dual
```

### Available Make Targets
- `make` / `make all`: Build the benchmark
- `make clean`: Remove build artifacts
- `make setup-files`: Optional dense `test_file_<size>` helpers
- `make test`: Run `client1_steady` baseline
- `make run-all`: Run every client section (`./benchmark all`)
- `make analyze`: Analyze `benchmark_results/`
- `make workflow`: Build, setup-files, then `./benchmark -m cached dual` and analyze. That target does not pass `--drop-once` or `--cgroup-config`; use the experiment commands above on a VM.

## 🔧 Advanced Usage

### Custom Configuration
Dual mode requires sections named `client1_steady` (A) and `client2_noisy` (B).
Example Mechanism-2 aggressor:

```ini
[client2_noisy]
description = Custom B: buffered writer neighbor
file_size = 32G
phase_0_pattern = randwrite
phase_0_block_size = 4k
phase_0_rate_iops = 20000
phase_0_numjobs = 4
phase_0_iodepth = 8
phase_0_runtime = 60
phase_0_ioengine = libaio
```

> **Platform note:** cgroups, PSI, and `memory.stat` require **Linux (cgroup v2)**.
> macOS is build-only.

### Command Line Options
```bash
sudo ./benchmark --help
sudo ./benchmark -v --drop-once --cgroup-config cgroup_shared.ini -m cached -o results/cached_dual dual
sudo ./benchmark -v --drop-once --senpai-high --cgroup-config cgroup_shared.ini -m cached -o results/senpai_high dual
sudo ./benchmark -c fairness_configs.ini -o results/ client1_steady
```

`--senpai-high` runs `senpai_high.py` on Client2 `memory.high` only. Knobs:
`--senpai-script`, `--senpai-client`, `--senpai-min-size`, `--senpai-max-size`,
`--senpai-interval`, `--senpai-pressure`. Timeseries: `--no-timeseries`,
`--no-clat-log` (`--clat-log` writes 1 s histograms, not per-IO samples). See
[summary.md — 1 Hz timeseries](summary.md#14-1-hz-timeseries).

## Repository Status

The harness is **implemented and runnable on Linux (cgroup v2)**.

### Layout

| Path | Role |
|------|------|
| `benchmark.c` | Harness: INI parsing, cgroup setup, fio orchestration, telemetry |
| `Makefile` | `make` → `./benchmark` (`gcc -std=c11`) |
| `fairness_configs.ini` | Victim + noisy-neighbor phase definitions |
| `cgroup_isolated.ini` | Per-tenant cgroups (`memory.max`, `memory.min`, `io.weight`) |
| `cgroup_shared.ini` | Shared 2G parent pool; nested tenant cgroups |
| `check_cgroups.sh` | Pre-flight cgroup v2 validation |
| `benchmark_analysis.py` | Summarize avg/p50/p99/p999, refaults, dirty, PSI, phase IO util |
| `report_latency_timeseries.py` | Per-second c1 p99 vs c2 memory.high (+ phase) |
| `report_io_utilization.py` | Per-second p99 / rios / IO PSI / rbytes throughput CSV |
| `results/` | Per-run output (`-o` overrides) |

### Harness

- **CLI:** `--config`, `--cgroup-config`, `-o`, `-m cached|direct|both`, `--no-cgroup`, `--no-psi`, `--no-bpftrace`, `--drop-once`, `--senpai-high`, `--no-timeseries`, `--no-clat-log`, `-v`
- **Modes:** `dual` (`client1_steady` + `client2_noisy`), single section name, `all`
- **Cache:** `drop_caches()` before every cached phase by default; `--drop-once` drops only before phase 0. Cached mode also passes `--invalidate=0` so fio does not discard the warm set at job start.
- **Test files:** `test_file_<client>_<file_size>` created lazily by the harness
- **Concurrency:** all clients in a phase forked together; phases run sequentially; **new fio process + JSON per client per phase**
- **fio output:** `<client>_<mode>_pN.json` with `clat_ns` percentiles

### cgroups

- Create tree under `/sys/fs/cgroup`, enable `memory` + `io`
- Apply `memory.max`, `memory.low`, `memory.min`, `io.weight` from cgroup INI
- Attach fio via `cgroup.procs` before exec
- Per-phase `memory.stat` / `memory.current` snapshots in `memstat/`

### Telemetry (1 Hz during each mode run)

- **iostat:** `iostat -dx 1` → `iostat/run_<mode>.iostat`
- **PSI:** `psi/<cgroup>_<mode>.csv`
- **PSI entries (debug):** `psi_entries/run_<mode>.csv` (+ `probe_*.bt`, `cgroup_ids_*.txt`)
- **vmstat:** `dirty/vmstat_<mode>.csv`
- **Per-cgroup dirty:** `dirty/<cgroup>_<mode>_dirty.csv`
- **timeseries (default):** `timeseries/` — client1/client2 latency percentiles + cgroup
  memory/PSI/io.stat (client2 includes a 1 Hz `memory.high` sample)
- **clat (default for client1+client2):** `clat/` — fio `write_hist_log` (`log_hist_msec=1000`) for 1 Hz p99 aggregation
- **senpai-high (optional):** `--senpai-high` runs `senpai_high.py` → `senpai/senpai_<client>_<mode>.log`

Requires `--cgroup-config` for per-tenant memstat/PSI/dirty.

### Workloads (`fairness_configs.ini`)

| Section | Role |
|---------|------|
| `client1_steady` | Tenant A — 512M `randread` on every phase; victim in `dual` |
| `client2_noisy` | Tenant B — 32G sequential `read`, quiet→burst→quiet (Mechanism 1) |

Phase keys: `pattern`, `block_size`, `iodepth`, `numjobs`, `rate_iops`,
`runtime`, `ioengine`, `io_size`, `rate_bw`, `rwmixread`,
`random_distribution`, `fdatasync`, `fsync`.

### Quick start

```bash
make
sudo ./check_cgroups.sh --cgroup-config cgroup_shared.ini
sudo ./benchmark -v --drop-once --cgroup-config cgroup_shared.ini -m cached -o results/cached_dual dual
./benchmark_analysis.py results/cached_dual
```

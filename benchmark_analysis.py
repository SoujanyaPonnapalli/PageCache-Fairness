#!/usr/bin/env python3
"""benchmark_analysis.py — summarize page-cache fairness results.

Reads a results directory produced by ./benchmark and reports:
  * Tenant A avg / p50 / p99 / p999 read latency (from fio clat_ns)
  * IOPS / bandwidth (secondary context)
  * workingset_refault_file_delta per phase per cgroup (from memstat/)
  * dirty-page pressure (from dirty/ — [TODO-3] vmstat + file_dirty samples)
  * per-cgroup memory.current per phase (from memstat/), plus cgroup
    memory.stat page-fault deltas (pgfault_delta / pgmajfault_delta)
  * per-cgroup memory (memory.stat file) + IO (io.stat rios) utilization
    per phase, alongside mincore and cachestat residency of the client's own
    test file (memory.stat file also counts cache created by the harness itself)
  * PSI some-total stall duration + optional bpftrace entry counts
    per phase (one table; not miss rates)

For 1 Hz timeseries / per-second IO util, use the standalone scripts:
  ./report_latency_timeseries.py <results_dir>
  ./report_io_utilization.py <results_dir>

Usage: ./benchmark_analysis.py <results_dir>
"""

import csv
import json
import os
import sys
from glob import glob


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! could not read {path}: {e}")
        return None


def us(ns):
    return ns / 1000.0


def report_latency(results_dir):
    print("\n## \U0001f4c8 READ LATENCY (Tenant A objective)")
    print("=" * 50)
    files = sorted(glob(os.path.join(results_dir, "*.json")))
    if not files:
        print("  (no fio json found)")
        return
    for path in files:
        data = load_json(path)
        if not data or "jobs" not in data:
            continue
        name = os.path.basename(path)
        for job in data["jobs"]:
            clat = job.get("read", {}).get("clat_ns", {})
            pct = clat.get("percentile", {})
            avg = clat.get("mean")
            p50 = pct.get("50.000000")
            p99 = pct.get("99.000000")
            p999 = pct.get("99.900000")
            iops = job.get("read", {}).get("iops", 0.0)
            bw = job.get("read", {}).get("bw", 0.0)  # KiB/s
            line = f"  {name} [{job.get('jobname','?')}]:"
            if avg is not None:
                line += f" avg={us(avg):.1f}us"
            if p50 is not None:
                line += f" p50={us(p50):.1f}us"
            if p99 is not None:
                line += f" p99={us(p99):.1f}us"
            if p999 is not None:
                line += f" p999={us(p999):.1f}us"
            line += f" iops={iops:.0f} bw={bw/1024:.1f}MiB/s"
            print(line)


def report_refaults(results_dir):
    print("\n## \U0001f501 WORKING SET REFAULTS (page-cache eviction churn)")
    print("=" * 50)
    memstat_dir = os.path.join(results_dir, "memstat")
    files = sorted(glob(os.path.join(memstat_dir, "*.csv")))
    if not files:
        print("  (no memstat csv found — Linux/cgroup v2 only)")
        return
    for path in files:
        name = os.path.basename(path).replace(".csv", "")
        print(f"\n**{name}:**")
        with open(path) as f:
            for row in csv.DictReader(f):
                if row.get("when") == "after":
                    def _as_int(key):
                        try:
                            return int(row.get(key, "-1"))
                        except (ValueError, TypeError):
                            return -1
                    d = _as_int("workingset_refault_file_delta")
                    t = _as_int("workingset_refault_file_total")
                    if d >= 0:
                        line = (f"  phase {row['phase']}: "
                                f"delta {d:>12,} pages ({d * 4096 / (1024*1024):8.1f} MiB)")
                        if t >= 0:
                            line += (f"  |  total {t:>12,} pages "
                                     f"({t * 4096 / (1024*1024):8.1f} MiB)")
                        print(line)


def report_dirty(results_dir):
    """[TODO-3] /proc/vmstat + per-cgroup file_dirty time series."""
    print("\n## \U0001f4a7 DIRTY-PAGE PRESSURE (vmstat + per-cgroup file_dirty)")
    print("=" * 50)
    dirty_dir = os.path.join(results_dir, "dirty")
    files = sorted(glob(os.path.join(dirty_dir, "*.csv")))
    if not files:
        print("  (no dirty csv found — Linux only)")
        return
    for path in files:
        name = os.path.basename(path).replace(".csv", "")
        peaks = {}
        with open(path) as f:
            reader = csv.DictReader(f)
            fields = [c for c in (reader.fieldnames or []) if c != "ts"]
            for row in reader:
                for c in fields:
                    try:
                        v = int(row[c])
                    except (ValueError, TypeError):
                        continue
                    peaks[c] = max(peaks.get(c, 0), v)
        summary = "  ".join(f"{c} peak={v:,}" for c, v in peaks.items())
        print(f"  {name}: {summary}")


def _as_nonneg_int(row, key):
    try:
        v = int(row.get(key, "-1"))
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _psi_samples(rows):
    """[(wall_time, mem_us, io_us), ...] from a psi csv.

    Prefers the wall_time column written by current benchmark.c. Older CSVs
    only have relative ts (0,1,2,…) — those cannot be phase-aligned.
    """
    samples = []
    for row in rows:
        wall = _as_nonneg_int(row, "wall_time")
        if wall is None:
            continue
        mem = _as_nonneg_int(row, "mem_some_total_us")
        io = _as_nonneg_int(row, "io_some_total_us")
        samples.append((wall, mem if mem is not None else -1, io if io is not None else -1))
    samples.sort()
    return samples


def _psi_value_at(samples, t, which):
    """Cumulative PSI total at last sample with wall_time <= t. which: 1=mem, 2=io."""
    best = None
    for s in samples:
        if s[0] <= t:
            best = s
        else:
            break
    if best is None:
        return None
    v = best[which]
    return v if v >= 0 else None


def _psi_phase_delta(samples, before_ts, after_ts, which):
    if not samples:
        return None
    v0 = _psi_value_at(samples, before_ts, which)
    v1 = _psi_value_at(samples, after_ts, which)
    if v0 is None:
        v0 = 0
    if v1 is None:
        return None
    return max(0, v1 - v0)


def _memstat_for_psi(results_dir, psi_name):
    """Map psi/<cgroup_label>_<mode>.csv to memstat/<client>_<mode>.csv.

    Shared-layout PSI files are named clients_client1_steady_cached; memstat
    uses the client section name client1_steady_cached.
    """
    memstat_dir = os.path.join(results_dir, "memstat")
    exact = os.path.join(memstat_dir, psi_name + ".csv")
    if os.path.exists(exact):
        return exact
    for mpath in sorted(glob(os.path.join(memstat_dir, "*.csv"))):
        mname = os.path.basename(mpath).replace(".csv", "")
        if psi_name.endswith(mname) or mname.endswith(psi_name):
            return mpath
    return None


def _fmt_psi_ms(us):
    return f"{us / 1000.0:.1f}ms" if us is not None and us >= 0 else "n/a"


def _fmt_count(v):
    return f"{v:,}" if v is not None and v >= 0 else "n/a"


def _split_client_mode(stem):
    if stem.endswith("_cached"):
        return stem[:-7], "cached"
    if stem.endswith("_direct"):
        return stem[:-7], "direct"
    return stem, ""


def _load_psi_duration_by_stem(results_dir):
    """Map memstat stem (client_mode) → PSI duration samples."""
    out = {}
    for path in sorted(glob(os.path.join(results_dir, "psi", "*.csv"))):
        name = os.path.basename(path).replace(".csv", "")
        with open(path) as f:
            rows = list(csv.DictReader(f))
        samples = _psi_samples(rows)
        mpath = _memstat_for_psi(results_dir, name)
        key = os.path.basename(mpath).replace(".csv", "") if mpath else name
        out[key] = samples
    return out


def _load_psi_entry_samples(csv_path):
    """Parse psi_entries/run_<mode>.csv → {client: [(ts, memstall, iowait), ...]}."""
    by_client = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            try:
                ts = int(row["ts"])
                client = row["client"]
                mem = int(row["memstall_entries"])
                io = int(row["iowait_entries"])
            except (KeyError, ValueError, TypeError):
                continue
            by_client.setdefault(client, []).append((ts, mem, io))
    for client in by_client:
        by_client[client].sort()
    return by_client


def _load_all_psi_entries(results_dir):
    """{(client, mode): [(ts, memstall, iowait), ...]} from psi_entries/run_*.csv."""
    out = {}
    for csv_path in sorted(glob(os.path.join(results_dir, "psi_entries", "run_*.csv"))):
        mode = os.path.basename(csv_path).replace("run_", "").replace(".csv", "")
        for client, samples in _load_psi_entry_samples(csv_path).items():
            out[(client, mode)] = samples
    return out


def _counter_at(samples, t, which):
    """Cumulative counter at sample nearest to wall time t (last sample <= t).

    which: 1 = memstall, 2 = iowait. Returns None if no sample at or before t.
    """
    best = None
    for s in samples:
        if s[0] <= t:
            best = s
        else:
            break
    if best is None:
        return None
    return best[which]


def _phase_entry_delta(samples, before_ts, after_ts, which):
    """Delta of cumulative entry counter over [before_ts, after_ts]."""
    if not samples:
        return None
    v0 = _counter_at(samples, before_ts, which)
    v1 = _counter_at(samples, after_ts, which)
    # If phase starts before first sample, treat baseline as 0
    if v0 is None:
        v0 = 0
    if v1 is None:
        return None
    return max(0, v1 - v0)


def _fio_logical_reads(results_dir, client, mode, phase):
    """Get the total number of logical reads for a client and mode and phase."""
    data = load_json(os.path.join(results_dir, f"{client}_{mode}_p{phase}.json"))
    if not data or "jobs" not in data:
        return 0
    total = 0
    for job in data["jobs"]:
        total += job.get("read", {}).get("total_ios", 0)
        total += job.get("write", {}).get("total_ios", 0)
    return total


def report_psi(results_dir):
    """One table: PSI some-total stall *duration* + optional bpftrace entry counts.

    memory / io     = memory.pressure / io.pressure some-total delta (ms)
    Memstall        = psi_memstall_enter hits (debug; not miss rate)
    I/O-wait        = sched_switch with in_iowait (debug; not miss rate)
    """
    print("\n## \U0001f6a6 PSI METRICS (duration + entry counts)")
    print("=" * 50)
    print("  memory / io: cgroup memory.pressure / io.pressure some-total delta.")
    print("  Memstall: kprobe on psi_memstall_enter (per cgroup).")
    print("  I/O-wait: sched_switch where outgoing task had in_iowait.")
    print("  Entry counts are debug signals only — not stall duration or misses.")

    memstat_files = sorted(glob(os.path.join(results_dir, "memstat", "*.csv")))
    if not memstat_files:
        print("  (no memstat csv — cannot bucket PSI into phases)")
        return

    duration_by_stem = _load_psi_duration_by_stem(results_dir)
    entries_by_client = _load_all_psi_entries(results_dir)

    print()
    hdr = (f"  {'Client':<22} {'Ph':>3} {'memory':>10} {'io':>10} "
           f"{'Memstall':>10} {'I/O-wait':>10} {'Total Reads':>12}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    any_row = False
    for mpath in memstat_files:
        stem = os.path.basename(mpath).replace(".csv", "")
        client, mode = _split_client_mode(stem)
        windows = _phase_windows(mpath)
        if not windows:
            continue
        dur = duration_by_stem.get(stem, [])
        ent = entries_by_client.get((client, mode), [])
        for ph in sorted(windows):
            before_ts, after_ts = windows[ph]
            mem_d = _psi_phase_delta(dur, before_ts, after_ts, 1) if dur else None
            io_d = _psi_phase_delta(dur, before_ts, after_ts, 2) if dur else None
            mem_e = _phase_entry_delta(ent, before_ts, after_ts, 1) if ent else None
            iow_e = _phase_entry_delta(ent, before_ts, after_ts, 2) if ent else None
            logical = _fio_logical_reads(results_dir, client, mode, ph)
            reads = f"{logical:,}" if logical > 0 else "n/a"
            print(f"  {stem:<22} {ph:>3} {_fmt_psi_ms(mem_d):>10} {_fmt_psi_ms(io_d):>10} "
                  f"{_fmt_count(mem_e):>10} {_fmt_count(iow_e):>10} {reads:>12}")
            any_row = True

    if not any_row:
        print("  (no phase-aligned rows — check memstat wall_time vs psi / psi_entries ts)")
    else:
        print()
        print("  memory/io measure stall *duration*. Memstall/I/O-wait count how often")
        print("  stall-accounting regions were entered (bpftrace). Neither is a cache")
        print("  miss rate. Primary PSI metric is the duration columns.")


def report_memory(results_dir):
    """Per-cgroup memory.current before/after each phase (from memstat/),
    plus cgroup memory.stat page-fault deltas (pgfault_delta / pgmajfault_delta)."""
    print("\n## \U0001f4be MEMORY CONSUMPTION (cgroup memory.current + page faults)")
    print("=" * 50)
    memstat_dir = os.path.join(results_dir, "memstat")
    files = sorted(glob(os.path.join(memstat_dir, "*.csv")))
    if not files:
        print("  (no memstat csv found — Linux/cgroup v2 only)")
        return

    def _as_int(row, key):
        try:
            return int(row.get(key))
        except (TypeError, ValueError):
            return None

    for path in files:
        name = os.path.basename(path).replace(".csv", "")
        print(f"\n**{name}:**")
        with open(path) as f:
            for row in csv.DictReader(f):
                cur_v = _as_int(row, "memory_current_bytes")
                if cur_v is None or cur_v < 0:
                    continue
                mib = cur_v / (1024 * 1024)
                line = f"  phase {row['phase']} ({row.get('when')}): {mib:9.1f} MiB"

                # page-fault deltas over the phase (only on 'after' rows)
                if row.get("when") == "after":
                    pgf = _as_int(row, "pgfault_delta")
                    pgm = _as_int(row, "pgmajfault_delta")
                    pf_parts = []
                    if pgf is not None and pgf >= 0:
                        pf_parts.append(f"pgfault_delta={pgf:,}")
                    if pgm is not None and pgm >= 0:
                        pf_parts.append(f"pgmajfault_delta={pgm:,}")
                    if pf_parts:
                        line += "  |  " + "  ".join(pf_parts)

                print(line)


def _phase_windows(memstat_path):
    """Return {phase: (before_wall_time, after_wall_time)} from a memstat csv."""
    tmp = {}
    with open(memstat_path) as f:
        for row in csv.DictReader(f):
            try:
                ph = int(row["phase"])
                wt = int(row["wall_time"])
            except (KeyError, ValueError, TypeError):
                continue
            tmp.setdefault(ph, {})[row.get("when")] = wt
    return {ph: (d["before"], d["after"]) for ph, d in tmp.items()
            if "before" in d and "after" in d}


def _fmt_mib(nbytes):
    if nbytes is None:
        return "n/a"
    return f"{nbytes / (1024 * 1024):,.1f}"


def _fmt_delta_mib(nbytes):
    if nbytes is None:
        return "n/a"
    sign = "+" if nbytes > 0 else ""
    return f"{sign}{nbytes / (1024 * 1024):,.1f}"


def _fmt_num(v, decimals=1):
    if v is None:
        return "n/a"
    if decimals == 0:
        return f"{v:,.0f}"
    return f"{v:,.{decimals}f}"


def _csv_int(row, key, *, allow_neg=False):
    """Parse an int CSV field; treat missing / harness sentinel (-1) as None."""
    if key not in row:
        return None
    try:
        v = int(row[key])
    except (TypeError, ValueError):
        return None
    if v == -1 and not allow_neg:
        return None
    if not allow_neg and v < 0:
        return None
    return v


def report_mem_io_util(results_dir):
    """Per-phase page-cache residency + device read IO utilization.

    file     = memory.stat file at phase end (page-cache bytes)
    Δfile    = file_bytes_delta over the phase
    target   = mincore residency of the client's own test file at phase end
    nr_cache = cachestat(2) nr_cache residency of the same file (Linux 6.5+)
    rios/s   = rios_delta / duration (completed device reads)
    Δrios    = io.stat rios delta over the phase
    """
    print("\n## \U0001f4ca MEMORY + IO UTILIZATION (per phase)")
    print("=" * 50)
    print("  file  = memory.stat file at phase end (all page cache charged).")
    print("  Δfile = change in file over the phase.")
    print("  target = mincore residency of the test file only; file - target is")
    print("           harness overhead (fio logs/json), not workload cache.")
    print("  nr_cache = cachestat(2) residency of the same file (Linux 6.5+).")
    print("  rios/s = Δrios / duration (cgroup io.stat completed reads).")

    memstat_dir = os.path.join(results_dir, "memstat")
    memstat_files = sorted(glob(os.path.join(memstat_dir, "*.csv")))
    if not memstat_files:
        print("  (no memstat csv found — needs --cgroup-config)")
        return

    hdr = (f"  {'Client':<22} {'Ph':>3} {'file MiB':>10} {'Δfile MiB':>10} "
           f"{'target MiB':>11} {'nr_cache MiB':>12} {'overhd MiB':>11} "
           f"{'rios/s':>12} {'Δrios':>12} {'dur s':>6}")
    print()
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    any_row = False
    for mpath in memstat_files:
        client_mode = os.path.basename(mpath).replace(".csv", "")
        windows = _phase_windows(mpath)
        if not windows:
            continue

        after = {}
        with open(mpath) as f:
            for row in csv.DictReader(f):
                if row.get("when") != "after":
                    continue
                try:
                    ph = int(row["phase"])
                except (KeyError, ValueError, TypeError):
                    continue
                after[ph] = row

        for ph in sorted(windows):
            before_ts, after_ts = windows[ph]
            duration = max(0, after_ts - before_ts)
            row = after.get(ph)
            if not row:
                continue

            # file_bytes_delta can be negative (cache shrink); -1 is sentinel only
            # when the harness could not compute a delta.
            file_b = _csv_int(row, "file_bytes")
            file_d = _csv_int(row, "file_bytes_delta", allow_neg=True)
            if file_d == -1:
                file_d = None
            rios_d = _csv_int(row, "rios_delta")
            rios_rate = (rios_d / duration) if (rios_d is not None and duration > 0) else None

            target_b = _csv_int(row, "target_file_resident_bytes")
            nr_cache_b = _csv_int(row, "target_file_nr_cache_bytes")
            overhead = (file_b - target_b) if (file_b is not None
                                              and target_b is not None) else None

            if file_b is None and file_d is None and rios_d is None:
                continue

            print(f"  {client_mode:<22} {ph:>3} {_fmt_mib(file_b):>10} "
                  f"{_fmt_delta_mib(file_d):>10} {_fmt_mib(target_b):>11} "
                  f"{_fmt_mib(nr_cache_b):>12} {_fmt_mib(overhead):>11} "
                  f"{_fmt_num(rios_rate):>12} "
                  f"{_fmt_num(rios_d, 0):>12} {duration:>6}")
            any_row = True

    if not any_row:
        print("  (no file/rios columns — re-run benchmark to capture "
              "memory.stat file + io.stat)")



def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    results_dir = sys.argv[1]
    if not os.path.isdir(results_dir):
        print(f"error: {results_dir} is not a directory")
        sys.exit(1)

    print(f"# Page-Cache Fairness Analysis — {results_dir}")
    report_latency(results_dir)
    report_refaults(results_dir)
    report_memory(results_dir)
    report_mem_io_util(results_dir)
    # Mechanism 2 fingerprint (dirty writeback) — keep for later; not primary for
    # current cached-read fairness readout.
    # report_dirty(results_dir)
    report_psi(results_dir)
    print()


if __name__ == "__main__":
    main()

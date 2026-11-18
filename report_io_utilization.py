#!/usr/bin/env python3
"""Join per-second p99, io.stat throughput, io.pressure, and page-cache residency.

Writes a CSV (default: <results>/timeseries/per_second_io_utilization.csv) and
prints the full table.

Usage: python3 report_io_utilization.py <results_dir> [mode] [-o PATH]
  mode defaults to the suffix of client1_steady_latency_*.csv (e.g. cached).

If the benchmark was run with sudo, timeseries/ may be root-owned — use -o to
write elsewhere, or: sudo chown -R $USER results/cached_dual
"""

import argparse
import csv
import os
import sys
from glob import glob

BS = 4096  # fallback block size when rbytes column absent (fio bs=4k)

HEADER = ["wall_time", "phase", "c1_p99_us", "c2_p99_us",
          "c1_mibps", "c2_mibps", "c1_rios", "c2_rios",
          "c1_file_mib", "c2_file_mib",
          "c1_target_mib", "c2_target_mib",
          "c1_nr_cache_mib", "c2_nr_cache_mib",
          "c1_io_us", "c2_io_us"]


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def cumulative_deltas(rows, col):
    out = {}
    prev = None
    for row in rows:
        t = int(row["wall_time"])
        raw = row.get(col)
        if raw is None or raw == "" or raw == "-1":
            out[t] = None
            prev = None
            continue
        val = int(raw)
        out[t] = val - prev if prev is not None else 0
        prev = val
    return out


def levels(rows, col):
    out = {}
    for row in rows:
        t = int(row["wall_time"])
        raw = row.get(col)
        if raw is None or raw == "" or raw == "-1":
            out[t] = None
        else:
            out[t] = int(raw)
    return out


def phase_windows(memstat_path):
    windows = {}
    for row in load_csv(memstat_path):
        try:
            ph = int(row["phase"])
            t = int(row["wall_time"])
            when = row["when"]
        except (KeyError, ValueError, TypeError):
            continue
        b, a = windows.get(ph, (None, None))
        if when == "before":
            b = t
        elif when == "after":
            a = t
        windows[ph] = (b, a)
    return {ph: (b, a) for ph, (b, a) in windows.items()
            if b is not None and a is not None}


def label_phase(t, windows):
    for ph in sorted(windows):
        b, a = windows[ph]
        if b <= t <= a:
            return f"phase_{ph}"
    return "gap"


def ts_mode_from_path(path):
    name = os.path.basename(path)
    if name.endswith(".csv"):
        name = name[:-4]
    for prefix in ("client1_steady_latency_", "client2_noisy_latency_",
                   "client1_steady_", "client2_noisy_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return "unknown"


def bytes_to_mibps(dbytes):
    if dbytes is None:
        return None
    return dbytes / (1024 * 1024)


def to_mib(nbytes):
    if nbytes is None:
        return None
    return nbytes / (1024 * 1024)


def read_mibps(t, rbytes_map, rios_map, has_rbytes):
    if has_rbytes:
        return bytes_to_mibps(rbytes_map.get(t))
    dr = rios_map.get(t)
    return bytes_to_mibps(dr * BS) if dr is not None else None


def csv_cell(v, decimals=None):
    if v is None:
        return ""
    if decimals is None:
        return str(v)
    return f"{v:.{decimals}f}"


def fmt(v, width, nd=None):
    if v is None:
        return f"{'-':>{width}}"
    if nd is not None:
        return f"{v:{width}.{nd}f}"
    return f"{v:>{width}}"


def write_csv(rows_out, out_path):
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for row in rows_out:
            w.writerow([
                row["wall_time"],
                row["phase"],
                csv_cell(row["c1_p99_us"], 1),
                csv_cell(row["c2_p99_us"], 1),
                csv_cell(row["c1_mibps"], 1),
                csv_cell(row["c2_mibps"], 1),
                csv_cell(row["c1_rios"]),
                csv_cell(row["c2_rios"]),
                csv_cell(row["c1_file_mib"], 1),
                csv_cell(row["c2_file_mib"], 1),
                csv_cell(row["c1_target_mib"], 1),
                csv_cell(row["c2_target_mib"], 1),
                csv_cell(row["c1_nr_cache_mib"], 1),
                csv_cell(row["c2_nr_cache_mib"], 1),
                csv_cell(row["c1_io_us"]),
                csv_cell(row["c2_io_us"]),
            ])


def write_csv_with_fallback(rows_out, results_dir, ts_dir, user_path):
    """Return path written, or None if all attempts failed."""
    if user_path:
        try:
            write_csv(rows_out, user_path)
            return user_path
        except OSError as e:
            print(f"warning: could not write {user_path}: {e}", file=sys.stderr)
            return None

    candidates = [
        os.path.join(ts_dir, "per_second_io_utilization.csv"),
        os.path.join(results_dir, "per_second_io_utilization.csv"),
    ]
    last_err = None
    for path in candidates:
        try:
            write_csv(rows_out, path)
            return path
        except OSError as e:
            last_err = e
    print(f"warning: could not write CSV ({last_err})", file=sys.stderr)
    print("  (benchmark output is often root-owned — try -o ~/per_second_io_utilization.csv "
          "or sudo chown -R $USER results/cached_dual)", file=sys.stderr)
    return None


def print_column_legend(has_rbytes):
    tp = "Δrbytes → MiB/s" if has_rbytes else "Δrios × 4 KiB → MiB/s (re-run for rbytes)"
    lines = [
        "Columns:",
        "  wall_time  unix second (aligns timeseries + latency CSVs)",
        "  phase      benchmark phase from memstat before/after, or gap",
        "  c1_p99     client1 (Tenant A) read p99 that second (µs); - = no sample",
        "  c2_p99     client2 (Tenant B) read p99 that second (µs); - = no sample",
        f"  c1_MBps    client1 device read throughput that second ({tp})",
        f"  c2_MBps    client2 device read throughput that second ({tp})",
        "  c1_rios    client1 completed device reads that second (Δ io.stat rios)",
        "  c2_rios    client2 completed device reads that second (Δ io.stat rios)",
        "  c1_file    client1 page cache held (memory.stat file) in MiB",
        "  c2_file    client2 page cache held (memory.stat file) in MiB",
        "  c1_tgt     client1 test-file residency (mincore) in MiB; file minus",
        "             this is harness cache, not working set",
        "  c2_tgt     client2 test-file residency (mincore) in MiB",
        "  c1_nr_cache client1 test-file residency (cachestat nr_cache) in MiB",
        "  c2_nr_cache client2 test-file residency (cachestat nr_cache) in MiB",
        "  c1_io_us   client1 IO stall that second (Δ io.pressure some total, µs)",
        "  c2_io_us   client2 IO stall that second (Δ io.pressure some total, µs)",
    ]
    for line in lines:
        print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", help="benchmark -o directory")
    parser.add_argument("mode", nargs="?", default=None,
                        help="cached/direct (default: infer from timeseries filenames)")
    parser.add_argument("-o", "--output", default=None,
                        help="CSV output path (default: timeseries/per_second_io_utilization.csv)")
    parser.add_argument("--no-write", action="store_true", help="print table only")
    args = parser.parse_args()

    results_dir = args.results_dir
    if not os.path.isdir(results_dir):
        print(f"error: {results_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    ts_dir = os.path.join(results_dir, "timeseries")
    if not os.path.isdir(ts_dir):
        print(f"error: no timeseries/ under {results_dir}", file=sys.stderr)
        sys.exit(1)

    lat1_paths = sorted(glob(os.path.join(ts_dir, "client1_steady_latency_*.csv")))
    lat2_paths = sorted(glob(os.path.join(ts_dir, "client2_noisy_latency_*.csv")))
    c1_paths = [p for p in sorted(glob(os.path.join(ts_dir, "client1_steady_*.csv")))
                if "latency" not in os.path.basename(p)
                and not os.path.basename(p).startswith("per_second")]
    c2_paths = [p for p in sorted(glob(os.path.join(ts_dir, "client2_noisy_*.csv")))
                if "latency" not in os.path.basename(p)
                and not os.path.basename(p).startswith("per_second")]
    if not lat1_paths or not c1_paths:
        print("error: need client1_steady_latency_*.csv and client1_steady_*.csv",
              file=sys.stderr)
        sys.exit(1)

    mode = args.mode or ts_mode_from_path(lat1_paths[0])
    lat1 = next((p for p in lat1_paths if p.endswith(f"_{mode}.csv")), lat1_paths[0])
    lat2 = next((p for p in lat2_paths if p.endswith(f"_{mode}.csv")),
                lat2_paths[0] if lat2_paths else None)
    c1_path = next((p for p in c1_paths if p.endswith(f"_{mode}.csv")), c1_paths[0])
    c2_path = next((p for p in c2_paths if p.endswith(f"_{mode}.csv")),
                   c2_paths[0] if c2_paths else None)

    c1_rows = load_csv(c1_path)
    c2_rows = load_csv(c2_path) if c2_path else []

    if "rios" not in c1_rows[0]:
        print("error: no rios in timeseries — re-run benchmark for 1 Hz io.stat",
              file=sys.stderr)
        sys.exit(1)

    has_rbytes = "rbytes" in c1_rows[0]

    c1_p99 = {int(r["wall_time"]): float(r["p99_us"]) for r in load_csv(lat1)}
    c2_p99 = {}
    if lat2:
        c2_p99 = {int(r["wall_time"]): float(r["p99_us"]) for r in load_csv(lat2)}

    c1_rbytes = cumulative_deltas(c1_rows, "rbytes") if has_rbytes else {}
    c2_rbytes = cumulative_deltas(c2_rows, "rbytes") if (has_rbytes and c2_rows) else {}
    c1_rios = cumulative_deltas(c1_rows, "rios")
    c2_rios = cumulative_deltas(c2_rows, "rios") if c2_rows else {}
    c1_io = cumulative_deltas(c1_rows, "io_psi_some_total_us")
    c2_io = cumulative_deltas(c2_rows, "io_psi_some_total_us") if c2_rows else {}
    c1_file = levels(c1_rows, "file_bytes")
    c2_file = levels(c2_rows, "file_bytes") if c2_rows else {}
    # target-file residency isolates workload cache from harness/log cache
    has_target = "target_file_resident_bytes" in c1_rows[0]
    c1_target = levels(c1_rows, "target_file_resident_bytes") if has_target else {}
    c2_target = (levels(c2_rows, "target_file_resident_bytes")
                 if (has_target and c2_rows) else {})
    has_nr_cache = "target_file_nr_cache_bytes" in c1_rows[0]
    c1_nr_cache = (levels(c1_rows, "target_file_nr_cache_bytes")
                   if has_nr_cache else {})
    c2_nr_cache = (levels(c2_rows, "target_file_nr_cache_bytes")
                   if (has_nr_cache and c2_rows) else {})

    memstat = os.path.join(results_dir, "memstat", f"client1_steady_{mode}.csv")
    windows = phase_windows(memstat) if os.path.isfile(memstat) else {}

    keys = sorted(set(c1_rios) | set(c2_rios) | set(c1_rbytes) | set(c2_rbytes))

    rows_out = []
    for t in keys:
        phase_s = label_phase(t, windows) if windows else "gap"
        rows_out.append({
            "wall_time": t,
            "phase": phase_s,
            "c1_p99_us": c1_p99.get(t),
            "c2_p99_us": c2_p99.get(t),
            "c1_mibps": read_mibps(t, c1_rbytes, c1_rios, has_rbytes),
            "c2_mibps": read_mibps(t, c2_rbytes, c2_rios, has_rbytes),
            "c1_rios": c1_rios.get(t),
            "c2_rios": c2_rios.get(t),
            "c1_file_mib": to_mib(c1_file.get(t)),
            "c2_file_mib": to_mib(c2_file.get(t)),
            "c1_target_mib": to_mib(c1_target.get(t)),
            "c2_target_mib": to_mib(c2_target.get(t)),
            "c1_nr_cache_mib": to_mib(c1_nr_cache.get(t)),
            "c2_nr_cache_mib": to_mib(c2_nr_cache.get(t)),
            "c1_io_us": c1_io.get(t),
            "c2_io_us": c2_io.get(t),
        })

    out_path = None
    if not args.no_write:
        out_path = write_csv_with_fallback(rows_out, results_dir, ts_dir, args.output)

    src = "rbytes" if has_rbytes else "rios×4k (re-run benchmark for rbytes)"
    print(f"# PER-SECOND IO UTILIZATION — {results_dir}")
    print(f"# throughput from {src}")
    if out_path:
        print(f"# wrote {out_path} ({len(rows_out)} rows)")
    elif not args.no_write:
        print(f"# ({len(rows_out)} rows — CSV not written; see warning above)")
    print()
    print_column_legend(has_rbytes)
    print()
    print(f"{'wall_time':>12} {'phase':>10} {'c1_p99':>10} {'c2_p99':>10} "
          f"{'c1_MBps':>8} {'c2_MBps':>8} "
          f"{'c1_rios':>8} {'c2_rios':>8} "
          f"{'c1_file':>8} {'c2_file':>8} "
          f"{'c1_tgt':>8} {'c2_tgt':>8} "
          f"{'c1_nc':>8} {'c2_nc':>8} "
          f"{'c1_io_us':>10} {'c2_io_us':>10}")
    for row in rows_out:
        t = row["wall_time"]
        print(f"{t:12d} {row['phase']:>10} "
              f"{fmt(row['c1_p99_us'], 10, 1)} "
              f"{fmt(row['c2_p99_us'], 10, 1)} "
              f"{fmt(row['c1_mibps'], 8, 1)} "
              f"{fmt(row['c2_mibps'], 8, 1)} "
              f"{fmt(row['c1_rios'], 8)} "
              f"{fmt(row['c2_rios'], 8)} "
              f"{fmt(row['c1_file_mib'], 8, 1)} "
              f"{fmt(row['c2_file_mib'], 8, 1)} "
              f"{fmt(row['c1_target_mib'], 8, 1)} "
              f"{fmt(row['c2_target_mib'], 8, 1)} "
              f"{fmt(row['c1_nr_cache_mib'], 8, 1)} "
              f"{fmt(row['c2_nr_cache_mib'], 8, 1)} "
              f"{fmt(row['c1_io_us'], 10)} "
              f"{fmt(row['c2_io_us'], 10)}")


if __name__ == "__main__":
    main()

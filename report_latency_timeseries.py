#!/usr/bin/env python3
"""Per-second Client1 p99 vs Client2 memory.high, labeled by benchmark phase.

Writes <results>/timeseries/per_second_latency_timeseries.csv and prints the
full table. memory.high comes from the 1 Hz client2_noisy samples.

Usage: python3 report_latency_timeseries.py <results_dir> [mode] [-o PATH]

If the benchmark was run with sudo, use -o ~/per_second_latency_timeseries.csv
or: sudo chown -R $USER results/cached_dual
"""

import argparse
import csv
import os
import sys
from glob import glob


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


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
    return "cached"


def fmt_bytes(raw):
    if raw is None or raw == "" or raw == "-1":
        return "-"
    v = int(raw)
    if v < 0:
        return "-"
    if v >= 1 << 30:
        return f"{v / (1 << 30):.2f}G"
    if v >= 1 << 20:
        return f"{v / (1 << 20):.0f}M"
    if v >= 1 << 10:
        return f"{v / (1 << 10):.0f}K"
    return str(v)


def fmt_p99(v, width=12):
    if v is None:
        return f"{'-':>{width}}"
    return f"{v:{width}.1f}"


def memory_high_by_second(ts_dir, mode, keys):
    """Return {wall_time: memory_high_bytes or None} for each key in keys."""
    sampled = {}
    c2_path = os.path.join(ts_dir, f"client2_noisy_{mode}.csv")
    if os.path.isfile(c2_path):
        for row in load_csv(c2_path):
            t = int(row["wall_time"])
            sampled[t] = row.get("memory_high_bytes")
    return {t: sampled.get(t) for t in keys}


def csv_cell_p99(v):
    return "" if v is None else f"{v:.1f}"


def csv_cell_mhigh(raw):
    if raw is None or raw == "" or raw == "-1":
        return ""
    return str(int(raw))


def write_csv(rows, out_path):
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_time", "phase", "c1_p99_us", "c2_memory_high_bytes"])
        for row in rows:
            w.writerow([
                row["wall_time"],
                row["phase"],
                csv_cell_p99(row["c1_p99_us"]),
                csv_cell_mhigh(row["c2_memory_high_bytes"]),
            ])


def write_csv_with_fallback(rows, results_dir, ts_dir, user_path):
    if user_path:
        try:
            write_csv(rows, user_path)
            return user_path
        except OSError as e:
            print(f"warning: could not write {user_path}: {e}", file=sys.stderr)
            return None

    candidates = [
        os.path.join(ts_dir, "per_second_latency_timeseries.csv"),
        os.path.join(results_dir, "per_second_latency_timeseries.csv"),
    ]
    last_err = None
    for path in candidates:
        try:
            write_csv(rows, path)
            return path
        except OSError as e:
            last_err = e
    print(f"warning: could not write CSV ({last_err})", file=sys.stderr)
    print("  (try -o ~/per_second_latency_timeseries.csv or "
          "sudo chown -R $USER results/cached_dual)", file=sys.stderr)
    return None


def print_column_legend():
    lines = [
        "Columns:",
        "  wall_time       unix second (aligns timeseries + latency CSVs)",
        "  phase           benchmark phase from memstat before/after, or gap",
        "  c1_p99_us       client1 (Tenant A) read p99 that second (µs); - = no sample",
        "  c2_mem.high     client2 memory.high (1 Hz client2_noisy timeseries)",
    ]
    for line in lines:
        print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_dir", help="benchmark -o directory")
    parser.add_argument("mode", nargs="?", default=None,
                        help="cached/direct (default: infer from timeseries filenames)")
    parser.add_argument("-o", "--output", default=None, help="CSV output path")
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
    if not lat1_paths:
        print("error: need client1_steady_latency_*.csv", file=sys.stderr)
        sys.exit(1)

    mode = args.mode or ts_mode_from_path(lat1_paths[0])
    lat1 = next((p for p in lat1_paths if p.endswith(f"_{mode}.csv")), lat1_paths[0])

    c1_p99 = {int(r["wall_time"]): float(r["p99_us"]) for r in load_csv(lat1)}
    keys = sorted(c1_p99)

    memstat = os.path.join(results_dir, "memstat", f"client1_steady_{mode}.csv")
    windows = phase_windows(memstat) if os.path.isfile(memstat) else {}

    mhigh = memory_high_by_second(ts_dir, mode, keys)

    rows_out = []
    for t in keys:
        rows_out.append({
            "wall_time": t,
            "phase": label_phase(t, windows) if windows else "gap",
            "c1_p99_us": c1_p99.get(t),
            "c2_memory_high_bytes": mhigh.get(t),
        })

    out_path = None
    if not args.no_write:
        out_path = write_csv_with_fallback(rows_out, results_dir, ts_dir, args.output)

    print(f"# PER-SECOND LATENCY vs MEMORY.HIGH — {results_dir}")
    if out_path:
        print(f"# wrote {out_path} ({len(rows_out)} rows)")
    elif not args.no_write:
        print(f"# ({len(rows_out)} rows — CSV not written; see warning above)")
    print()
    print_column_legend()
    print()
    print(f"{'wall_time':>12} {'phase':>10} {'c1_p99_us':>12} {'c2_mem.high':>12}")
    for row in rows_out:
        t = row["wall_time"]
        print(f"{t:12d} {row['phase']:>10} "
              f"{fmt_p99(row['c1_p99_us'])} "
              f"{fmt_bytes(row['c2_memory_high_bytes']):>12}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Experimental two-tenant cgroup-v2 fair-share controller.

C1: persistent, refault-driven memory.low protection.
C2: elastic memory.high, grown from observed refault/cache growth or a
    bounded probe when memory.events.local:high rises.

No workload file size, configured IOPS, block size or live benchmark samples
are used for policy decisions. Harness flags are accepted for compatibility
but ignored where noted. This is a research prototype, not an SLO guarantee.

Use --dry-run before enabling writes to a dedicated test cgroup hierarchy.
"""
from __future__ import annotations

import argparse
import csv
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
PAGE = os.sysconf('SC_PAGE_SIZE')


def bound(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def aligned(value: float) -> int:
    return max(0, int(value) // PAGE * PAGE)


def read_number(directory: Path, name: str, *, allow_max: bool = False) -> int | None:
    raw = (directory / name).read_text().strip()
    if raw == 'max' and allow_max:
        return None
    return int(raw)


def read_fields(directory: Path, name: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in (directory / name).read_text().splitlines():
        key, value, *_ = row.split()
        result[key] = int(value)
    return result


def read_psi_some(directory: Path) -> int:
    for line in (directory / 'memory.pressure').read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == 'some':
            return int(next(part.split('=', 1)[1] for part in parts[1:]
                            if part.startswith('total=')))
    raise ValueError(f'missing some total in {directory}/memory.pressure')


def read_rbytes(directory: Path) -> int:
    total = 0
    for line in (directory / 'io.stat').read_text().splitlines():
        for part in line.split()[1:]:
            if part.startswith('rbytes='):
                total += int(part.split('=', 1)[1])
    return total


def counter_delta(current: int, previous: int) -> int:
    # A decrease means that the cgroup/counter may have been reset.
    # Do not misinterpret a reset as a burst of activity.
    return max(0, current - previous)


@dataclass(frozen=True)
class Sample:
    stamp: float
    usage: int
    file_cache: int
    refault_pages: int
    reclaimed_pages: int
    high_events: int
    memory_psi_us: int
    read_bytes: int


def sample_tenant(path: Path) -> Sample:
    st = read_fields(path, 'memory.stat')
    ev = read_fields(path, 'memory.events.local')
    return Sample(
        stamp=time.monotonic(),
        usage=read_number(path, 'memory.current'),
        file_cache=st['file'],
        refault_pages=st['workingset_refault_file'],
        reclaimed_pages=st['pgsteal'],
        high_events=ev['high'],
        memory_psi_us=read_psi_some(path),
        read_bytes=read_rbytes(path),
    )


@dataclass(frozen=True)
class Interval:
    usage_delta: int
    cache_delta: int
    refault_bytes: int
    reclaimed_bytes: int
    high_events: int
    psi_us: int
    physical_read_bytes: int


def interval(current: Sample, previous: Sample) -> Interval:
    return Interval(
        usage_delta=current.usage - previous.usage,
        cache_delta=current.file_cache - previous.file_cache,
        refault_bytes=counter_delta(current.refault_pages, previous.refault_pages) * PAGE,
        reclaimed_bytes=counter_delta(current.reclaimed_pages, previous.reclaimed_pages) * PAGE,
        high_events=counter_delta(current.high_events, previous.high_events),
        psi_us=counter_delta(current.memory_psi_us, previous.memory_psi_us),
        physical_read_bytes=counter_delta(current.read_bytes, previous.read_bytes),
    )


def read_limit(path: Path, name: str, default_if_max: int) -> int:
    value = read_number(path, name, allow_max=True)
    return default_if_max if value is None else value


def apply_limit(path: Path, name: str, value: int, dry_run: bool):
    if not dry_run:
        (path / name).write_text(str(aligned(value)))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('cgpath', help='C2 noisy cgroup path (existing harness positional argument)')
    p.add_argument('--victim-cgpath', required=True, help='C1 protected cgroup path')
    p.add_argument('--parent-cgpath', required=True, help='Shared parent cgroup')
    p.add_argument('--controller-csv', default=None)
    p.add_argument('--control-interval', type=float, default=1.0)
    p.add_argument('--gain-up', type=float, default=1.0, help='C1 low increment/refault byte; C2 growth gain')
    p.add_argument('--gain-down', type=float, default=0.1, help='Fraction of unused allocation released after quiet samples')
    p.add_argument('--quiet-intervals', type=int, default=3)
    p.add_argument('--psi-quiet-fraction', type=float, default=0.01,
                   help='Max some-stall time fraction for a quiet interval')
    p.add_argument('--reserve-headroom-mib', type=float, default=32.0)
    p.add_argument('--probe-step-mib', type=float, default=16.0,
                   help='Bounded experimental C2 high increase for high-limit events alone')
    p.add_argument('--low-min-size', type=int, default=0)
    p.add_argument('--low-max-size', type=int, default=2 << 30)
    p.add_argument('--min-size', type=int, default=0, help='Minimum C2 high in bytes')
    p.add_argument('--max-size', type=int, default=2 << 30)
    p.add_argument('--read-amplification', type=float, default=1.0,
                   help='Diagnostic Delta-inspired minimum only; no workload config')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--max-iterations', type=int, default=0,
                   help='Optional bounded run for testing (0 = continuous)')
    # Legacy CLI options: accepted only so benchmark.c can launch this script.
    # They are intentionally NEVER used to calculate memory allocations.
    for option in ('--victim-p99', '--victim-nr-cache', '--noisy-nr-cache',
                   '--victim-file', '--noisy-file', '--victim-latency-csv',
                   '--noisy-latency-csv'):
        p.add_argument(option, default=None, help=argparse.SUPPRESS)
    for option in ('--victim-file-size', '--noisy-file-size',
                   '--victim-block-bytes', '--noisy-block-bytes',
                   '--low-up', '--low-down', '--coeff-probe', '--coeff-backoff',
                   '--cache-drop-bytes', '--cache-growth-bytes'):
        p.add_argument(option, type=int, default=None, help=argparse.SUPPRESS)
    for option in ('--retention-low', '--retention-healthy', '--retention-drop',
                   '--noisy-parent-cache-threshold', '--parent-util-threshold',
                   '--p99-target', '--sample-max-age', '--max-probe', '--max-backoff',
                   '--delta-seconds', '--read-bandwidth-mib', '--ema-alpha'):
        p.add_argument(option, type=float, default=None, help=argparse.SUPPRESS)
    for option in ('--interval', '--pressure', '--low-interval', '--ramp-up-clients'):
        p.add_argument(option, type=int, default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if (a.control_interval <= 0 or a.gain_up < 0 or not 0 <= a.gain_down <= 1
            or a.quiet_intervals < 1 or not 0 <= a.psi_quiet_fraction <= 1
            or a.reserve_headroom_mib < 0 or a.probe_step_mib < 0
            or a.read_amplification <= 0 or a.low_min_size < 0 or a.min_size < 0
            or a.low_max_size < a.low_min_size or a.max_size < a.min_size
            or a.max_iterations < 0):
        p.error('invalid controller parameters')
    return a


def decide(a, *, capacity, reserve, parent_usage, c1, c2, d1, d2,
           protected, old_high, quiet1, quiet2, dt):
    """Pure controller policy: returns low, high, state and diagnostic fields."""
    fair = capacity / 2.0
    quiet_c1 = d1.refault_bytes == 0 and d1.psi_us / (dt * 1e6) <= a.psi_quiet_fraction
    quiet_c2 = (d2.refault_bytes == 0 and d2.high_events == 0
                and d2.psi_us / (dt * 1e6) <= a.psi_quiet_fraction)
    quiet1 = quiet1 + 1 if quiet_c1 else 0
    quiet2 = quiet2 + 1 if quiet_c2 else 0

    # Refaults are feedback, NOT unique missing pages; gain-up is tunable.
    if d1.refault_bytes > 0:
        protected += a.gain_up * d1.refault_bytes
        reason1 = 'raise_refault'
    elif quiet1 >= a.quiet_intervals and c1.usage < protected:
        protected -= a.gain_down * (protected - c1.usage)
        reason1 = 'release_unused'
    else:
        reason1 = 'hold'

    protected = bound(protected, a.low_min_size,
                      min(a.low_max_size, fair, max(0, capacity-reserve)))
    new_low = aligned(protected)
    # A conservative planning ceiling that respects BOTH currently charged
    # C1 memory and protection necessary for its cache to recover.
    ceiling = aligned(max(0, min(a.max_size,
                   capacity - max(c1.usage, new_low) - reserve)))
    # If the ceiling is below actual C2 usage, applying it can force reclaim;
    # this is intentional during victim recovery, but may cause temporary
    # high-limit events. Never write an invalid or negative limit.
    high_target = old_high
    growth = 0.0
    can_expand = (parent_usage < capacity-reserve and c2.usage < ceiling)
    demand = d2.high_events > 0 or d2.refault_bytes > 0 or d2.cache_delta > 0
    if demand and can_expand:
        growth = a.gain_up * max(d2.refault_bytes, max(0, d2.cache_delta))
        if growth == 0 and d2.high_events > 0:
            growth = a.probe_step_mib*MIB
        high_target = max(old_high, c2.usage) + growth
        reason2 = 'expand' if growth else 'hold_no_growth'
    elif not demand and quiet2 >= a.quiet_intervals:
        high_target = old_high - a.gain_down*max(0, old_high-c2.usage)
        reason2 = 'release_unused'
    else:
        reason2 = 'hold'
    # Ceiling always takes priority. min_size is advisory if there is
    # insufficient available capacity for it.
    high_target = aligned(bound(high_target, min(a.min_size, ceiling), ceiling))
    read_bandwidth = (d1.physical_read_bytes+d2.physical_read_bytes)/dt
    min_reservation = max(0, fair - a.control_interval*read_bandwidth/a.read_amplification)
    return new_low, high_target, protected, quiet1, quiet2, {
        'fair_share': fair, 'min_reservation': min_reservation,
        'c2_high_ceiling': ceiling, 'growth_budget': growth,
        'c2_active_demand': int(demand), 'c1_reason': reason1,
        'c2_reason': reason2,
    }


COLUMNS = [
    'wall_time','dt_s','parent_current','parent_max','parent_headroom',
    'fair_share','diagnostic_min_reservation',
    'c1_usage','c1_usage_delta','c1_file_cache','c1_cache_delta',
    'c1_refault_bytes','c1_reclaimed_bytes','c1_psi_us', 'c1_rbytes_delta',
    'c2_usage','c2_usage_delta','c2_file_cache','c2_cache_delta',
    'c2_refault_bytes','c2_reclaimed_bytes','c2_psi_us','c2_rbytes_delta',
    'c2_high_events_delta','c2_active_demand',
    'protected_target','c1_low_before','c1_low_target','c1_low_after',
    'c2_high_before','c2_high_ceiling','c2_growth_budget',
    'c2_high_target','c2_high_after', 'c1_reason','c2_reason','action',
]


def main(argv=None):
    a = parse_args(argv)
    parent = Path(a.parent_cgpath)
    c1_path, c2_path = Path(a.victim_cgpath), Path(a.cgpath)
    cap = read_number(parent, 'memory.max', allow_max=True)
    if cap is None or cap <= 0:
        raise SystemExit('Parent memory.max must be a finite positive value')
    reserve = int(a.reserve_headroom_mib*MIB)
    if reserve >= cap:
        raise SystemExit('Headroom cannot exceed parent memory.max')
    # Must sample before changing any limit; first tick is just a baseline.
    prev1, prev2 = sample_tenant(c1_path), sample_tenant(c2_path)
    prev_time = time.monotonic()
    fair = cap/2
    protected = float(bound(prev1.usage, a.low_min_size,
                       min(a.low_max_size, fair, cap-reserve)))
    quiet1 = quiet2 = 0
    out = None
    if a.controller_csv:
        target = Path(a.controller_csv)
        target.parent.mkdir(parents=True, exist_ok=True)
        was_empty = not target.exists() or target.stat().st_size == 0
        out = target.open('a', newline='', buffering=1)
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        if was_empty:
            writer.writeheader()
    else:
        writer = None
    print(f'Kernel-driven Senpai armed: cap={cap/MIB:.1f} MiB, '
          f'fair={fair/MIB:.1f} MiB, c1_initial={protected/MIB:.1f} MiB, '
          f'dry_run={a.dry_run}', flush=True)
    n = 0
    try:
        while True:
            time.sleep(a.control_interval)
            try:
                cur1, cur2 = sample_tenant(c1_path), sample_tenant(c2_path)
                parent_usage = read_number(parent, 'memory.current')
                old_low = read_limit(c1_path, 'memory.low', 0)
                old_high = read_limit(c2_path, 'memory.high', cap)
            except (OSError, ValueError, KeyError, StopIteration) as e:
                print(f'Hold: incomplete kernel sample: {e}', file=sys.stderr, flush=True)
                # Rebaseline on the next successful sample to avoid stale deltas.
                prev1 = prev2 = None
                continue
            dt = max(1e-6, time.monotonic()-prev_time)
            prev_time = time.monotonic()
            if prev1 is None or prev2 is None:
                prev1, prev2 = cur1, cur2
                continue
            d1, d2 = interval(cur1, prev1), interval(cur2, prev2)
            prev1, prev2 = cur1, cur2
            low, high, protected, quiet1, quiet2, info = decide(
                a, capacity=cap, reserve=reserve, parent_usage=parent_usage,
                c1=cur1, c2=cur2, d1=d1, d2=d2, protected=protected,
                old_high=old_high, quiet1=quiet1, quiet2=quiet2, dt=dt)
            actions = []
            if low != aligned(old_low):
                apply_limit(c1_path, 'memory.low', low, a.dry_run)
                actions.append('set_c1_low')
            if high != aligned(old_high):
                apply_limit(c2_path, 'memory.high', high, a.dry_run)
                actions.append('set_c2_high')
            actual_low = read_limit(c1_path, 'memory.low', 0)
            actual_high = read_limit(c2_path, 'memory.high', cap)
            if a.dry_run:
                actions.insert(0, 'dry_run')
            row = dict(
                wall_time=int(time.time()), dt_s=dt, parent_current=parent_usage,
                parent_max=cap, parent_headroom=max(0,cap-parent_usage),
                fair_share=info['fair_share'],
                diagnostic_min_reservation=info['min_reservation'],
                c1_usage=cur1.usage, c1_usage_delta=d1.usage_delta,
                c1_file_cache=cur1.file_cache, c1_cache_delta=d1.cache_delta,
                c1_refault_bytes=d1.refault_bytes,
                c1_reclaimed_bytes=d1.reclaimed_bytes, c1_psi_us=d1.psi_us,
                c1_rbytes_delta=d1.physical_read_bytes,
                c2_usage=cur2.usage, c2_usage_delta=d2.usage_delta,
                c2_file_cache=cur2.file_cache, c2_cache_delta=d2.cache_delta,
                c2_refault_bytes=d2.refault_bytes,
                c2_reclaimed_bytes=d2.reclaimed_bytes, c2_psi_us=d2.psi_us,
                c2_rbytes_delta=d2.physical_read_bytes,
                c2_high_events_delta=d2.high_events,
                c2_active_demand=info['c2_active_demand'],
                protected_target=aligned(protected),
                c1_low_before=old_low, c1_low_target=low, c1_low_after=actual_low,
                c2_high_before=old_high, c2_high_ceiling=info['c2_high_ceiling'],
                c2_growth_budget=info['growth_budget'], c2_high_target=high,
                c2_high_after=actual_high, c1_reason=info['c1_reason'],
                c2_reason=info['c2_reason'], action='+'.join(actions) or 'hold')
            if writer:
                writer.writerow(row)
            print(f'C1 usage={cur1.usage/MIB:.0f} MiB low={low/MIB:.0f} MiB '
                  f'refault={d1.refault_bytes/MIB:.1f} MiB; '
                  f'C2 usage={cur2.usage/MIB:.0f} MiB high={high/MIB:.0f} MiB '
                  f'ceiling={info["c2_high_ceiling"]/MIB:.0f} MiB '
                  f'high_events={d2.high_events}: {row["action"]}', flush=True)
            n += 1
            if a.max_iterations and n >= a.max_iterations:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()


if __name__ == '__main__':
    main()

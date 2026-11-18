#!/usr/bin/env python3
"""Sample-driven cgroup-v2 memory controller with dynamic victim identification.

At each tick, clients with memory.current < capacity/number_of_clients are
classified as victims; the others are bursty. This is a *heuristic role*, not a
proof of latency sensitivity. Victims receive refault-proportional memory.low
protection; bursty clients can borrow remaining space through memory.high.

No benchmark file size, IOPS, or block size is used. The two-tenant harness
interface is supported, and additional clients can be passed with
--client-cgpath. Run --dry-run on a dedicated cgroup hierarchy first.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
PAGE = os.sysconf('SC_PAGE_SIZE')


def clamp(value, lo, hi):
    return max(lo, min(value, hi))


def aligned(value):
    return max(0, int(value) // PAGE * PAGE)


def number(path: Path, name: str, *, allow_max=False):
    raw = (path / name).read_text().strip()
    return None if allow_max and raw == 'max' else int(raw)


def fields(path: Path, name: str):
    return {parts[0]: int(parts[1]) for row in (path / name).read_text().splitlines()
            if len(parts := row.split()) >= 2}


def psi_some(path: Path):
    for row in (path / 'memory.pressure').read_text().splitlines():
        parts = row.split()
        if parts and parts[0] == 'some':
            return int(next(x[6:] for x in parts[1:] if x.startswith('total=')))
    raise ValueError(f'PSI some total missing for {path}')


def read_bytes(path: Path):
    return sum(int(part[7:]) for row in (path / 'io.stat').read_text().splitlines()
               for part in row.split()[1:] if part.startswith('rbytes='))


def delta_counter(current, previous):
    return max(0, current - previous)


@dataclass(frozen=True)
class Sample:
    usage: int
    file_cache: int
    refault_pages: int
    pgsteal_pages: int
    high_events: int
    psi_us: int
    rbytes: int


def sample(path: Path):
    st, ev = fields(path, 'memory.stat'), fields(path, 'memory.events.local')
    return Sample(number(path, 'memory.current'), st['file'],
                  st['workingset_refault_file'], st['pgsteal'], ev['high'],
                  psi_some(path), read_bytes(path))


@dataclass(frozen=True)
class Change:
    usage_delta: int
    cache_delta: int
    refault_bytes: int
    pgsteal_bytes: int
    high_delta: int
    psi_delta_us: int
    rbytes_delta: int


def changes(current: Sample, old: Sample):
    return Change(current.usage - old.usage,
                  current.file_cache - old.file_cache,
                  delta_counter(current.refault_pages, old.refault_pages) * PAGE,
                  delta_counter(current.pgsteal_pages, old.pgsteal_pages) * PAGE,
                  delta_counter(current.high_events, old.high_events),
                  delta_counter(current.psi_us, old.psi_us),
                  delta_counter(current.rbytes, old.rbytes))


@dataclass
class Tenant:
    path: Path
    sample: Sample
    protection: float
    quiet: int = 0
    role: str = 'unknown'


def decide(args, capacity, parent_usage, samples, diffs, protections,
           quiets, old_highs, dt):
    """Pure policy, returning (lows, highs, protections, quiets, details).

    Victim criterion is based solely on observed usage relative to fair share.
    Victim budget uses ACTUAL victim usage, not memory.low. High ceilings
    are planning targets: memory.high is soft and can be exceeded.
    """
    names = list(samples)
    n = len(names)
    fair = capacity / n
    reserve = int(args.reserve_headroom_mib * MIB)
    budget = max(0, capacity - reserve)
    victims = [k for k in names if samples[k].usage < fair]
    bursty = [k for k in names if k not in victims]
    victim_usage = sum(samples[k].usage for k in victims)

    # C1-style protection now applies to every dynamically detected victim,
    # and can grow beyond its fair share based on observed refaults.
    next_p, next_q, reasons = {}, {}, {}
    for k in names:
        s, d = samples[k], diffs[k]
        quiet_now = (d.refault_bytes == 0 and
                     d.psi_delta_us / max(dt * 1_000_000, 1) <= args.psi_quiet_fraction)
        q = quiets[k] + 1 if quiet_now else 0
        p = protections[k]
        if d.refault_bytes > 0:
            p += args.gain_up * d.refault_bytes
            why = 'raise_refault'
        elif q >= args.quiet_intervals and s.usage < p:
            p -= args.gain_down * (p - s.usage)
            why = 'release_unused'
        else:
            why = 'hold'
        # No min(protected, fair) clamp; only global capacity bounds.
        next_p[k] = clamp(p, args.low_min_size, min(args.low_max_size, budget))
        next_q[k] = q
        reasons[k] = why

    # Avoid aggregate memory.low overcommit. Prioritize current victims; the
    # remaining budget may protect bursty tenants too, but does not guarantee
    # each could simultaneously demand all of its desired protection.
    lows = {}
    remaining = budget
    for k in victims + bursty:
        grant = aligned(min(next_p[k], remaining))
        lows[k] = grant
        remaining = max(0, remaining - grant)

    # For each bursty tenant, subtract all victim ACTUAL usage. With multiple
    # bursty tenants, account for other bursty tenants' current footprints too
    # to avoid giving every bursty client an identical full shared allowance.
    ceilings = {}
    for k in names:
        others_bursty = sum(samples[j].usage for j in bursty if j != k)
        ceilings[k] = aligned(max(0, budget - victim_usage - others_bursty))

    highs, growths, high_reasons = {}, {}, {}
    for k in names:
        s, d, old_high = samples[k], diffs[k], old_highs[k]
        if k in victims:
            # Victims should not be throttled by an outdated low high-limit;
            # aim for parent capacity. The sum of actual usage is still
            # bounded by parent memory.max, not by these soft thresholds.
            highs[k] = aligned(min(args.max_size, budget))
            growths[k], high_reasons[k] = 0, 'victim_unthrottle'
            continue

        ceiling = min(ceilings[k], args.max_size)
        active = d.high_delta > 0 or d.refault_bytes > 0 or d.cache_delta > 0
        usable = parent_usage < budget and s.usage < ceiling
        growth = 0
        candidate = old_high
        if active and usable:
            growth = args.gain_up * max(d.refault_bytes, max(0, d.cache_delta))
            if growth == 0 and d.high_delta:
                growth = args.probe_step_mib * MIB
            candidate = max(old_high, s.usage) + growth
            why = 'expand' if growth else 'hold_no_growth'
        elif not active and next_q[k] >= args.quiet_intervals:
            candidate = old_high - args.gain_down * max(0, old_high - s.usage)
            why = 'release_unused'
        else:
            why = 'hold'
        highs[k] = aligned(clamp(candidate, min(args.min_size, ceiling), ceiling))
        growths[k], high_reasons[k] = growth, why

    details = dict(fair_share=fair, victims=victims, bursty=bursty,
                   victim_usage=victim_usage, ceilings=ceilings,
                   growths=growths, low_reasons=reasons,
                   high_reasons=high_reasons)
    return lows, highs, next_p, next_q, details


def cli(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('cgpath', help='Legacy harness second client path')
    p.add_argument('--victim-cgpath', required=True,
                   help='Legacy harness first client path; role is dynamically inferred')
    p.add_argument('--client-cgpath', action='append', default=[],
                   help='Additional client path; repeat for more tenants')
    p.add_argument('--parent-cgpath', required=True)
    p.add_argument('--controller-csv')
    p.add_argument('--control-interval', type=float, default=1.0)
    p.add_argument('--gain-up', type=float, default=1.0)
    p.add_argument('--gain-down', type=float, default=0.1)
    p.add_argument('--quiet-intervals', type=int, default=3)
    p.add_argument('--psi-quiet-fraction', type=float, default=0.01)
    p.add_argument('--reserve-headroom-mib', type=float, default=32)
    p.add_argument('--probe-step-mib', type=float, default=16)
    p.add_argument('--low-min-size', type=int, default=0)
    p.add_argument('--low-max-size', type=int, default=2 << 30)
    p.add_argument('--min-size', type=int, default=0)
    p.add_argument('--max-size', type=int, default=2 << 30)
    p.add_argument('--read-amplification', type=float, default=1.0,
                   help='Diagnostic placeholder only, not used in policy')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--max-iterations', type=int, default=0)
    # Legacy benchmark flags are accepted but never used for allocation.
    for flag in ('--victim-p99', '--victim-nr-cache', '--noisy-nr-cache',
                 '--victim-file', '--noisy-file', '--victim-latency-csv',
                 '--noisy-latency-csv'):
        p.add_argument(flag, default=None, help=argparse.SUPPRESS)
    for flag in ('--victim-file-size', '--noisy-file-size',
                 '--victim-block-bytes', '--noisy-block-bytes',
                 '--low-up', '--low-down', '--coeff-probe', '--coeff-backoff',
                 '--cache-drop-bytes', '--cache-growth-bytes'):
        p.add_argument(flag, type=int, default=None, help=argparse.SUPPRESS)
    for flag in ('--retention-low', '--retention-healthy', '--retention-drop',
                 '--noisy-parent-cache-threshold', '--parent-util-threshold',
                 '--p99-target', '--sample-max-age', '--max-probe', '--max-backoff',
                 '--delta-seconds', '--read-bandwidth-mib', '--ema-alpha'):
        p.add_argument(flag, type=float, default=None, help=argparse.SUPPRESS)
    for flag in ('--interval', '--pressure', '--low-interval', '--ramp-up-clients'):
        p.add_argument(flag, type=int, default=None, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if (a.control_interval <= 0 or a.gain_up < 0 or not 0 <= a.gain_down <= 1
            or a.quiet_intervals < 1 or not 0 <= a.psi_quiet_fraction <= 1
            or a.reserve_headroom_mib < 0 or a.probe_step_mib < 0
            or a.low_max_size < a.low_min_size or a.max_size < a.min_size
            or a.max_iterations < 0):
        p.error('invalid controller parameters')
    return a


def main(argv=None):
    a = cli(argv)
    parent = Path(a.parent_cgpath)
    paths = [Path(a.victim_cgpath), Path(a.cgpath)] + list(map(Path, a.client_cgpath))
    if len({str(x.resolve()) for x in paths}) != len(paths):
        raise SystemExit('Duplicate client paths')
    cap = number(parent, 'memory.max', allow_max=True)
    if cap is None or cap <= 0:
        raise SystemExit('Finite parent memory.max required')
    if int(a.reserve_headroom_mib * MIB) >= cap:
        raise SystemExit('Reserved headroom exceeds parent cap')
    names = [p.name for p in paths]
    if len(set(names)) != len(names):
        raise SystemExit('Client cgroup basenames must be unique')
    path_of = dict(zip(names, paths))
    old_samples = {k: sample(p) for k, p in path_of.items()}
    fair = cap / len(names)
    protections = {k: float(clamp(s.usage, a.low_min_size,
                                 min(a.low_max_size, cap)))
                   for k, s in old_samples.items()}
    quiets = {k: 0 for k in names}
    old_t = time.monotonic()
    log = None
    writer = None
    if a.controller_csv:
        f = Path(a.controller_csv)
        f.parent.mkdir(parents=True, exist_ok=True)
        new_file = not f.exists() or f.stat().st_size == 0
        log = f.open('a', newline='', buffering=1)
        columns = ['time', 'dt', 'tenant', 'role', 'usage', 'usage_delta',
                   'file_cache', 'cache_delta', 'refault_bytes', 'pgsteal_bytes',
                   'high_events_delta', 'psi_delta_us', 'physical_read_bytes',
                   'protection_target', 'low_before', 'low_target', 'low_after',
                   'high_before', 'high_target', 'high_after', 'high_ceiling',
                   'victim_usage_sum', 'fair_share', 'growth_budget',
                   'low_reason', 'high_reason', 'dry_run']
        writer = csv.DictWriter(log, fieldnames=columns)
        if new_file:
            writer.writeheader()
    print(f'Dynamic Senpai started: capacity={cap/MIB:.1f}MiB, '
          f'tenants={len(names)}, fair={fair/MIB:.1f}MiB, dry_run={a.dry_run}',
          flush=True)
    ticks = 0
    try:
        while not a.max_iterations or ticks < a.max_iterations:
            time.sleep(a.control_interval)
            try:
                samples = {k: sample(p) for k, p in path_of.items()}
                parent_usage = number(parent, 'memory.current')
                old_lows = {k: number(p, 'memory.low') for k, p in path_of.items()}
                old_highs = {k: (cap if (v := number(p, 'memory.high', allow_max=True)) is None else v)
                             for k, p in path_of.items()}
            except (OSError, KeyError, ValueError, StopIteration) as exc:
                print(f'Sample failed; holding limits: {exc}', file=sys.stderr)
                old_samples = None
                continue
            now = time.monotonic()
            dt = max(now - old_t, 1e-6)
            old_t = now
            if old_samples is None:
                old_samples = samples
                continue
            diffs = {k: changes(samples[k], old_samples[k]) for k in names}
            old_samples = samples
            lows, highs, protections, quiets, details = decide(
                a, cap, parent_usage, samples, diffs, protections, quiets,
                old_highs, dt)
            # Update low boundaries before high thresholds to establish
            # protection before reclaiming from bursty clients.
            for k, p in path_of.items():
                if lows[k] != aligned(old_lows[k]) and not a.dry_run:
                    (p / 'memory.low').write_text(str(lows[k]))
            for k, p in path_of.items():
                if highs[k] != aligned(old_highs[k]) and not a.dry_run:
                    (p / 'memory.high').write_text(str(highs[k]))
            if writer:
                for k in names:
                    s, d = samples[k], diffs[k]
                    writer.writerow(dict(
                        time=time.time(), dt=dt, tenant=k,
                        role='victim' if k in details['victims'] else 'bursty',
                        usage=s.usage, usage_delta=d.usage_delta,
                        file_cache=s.file_cache, cache_delta=d.cache_delta,
                        refault_bytes=d.refault_bytes, pgsteal_bytes=d.pgsteal_bytes,
                        high_events_delta=d.high_delta, psi_delta_us=d.psi_delta_us,
                        physical_read_bytes=d.rbytes_delta,
                        protection_target=aligned(protections[k]),
                        low_before=old_lows[k], low_target=lows[k],
                        low_after=number(path_of[k], 'memory.low'),
                        high_before=old_highs[k], high_target=highs[k],
                        high_after=(cap if (v := number(path_of[k], 'memory.high', allow_max=True)) is None else v),
                        high_ceiling=details['ceilings'][k],
                        victim_usage_sum=details['victim_usage'], fair_share=fair,
                        growth_budget=details['growths'][k],
                        low_reason=details['low_reasons'][k],
                        high_reason=details['high_reasons'][k], dry_run=int(a.dry_run)))
            print(' | '.join(f'{k}: {"victim" if k in details["victims"] else "bursty"}'
                             f' usage={samples[k].usage/MIB:.0f}MiB'
                             f' low={lows[k]/MIB:.0f}MiB high={highs[k]/MIB:.0f}MiB'
                             for k in names), flush=True)
            ticks += 1
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            log.close()


if __name__ == '__main__':
    main()

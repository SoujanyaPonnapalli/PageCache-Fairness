#!/usr/bin/env python3
#
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.
#
# Customized fork of facebookincubator/senpai for the page-cache fairness
# benchmark. Upstream (memory.high-only) lives in senpai_high.py.
#
# Several latency-sensitive tenants and several noisy neighbors share one parent.
# A single victim and a single noisy neighbor is the original two-tenant case:
# pass one --victim-cgpath and the positional cgpath, and the gate is unchanged.
#
#   Noisy neighbors (positional cgpath, plus repeatable --noisy-cgpath)
#       memory.high from PSI. Upward expansion of a neighbor is suppressed
#       while that neighbor is displacing some protected tenant.
#
#   Good tenants (repeatable --victim-cgpath)
#       memory.low is raised for a tenant only when all of the following hold:
#         1. shared parent is at/near memory.max (or hit max this tick),
#         2. that tenant's target-file nr_cache retention is low or falling, and
#         3. some noisy neighbor's nr_cache is growing or occupies a large
#            share of the parent memory budget.
#
#       p99 is an application-level harm signal and provides a slack/healthy
#       band for deciding when that tenant's protection can be relaxed.
#
# The benchmark harness should start this script only after phase 0 so neither
# controller moves knobs during warm-up.

import argparse
import csv
import datetime
import os
import time
from dataclasses import dataclass
from typing import Optional

PAGE_SIZE = 4096


def h(x):
    """Translate a number of bytes to a human-readable string."""
    if x is None:
        return "n/a"
    order = 0
    suffix = ['', 'k', 'M', 'G', 'T']
    max_order = len(suffix) - 1
    x = float(x)
    while abs(x) > 1024 and order < max_order:
        x /= 1024.0
        order += 1
    return '%.2f%s' % (x, suffix[order])


def log(string):
    """Log timestamped information to stdout."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(ts + ' ' + string, flush=True)


def pct(x):
    return "n/a" if x is None else f"{100.0 * x:.1f}%"


class Cgroup(object):
    def __init__(self, path, limit_min=0, limit_max=(1 << 63) - 1,
                 manage_high=True, manage_low=False,
                 low_min=0, low_max=None, label=None):
        self.path = path
        self.label = label or os.path.basename(path.rstrip("/")) or path
        self.limit = None
        self.limit_min = limit_min
        self.limit_max = limit_max
        self.low = None
        self.low_min = low_min
        self.low_max = limit_max if low_max is None else low_max
        self.manage_high = manage_high
        self.manage_low = manage_low

        if manage_high:
            self.pressure()  # Ensure PSI is available.
            self.set_limit(None)

        if manage_low:
            # Preserve memory.low from the cgroup config. Do NOT initialize it
            # from memory.current; fairness protection should be added only
            # after the controller observes contention.
            self.low = self.read_low()
            self.low = max(self.low_min, min(self.low_max, self.low))
            self.low &= ~(PAGE_SIZE - 1)
            if self.read_low() != self.low:
                self.write("memory.low", str(self.low))
                log(f'Adjusted {self.label} memory.low to {h(self.low)}')
            else:
                log(f'Preserving {self.label} memory.low={h(self.low)}')

    # PSI format:
    # some avg10=0.00 avg60=0.00 avg300=0.00 total=0
    # full avg10=0.00 avg60=0.00 avg300=0.00 total=0

    def pressure(self):
        return float(self.readlines("memory.pressure")[0].split()[1].split('=')[1])

    def total(self):
        return int(self.readlines("memory.pressure")[0].split()[4].split('=')[1])

    # Client2: PSI -> memory.high

    def scale_limit(self, factor):
        x = self.read_limit()
        x = int(x + x * factor)
        self.set_limit(x)

    def set_limit(self, limit):
        self.import_limit(limit)
        self.write("memory.high", str(self.limit))

    def import_limit(self, limit):
        """Sanitize memory.high candidate and store it in self.limit."""
        # Keep upstream Senpai's resynchronization behavior. On initialization,
        # self.limit is None, so memory.high is reset from memory.current.
        if self.read_limit() != self.limit:
            log(f'Resetting {self.label} memory.high to memory.current.')
            limit = self.read_current()
        limit = max(self.limit_min, min(self.limit_max, limit))
        limit &= ~(PAGE_SIZE - 1)
        self.limit = limit

    # Client1: fairness controller -> memory.low

    def set_low(self, low):
        self.import_low(low)
        if self.read_low() != self.low:
            self.write("memory.low", str(self.low))

    def import_low(self, low):
        """Clamp memory.low without deriving it from memory.current."""
        if low is None:
            low = self.read_low()
        low = max(self.low_min, min(self.low_max, low))
        low &= ~(PAGE_SIZE - 1)
        self.low = low

    def adjust_low_bytes(self, delta):
        """Adjust memory.low by an absolute number of bytes."""
        old = self.read_low()
        new = max(self.low_min, min(self.low_max, old + delta))
        new &= ~(PAGE_SIZE - 1)
        if new != old:
            self.write("memory.low", str(new))
        self.low = new
        return old, new

    # Generic cgroupfs helpers

    def read_current(self):
        return int(self.read("memory.current").strip())

    def read_max(self):
        x = self.read("memory.max").strip()
        if x == "max":
            return None
        return int(x)

    def read_limit(self):
        x = self.read("memory.high").strip()
        if x == "max":
            return (1 << 64) - 1
        return int(x)

    def read_low(self):
        x = self.read("memory.low").strip()
        if x == "max":
            return (1 << 64) - 1
        return int(x)

    def read_event(self, name, local=True):
        filename = "memory.events.local" if local else "memory.events"
        try:
            for line in self.readlines(filename):
                fields = line.split()
                if len(fields) == 2 and fields[0] == name:
                    return int(fields[1])
        except (OSError, ValueError):
            return 0
        return 0

    def read(self, filename):
        with open(os.path.join(self.path, filename)) as f:
            return f.read()

    def readlines(self, filename):
        with open(os.path.join(self.path, filename)) as f:
            return f.readlines()

    def write(self, filename, string):
        with open(os.path.join(self.path, filename), 'w') as f:
            f.write(string)


def read_live_float(path):
    """Read one-line live file: wall_time,value -> (int, float) or None."""
    if not path:
        return None
    try:
        with open(path) as f:
            wall_s, value_s = f.read().strip().split(",", 1)
        return int(float(wall_s)), float(value_s)
    except (OSError, ValueError):
        return None


def read_live_int(path):
    """Read one-line live file: wall_time,value -> (int, int) or None."""
    if not path:
        return None
    try:
        with open(path) as f:
            wall_s, value_s = f.read().strip().split(",", 1)
        return int(float(wall_s)), int(float(value_s))
    except (OSError, ValueError):
        return None


def sample_is_fresh(sample, max_age):
    if sample is None:
        return False
    wall_time, _ = sample
    return abs(time.time() - wall_time) <= max_age


def resolve_file_size(path, explicit_size):
    if explicit_size is not None:
        return explicit_size
    if not path:
        return None
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def fairness_gate(parent_hot, any_victim_evicting, any_noisy_evidence):
    """Shared-parent scarcity, some good tenant losing cache, some noisy neighbor taking it."""
    return bool(parent_hot and any_victim_evicting and any_noisy_evidence)


def victim_should_raise(parent_hot, this_evicting, any_noisy_evidence):
    """Raise this tenant's memory.low. With one victim this matches fairness_gate."""
    return bool(parent_hot and this_evicting and any_noisy_evidence)


def noisy_should_suppress(parent_hot, any_victim_evicting, this_evidence):
    """Hold this neighbor's memory.high. With one neighbor this matches fairness_gate."""
    return bool(parent_hot and any_victim_evicting and this_evidence)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _align(parser, paths, values, path_flag, value_flag, required):
    values = _as_list(values) if values is not None else []
    if not values:
        if required:
            parser.error(f'{path_flag} requires {value_flag} once per tenant')
        return [None] * len(paths)
    if len(values) != len(paths):
        parser.error(
            f'{value_flag} ({len(values)}) must match {path_flag} ({len(paths)})'
        )
    return values


@dataclass
class VictimSpec:
    cgpath: str
    p99_path: Optional[str]
    nr_cache_path: Optional[str]
    file_path: Optional[str]
    file_size: Optional[int]


@dataclass
class NoisySpec:
    cgpath: str
    nr_cache_path: Optional[str]
    file_path: Optional[str]
    file_size: Optional[int]


@dataclass
class VictimSample:
    name: str
    p99: Optional[float]
    p99_new: bool
    nr_cache: Optional[int]
    nr_cache_new: bool
    nr_cache_delta: Optional[int]
    retention: Optional[float]
    retention_delta: Optional[float]
    parent_cache_ratio: Optional[float]
    retention_low: bool
    retention_healthy: bool
    cache_falling: bool
    p99_bad: bool
    p99_healthy: bool
    p99_slack: bool

    @property
    def evicting(self):
        return self.retention_low or self.cache_falling


@dataclass
class NoisySample:
    name: str
    nr_cache: Optional[int]
    nr_cache_new: bool
    nr_cache_delta: Optional[int]
    retention: Optional[float]
    parent_cache_ratio: Optional[float]
    cache_growing: bool
    cache_dominant: bool
    psi_avg10: float
    psi_total: int
    psi_delta: int
    psi_integral: int

    @property
    def evidence(self):
        return self.cache_growing or self.cache_dominant


@dataclass
class Tick:
    wall_time: int
    parent_current: Optional[int]
    parent_max: Optional[int]
    parent_util: Optional[float]
    parent_max_event_delta: int
    parent_hot: bool
    victims: list
    noisies: list


class VictimCtl(object):
    def __init__(self, spec, conf, label):
        self.name = label
        self.spec = spec
        self.file_size = spec.file_size
        self.low_grace_ticks = conf.low_interval
        self.last_p99_wall = None
        self.last_cache_wall = None
        self.last_nr_cache = None
        self.last_retention = None
        self.cgroup = Cgroup(
            spec.cgpath,
            conf.min_size,
            conf.max_size,
            manage_high=False,
            manage_low=True,
            low_min=conf.low_min_size,
            low_max=conf.low_max_size,
            label=label,
        )


class NoisyCtl(object):
    def __init__(self, spec, conf, label):
        self.name = label
        self.spec = spec
        self.file_size = spec.file_size
        self.grace_ticks = conf.interval
        self.last_cache_wall = None
        self.last_nr_cache = None
        self.cgroup = Cgroup(
            spec.cgpath,
            conf.min_size,
            conf.max_size,
            label=label,
        )
        self.last_total = self.cgroup.total()
        self.integral = 0


class Senpai(object):
    def __init__(self, conf, victim_specs, noisy_specs):
        self.conf = conf
        self.single = len(victim_specs) <= 1 and len(noisy_specs) == 1
        log('Configuration:')
        for key, val in vars(conf).items():
            log(f'  {key} = {val}')

        self.noisies = []
        for i, spec in enumerate(noisy_specs):
            label = "Client2" if len(noisy_specs) == 1 else _tenant_name(spec.cgpath)
            self.noisies.append(NoisyCtl(spec, conf, label))

        self.parent = None
        self.last_parent_max_events = 0
        if conf.parent_cgpath:
            self.parent = Cgroup(
                conf.parent_cgpath,
                manage_high=False,
                manage_low=False,
            )
            self.last_parent_max_events = self.parent.read_event("max")

        self.victims = []
        for spec in victim_specs:
            label = "Client1" if len(victim_specs) == 1 else _tenant_name(spec.cgpath)
            self.victims.append(VictimCtl(spec, conf, label))

        if self.victims:
            log(
                f'{len(self.victims)} good tenant(s), {len(self.noisies)} noisy neighbor(s) '
                f'(p99 target={conf.p99_target:.1f}±{conf.p99_delta:.1f} us, '
                f'parent_hot>={conf.parent_util_threshold:.0%}, '
                f'retention_low<{conf.retention_low:.0%}, '
                f'retention_healthy>={conf.retention_healthy:.0%})'
            )
            for victim in self.victims:
                log(f'{victim.name} target-file size={h(victim.file_size)}')
            for noisy in self.noisies:
                log(f'{noisy.name} target-file size={h(noisy.file_size)}')
        else:
            log('No --victim-cgpath; memory.low control disabled')

        self._init_controller_csv()

    def _init_controller_csv(self):
        if not self.conf.controller_csv:
            return
        parent_dir = os.path.dirname(self.conf.controller_csv)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        if os.path.exists(self.conf.controller_csv) and os.path.getsize(self.conf.controller_csv) > 0:
            return
        with open(self.conf.controller_csv, 'w', newline='') as f:
            csv.writer(f).writerow([
                'wall_time', 'name', 'role',
                'parent_current', 'parent_max', 'parent_util',
                'parent_max_event_delta', 'parent_hot',
                'p99_us', 'p99_bad', 'p99_healthy', 'p99_slack',
                'nr_cache', 'nr_cache_delta', 'retention', 'retention_delta',
                'parent_cache_ratio',
                'retention_low', 'retention_healthy', 'cache_falling',
                'cache_growing', 'cache_dominant',
                'psi_avg10', 'psi_delta', 'psi_integral',
                'fairness_active', 'memory_low', 'memory_high', 'action',
            ])

    def run(self):
        while True:
            time.sleep(1)
            self.tick()

    def tick(self):
        sample = self.sample_state()
        any_victim = any(v.evicting for v in sample.victims)
        any_noisy = any(n.evidence for n in sample.noisies)
        active = fairness_gate(sample.parent_hot, any_victim, any_noisy)
        low_actions = [
            self.adjust_memory_low(sample, ctl, v, active)
            for ctl, v in zip(self.victims, sample.victims)
        ]
        high_actions = [
            self.adjust_memory_high(sample, ctl, n, active)
            for ctl, n in zip(self.noisies, sample.noisies)
        ]
        self.log_snapshot(sample, active, low_actions, high_actions)
        self.write_controller_csv(sample, active, low_actions, high_actions)

    def _sample_parent(self):
        parent_current = None
        parent_max = None
        parent_util = None
        parent_max_event_delta = 0
        parent_hot = False
        if not self.parent:
            return parent_current, parent_max, parent_util, parent_max_event_delta, parent_hot
        try:
            parent_current = self.parent.read_current()
            parent_max = self.parent.read_max()
            if parent_max and parent_max > 0:
                parent_util = parent_current / float(parent_max)
            current_events = self.parent.read_event("max")
            if current_events >= self.last_parent_max_events:
                parent_max_event_delta = current_events - self.last_parent_max_events
            self.last_parent_max_events = current_events
            parent_hot = (
                (parent_util is not None and
                 parent_util >= self.conf.parent_util_threshold)
                or parent_max_event_delta > 0
            )
        except (OSError, ValueError):
            pass
        return parent_current, parent_max, parent_util, parent_max_event_delta, parent_hot

    def _sample_victim(self, victim, parent_max):
        p99_sample = read_live_float(victim.spec.p99_path)
        p99 = None
        p99_new = False
        if sample_is_fresh(p99_sample, self.conf.sample_max_age):
            wall, p99 = p99_sample
            p99_new = wall != victim.last_p99_wall
            if p99_new:
                victim.last_p99_wall = wall

        target = max(self.conf.p99_target, 1e-6)
        bad_ceil = target + max(self.conf.p99_delta, 0.0)
        p99_bad = p99 is not None and p99 > bad_ceil
        p99_healthy = p99 is not None and p99 <= target
        p99_slack = p99 is not None and target < p99 <= bad_ceil

        cache_sample = read_live_int(victim.spec.nr_cache_path)
        nr_cache = None
        nr_cache_new = False
        nr_cache_delta = None
        retention = None
        retention_delta = None
        parent_cache_ratio = None
        if sample_is_fresh(cache_sample, self.conf.sample_max_age):
            wall, nr_cache = cache_sample
            nr_cache_new = wall != victim.last_cache_wall
            if nr_cache_new:
                if victim.last_nr_cache is not None:
                    nr_cache_delta = nr_cache - victim.last_nr_cache
                victim.last_nr_cache = nr_cache
                victim.last_cache_wall = wall
            if victim.file_size and victim.file_size > 0:
                retention = min(1.0, nr_cache / float(victim.file_size))
                if nr_cache_new and victim.last_retention is not None:
                    retention_delta = retention - victim.last_retention
                if nr_cache_new:
                    victim.last_retention = retention
            if parent_max and parent_max > 0:
                parent_cache_ratio = nr_cache / float(parent_max)

        retention_low = retention is not None and retention < self.conf.retention_low
        retention_healthy = (
            retention is not None and retention >= self.conf.retention_healthy
        )
        cache_falling = (
            nr_cache_delta is not None and nr_cache_delta <= -self.conf.cache_drop_bytes
        ) or (
            retention_delta is not None and retention_delta <= -self.conf.retention_drop
        )
        return VictimSample(
            name=victim.name,
            p99=p99,
            p99_new=p99_new,
            nr_cache=nr_cache,
            nr_cache_new=nr_cache_new,
            nr_cache_delta=nr_cache_delta,
            retention=retention,
            retention_delta=retention_delta,
            parent_cache_ratio=parent_cache_ratio,
            retention_low=retention_low,
            retention_healthy=retention_healthy,
            cache_falling=cache_falling,
            p99_bad=p99_bad,
            p99_healthy=p99_healthy,
            p99_slack=p99_slack,
        )

    def _sample_noisy(self, noisy, parent_max):
        cache_sample = read_live_int(noisy.spec.nr_cache_path)
        nr_cache = None
        nr_cache_new = False
        nr_cache_delta = None
        retention = None
        parent_cache_ratio = None
        if sample_is_fresh(cache_sample, self.conf.sample_max_age):
            wall, nr_cache = cache_sample
            nr_cache_new = wall != noisy.last_cache_wall
            if nr_cache_new:
                if noisy.last_nr_cache is not None:
                    nr_cache_delta = nr_cache - noisy.last_nr_cache
                noisy.last_nr_cache = nr_cache
                noisy.last_cache_wall = wall
            if noisy.file_size and noisy.file_size > 0:
                retention = min(1.0, nr_cache / float(noisy.file_size))
            if parent_max and parent_max > 0:
                parent_cache_ratio = nr_cache / float(parent_max)

        total = noisy.cgroup.total()
        psi_delta = max(0, total - noisy.last_total)
        noisy.last_total = total
        noisy.integral += psi_delta
        return NoisySample(
            name=noisy.name,
            nr_cache=nr_cache,
            nr_cache_new=nr_cache_new,
            nr_cache_delta=nr_cache_delta,
            retention=retention,
            parent_cache_ratio=parent_cache_ratio,
            cache_growing=(
                nr_cache_delta is not None
                and nr_cache_delta >= self.conf.cache_growth_bytes
            ),
            cache_dominant=(
                parent_cache_ratio is not None
                and parent_cache_ratio >= self.conf.noisy_parent_cache_threshold
            ),
            psi_avg10=noisy.cgroup.pressure(),
            psi_total=total,
            psi_delta=psi_delta,
            psi_integral=noisy.integral,
        )

    def sample_state(self):
        now = int(time.time())
        parent_current, parent_max, parent_util, parent_delta, parent_hot = self._sample_parent()
        victims = [self._sample_victim(v, parent_max) for v in self.victims]
        noisies = [self._sample_noisy(n, parent_max) for n in self.noisies]
        return Tick(
            wall_time=now,
            parent_current=parent_current,
            parent_max=parent_max,
            parent_util=parent_util,
            parent_max_event_delta=parent_delta,
            parent_hot=parent_hot,
            victims=victims,
            noisies=noisies,
        )

    def adjust_memory_low(self, tick, victim, sample, fairness_active):
        noisy_new = any(n.nr_cache_new for n in tick.noisies)
        if not (sample.p99_new or sample.nr_cache_new or noisy_new):
            return "hold_stale"

        any_noisy = any(n.evidence for n in tick.noisies)
        if victim_should_raise(tick.parent_hot, sample.evicting, any_noisy):
            old, new = victim.cgroup.adjust_low_bytes(self.conf.low_up)
            victim.low_grace_ticks = self.conf.low_interval
            if new != old:
                reasons = []
                if sample.p99_bad:
                    reasons.append("p99_bad")
                if sample.cache_falling:
                    reasons.append("c1_nr_cache_falling" if self.single
                                   else f'{victim.name}_nr_cache_falling')
                if sample.retention_low:
                    reasons.append("c1_retention_low" if self.single
                                   else f'{victim.name}_retention_low')
                if any(n.cache_growing for n in tick.noisies):
                    reasons.append("c2_nr_cache_growing" if self.single
                                   else "noisy_nr_cache_growing")
                if any(n.cache_dominant for n in tick.noisies):
                    reasons.append("c2_cache_share_high" if self.single
                                   else "noisy_cache_share_high")
                log(
                    f'  adjust {victim.name} memory.low: {h(old)} -> {h(new)} '
                    f'(+{h(self.conf.low_up)}; {",".join(reasons)})'
                )
                return "raise_low"
            return "hold_low_ceiling"

        if sample.p99_bad:
            if fairness_active and not sample.evicting:
                log(f'  hold {victim.name} memory.low: p99 bad, but this tenant is not losing cache')
                return "hold_bad_p99_other_tenant"
            log(f'  hold {victim.name} memory.low: p99 bad, but fairness gate is false')
            return "hold_bad_p99_no_contention"
        if sample.p99_slack:
            return "hold_slack"

        can_relax = (
            sample.p99_healthy
            and not tick.parent_hot
            and sample.retention_healthy
            and not sample.cache_falling
        )
        if not can_relax:
            return "hold_not_safe_to_relax"
        if victim.low_grace_ticks > 0:
            victim.low_grace_ticks -= 1
            return "hold_low_grace"

        old, new = victim.cgroup.adjust_low_bytes(-self.conf.low_down)
        victim.low_grace_ticks = self.conf.low_interval
        if new != old:
            log(
                f'  adjust {victim.name} memory.low: {h(old)} -> {h(new)} '
                f'(-{h(self.conf.low_down)}; healthy + cache recovered)'
            )
            return "lower_low"
        return "hold_low_floor"

    def adjust_memory_high(self, tick, noisy, sample, fairness_active):
        prefix = "c2" if self.single else noisy.name
        log(
            '%s_high=%s pressure=%f time_to_probe=%2d total=%d delta=%d integral=%d' %
            (prefix, h(noisy.cgroup.read_limit()), sample.psi_avg10,
             noisy.grace_ticks, sample.psi_total, sample.psi_delta, sample.psi_integral)
        )

        if noisy.integral > self.conf.pressure:
            if noisy_should_suppress(tick.parent_hot, any(v.evicting for v in tick.victims), sample.evidence):
                log(f'  suppress {noisy.name} memory.high increase: fairness_active')
                noisy.integral = 0
                noisy.grace_ticks = self.conf.interval - 1
                return "suppress_high_raise"

            err = noisy.integral / self.conf.pressure
            adj = (err / self.conf.coeff_backoff) ** 2
            adj = min(adj * self.conf.max_backoff, self.conf.max_backoff)
            self._adjust_high(noisy, adj)
            noisy.grace_ticks = self.conf.interval - 1
            return "raise_high"

        if noisy.grace_ticks:
            noisy.grace_ticks -= 1
            return "hold_high_grace"

        err = self.conf.pressure / max(noisy.integral, 1)
        adj = (err / self.conf.coeff_probe) ** 2
        adj = min(adj * self.conf.max_probe, self.conf.max_probe)
        self._adjust_high(noisy, -adj)
        noisy.grace_ticks = self.conf.interval - 1
        return "lower_high"

    def _adjust_high(self, noisy, factor):
        log(f'  adjust {noisy.name} memory.high: {factor:+.6f}')
        noisy.cgroup.scale_limit(factor)
        noisy.integral = 0

    def log_snapshot(self, tick, fairness_active, low_actions, high_actions):
        if self.single and tick.victims and tick.noisies:
            v = tick.victims[0]
            n = tick.noisies[0]
            log(
                'fairness parent=%s max_event+=%d '
                'c1_p99=%s c1_nr=%s c1_ret=%s c1_d=%s '
                'c2_nr=%s c2_parent_share=%s c2_d=%s '
                'gate=%s low_action=%s high_action=%s' % (
                    pct(tick.parent_util),
                    tick.parent_max_event_delta,
                    'n/a' if v.p99 is None else f'{v.p99:.1f}us',
                    h(v.nr_cache),
                    pct(v.retention),
                    h(v.nr_cache_delta),
                    h(n.nr_cache),
                    pct(n.parent_cache_ratio),
                    h(n.nr_cache_delta),
                    fairness_active,
                    low_actions[0],
                    high_actions[0],
                )
            )
            return

        log(
            'fairness parent=%s max_event+=%d gate=%s' % (
                pct(tick.parent_util), tick.parent_max_event_delta, fairness_active,
            )
        )
        for v, action in zip(tick.victims, low_actions):
            log(
                '  %s p99=%s nr=%s ret=%s d=%s low_action=%s' % (
                    v.name,
                    'n/a' if v.p99 is None else f'{v.p99:.1f}us',
                    h(v.nr_cache), pct(v.retention), h(v.nr_cache_delta), action,
                )
            )
        for n, action in zip(tick.noisies, high_actions):
            log(
                '  %s nr=%s parent_share=%s d=%s high_action=%s' % (
                    n.name, h(n.nr_cache), pct(n.parent_cache_ratio),
                    h(n.nr_cache_delta), action,
                )
            )

    def write_controller_csv(self, tick, fairness_active, low_actions, high_actions):
        if not self.conf.controller_csv:
            return
        with open(self.conf.controller_csv, 'a', newline='') as f:
            writer = csv.writer(f)
            for ctl, v, action in zip(self.victims, tick.victims, low_actions):
                writer.writerow([
                    tick.wall_time, v.name, 'victim',
                    tick.parent_current, tick.parent_max, tick.parent_util,
                    tick.parent_max_event_delta, int(tick.parent_hot),
                    v.p99, int(v.p99_bad), int(v.p99_healthy), int(v.p99_slack),
                    v.nr_cache, v.nr_cache_delta, v.retention, v.retention_delta,
                    v.parent_cache_ratio,
                    int(v.retention_low), int(v.retention_healthy), int(v.cache_falling),
                    '', '',
                    '', '', '',
                    int(fairness_active), ctl.cgroup.read_low(), '', action,
                ])
            for ctl, n, action in zip(self.noisies, tick.noisies, high_actions):
                writer.writerow([
                    tick.wall_time, n.name, 'noisy',
                    tick.parent_current, tick.parent_max, tick.parent_util,
                    tick.parent_max_event_delta, int(tick.parent_hot),
                    '', '', '', '',
                    n.nr_cache, n.nr_cache_delta, n.retention, '',
                    n.parent_cache_ratio,
                    '', '', '',
                    int(n.cache_growing), int(n.cache_dominant),
                    n.psi_avg10, n.psi_delta, n.psi_integral,
                    int(fairness_active), '', ctl.cgroup.read_limit(), action,
                ])


def _tenant_name(cgpath):
    return os.path.basename(cgpath.rstrip("/")) or cgpath


def build_specs(parser, conf):
    victim_paths = _as_list(conf.victim_cgpath)
    p99s = _align(parser, victim_paths, conf.victim_p99,
                  '--victim-cgpath', '--victim-p99', required=bool(victim_paths))
    caches = _align(parser, victim_paths, conf.victim_nr_cache,
                    '--victim-cgpath', '--victim-nr-cache', required=bool(victim_paths))
    files = _align(parser, victim_paths, conf.victim_file,
                   '--victim-cgpath', '--victim-file', required=False)
    sizes = _align(parser, victim_paths, conf.victim_file_size,
                   '--victim-cgpath', '--victim-file-size', required=False)
    victims = []
    for path, p99, cache, file_path, size in zip(victim_paths, p99s, caches, files, sizes):
        resolved = resolve_file_size(file_path, size)
        if resolved is None:
            parser.error(
                f'{path} requires --victim-file or --victim-file-size '
                'so nr_cache can be converted to a retention ratio'
            )
        victims.append(VictimSpec(path, p99, cache, file_path, resolved))

    if victims:
        if not conf.parent_cgpath:
            parser.error('fairness control requires --parent-cgpath')
        parents = {os.path.dirname(v.cgpath.rstrip("/")) for v in victims}
        if len(parents) != 1:
            parser.error('all --victim-cgpath values must share one parent directory')
        if not (0.0 < conf.retention_low <= conf.retention_healthy <= 1.0):
            parser.error('Require 0 < --retention-low <= --retention-healthy <= 1')
        if not (0.0 < conf.parent_util_threshold <= 1.0):
            parser.error('--parent-util-threshold must be in (0, 1]')

    noisy_paths = [conf.cgpath] + _as_list(conf.noisy_cgpath)
    need_cache = bool(victims)
    noisy_caches = _align(parser, noisy_paths, conf.noisy_nr_cache,
                          'noisy cgroup', '--noisy-nr-cache', required=need_cache)
    noisy_files = _align(parser, noisy_paths, conf.noisy_file,
                         'noisy cgroup', '--noisy-file', required=False)
    noisy_sizes = _align(parser, noisy_paths, conf.noisy_file_size,
                         'noisy cgroup', '--noisy-file-size', required=False)
    noisies = []
    for path, cache, file_path, size in zip(noisy_paths, noisy_caches, noisy_files, noisy_sizes):
        resolved = resolve_file_size(file_path, size)
        if need_cache and resolved is None:
            parser.error(
                f'{path} requires --noisy-file or --noisy-file-size '
                'so nr_cache can be converted to a retention ratio'
            )
        noisies.append(NoisySpec(path, cache, file_path, resolved))
    return victims, noisies


def self_check():
    """The one-victim, one-neighbor gate matches the original three-way AND."""
    for parent in (False, True):
        for victim in (False, True):
            for noisy in (False, True):
                gate = fairness_gate(parent, victim, noisy)
                assert victim_should_raise(parent, victim, noisy) is gate
                assert noisy_should_suppress(parent, victim, noisy) is gate
    # A healthy sibling is not raised just because another good tenant is evicted.
    assert fairness_gate(True, True, True)
    assert not victim_should_raise(True, False, True)
    # An idle neighbor may still expand while a different neighbor is displacing tenants.
    assert not noisy_should_suppress(True, True, False)
    assert noisy_should_suppress(True, True, True)


parser = argparse.ArgumentParser(
    description="""
Page-cache fairness Senpai (fork of facebookincubator/senpai).

Noisy neighbors:
  The positional cgpath is the first neighbor. Repeat --noisy-cgpath for more.
  Each neighbor's memory.high follows upstream Senpai's PSI law. An upward
  move is suppressed only for a neighbor that is itself taking cache while a
  good tenant is losing cache under a saturated shared parent.

Good tenants (--victim-cgpath, repeatable):
  Each tenant gets its own memory.low. Repeat --victim-p99, --victim-nr-cache,
  and --victim-file in the same order. memory.low rises only for a tenant that
  is itself losing cache.

Fairness gate:
  parent_hot
    AND some good tenant's retention is low or its nr_cache is falling
    AND some noisy neighbor's nr_cache is growing or dominates the parent

With one victim and one noisy neighbor this is the original gate.
""",
    formatter_class=argparse.RawDescriptionHelpFormatter,
)

parser.add_argument('cgpath', type=str,
                    help='First noisy cgroup — memory.high from PSI')
parser.add_argument('--noisy-cgpath', action='append', default=None,
                    help='Another noisy cgroup (repeatable). Pair with another '
                         '--noisy-nr-cache and --noisy-file, in order.')
parser.add_argument('--min-size', type=int, default=100 << 20)
parser.add_argument('--max-size', type=int, default=100 << 30)
parser.add_argument('--interval', type=int, default=6)
parser.add_argument('--pressure', type=int, default=10 * 1000)
parser.add_argument('--max-probe', type=float, default=0.01)
parser.add_argument('--max-backoff', type=float, default=1.0)
parser.add_argument('--coeff-probe', type=int, default=10)
parser.add_argument('--coeff-backoff', type=int, default=20)

parser.add_argument('--victim-cgpath', action='append', default=None,
                    help='Good-tenant cgroup — memory.low actuator (repeatable)')
parser.add_argument('--parent-cgpath', type=str, default=None,
                    help='Shared parent cgroup, e.g. /sys/fs/cgroup/clients')

parser.add_argument('--victim-p99', '--victim-p99-csv', dest='victim_p99',
                    action='append', default=None,
                    help='One-line live p99 file per victim, in --victim-cgpath order')
parser.add_argument('--victim-nr-cache', '--victim-resident', dest='victim_nr_cache',
                    action='append', default=None,
                    help='One-line nr_cache file per victim, in --victim-cgpath order')
parser.add_argument('--noisy-nr-cache', action='append', default=None,
                    help='One-line nr_cache file per noisy neighbor, positional cgpath first')

parser.add_argument('--victim-file', action='append', default=None,
                    help='Victim target file, in --victim-cgpath order')
parser.add_argument('--noisy-file', action='append', default=None,
                    help='Noisy target file, positional cgpath first')
parser.add_argument('--victim-file-size', action='append', type=int, default=None,
                    help='Victim target-file size in bytes; overrides --victim-file stat')
parser.add_argument('--noisy-file-size', action='append', type=int, default=None,
                    help='Noisy target-file size in bytes; overrides --noisy-file stat')

parser.add_argument('--parent-util-threshold', type=float, default=0.90,
                    help='Parent memory.current/memory.max threshold for contention '
                         '(default: 0.90)')
parser.add_argument('--retention-low', type=float, default=0.85,
                    help='nr_cache/file_size ratio considered low (default: 0.85)')
parser.add_argument('--retention-healthy', type=float, default=0.95,
                    help='nr_cache/file_size ratio required before relaxing memory.low '
                         '(default: 0.95)')
parser.add_argument('--retention-drop', type=float, default=0.03,
                    help='One-sample retention-ratio drop considered eviction '
                         '(default: 0.03)')
parser.add_argument('--cache-drop-bytes', type=int, default=16 << 20,
                    help='nr_cache drop considered eviction (default: 16 MiB)')
parser.add_argument('--cache-growth-bytes', type=int, default=16 << 20,
                    help='Noisy nr_cache growth considered growth (default: 16 MiB)')
parser.add_argument('--noisy-parent-cache-threshold', type=float, default=0.50,
                    help='Noisy nr_cache / parent memory.max ratio considered large '
                         '(default: 0.50)')
parser.add_argument('--sample-max-age', type=float, default=3.0,
                    help='Ignore live p99/nr_cache samples older than this many seconds '
                         '(default: 3.0)')

parser.add_argument('--p99-target', type=float, default=3.0,
                    help='Good-tenant p99 SLO in microseconds (default: 3.0)')
parser.add_argument('--p99-delta', type=float, default=3.0,
                    help='Slack above p99 target; p99 > target+delta is bad '
                         '(default: 3.0 us)')

parser.add_argument('--low-min-size', type=int, default=0,
                    help='Floor for memory.low in bytes')
parser.add_argument('--low-max-size', type=int, default=2 << 30,
                    help='Ceiling for memory.low in bytes (default: 2 GiB)')
parser.add_argument('--low-up', type=int, default=32 << 20,
                    help='Bytes to add to memory.low when this tenant is displaced '
                         '(default: 32 MiB)')
parser.add_argument('--low-down', type=int, default=16 << 20,
                    help='Bytes to subtract when safely relaxing protection '
                         '(default: 16 MiB)')
parser.add_argument('--low-interval', type=int, default=6,
                    help='Healthy samples required between memory.low downward probes '
                         '(default: 6)')

parser.add_argument('--controller-csv', default=None,
                    help='Optional CSV path. One row per tenant per tick.')

if __name__ == '__main__':
    self_check()
    conf = parser.parse_args()
    victim_specs, noisy_specs = build_specs(parser, conf)
    Senpai(conf, victim_specs, noisy_specs).run()

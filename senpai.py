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
# Dual control:
#   Client2 (noisy, positional cgpath)
#       memory.high from PSI, except upward expansion is suppressed while
#       the fairness controller observes victim cache displacement.
#
#   Client1 (victim, --victim-cgpath)
#       memory.low is raised only when all of the following hold:
#         1. shared parent is at/near memory.max (or hit max this tick),
#         2. Client1's target-file nr_cache retention is low/falling, and
#         3. Client2's nr_cache is growing or occupies a large share of the
#            parent memory budget.
#
#       p99 is an application-level harm signal and provides a slack/healthy
#       band for deciding when protection can safely be relaxed.
#
# The benchmark harness should start this script only after phase 0 (Client1
# warmup) so neither controller moves knobs during warm-up.

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
                 low_min=0, low_max=None):
        self.path = path
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
                log(f'Adjusted Client1 memory.low to {h(self.low)}')
            else:
                log(f'Preserving Client1 memory.low={h(self.low)}')

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
            log('Resetting Client2 memory.high to memory.current.')
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


@dataclass
class Sample:
    wall_time: int

    parent_current: Optional[int]
    parent_max: Optional[int]
    parent_util: Optional[float]
    parent_max_event_delta: int
    parent_hot: bool

    c1_p99: Optional[float]
    c1_p99_new: bool

    c1_nr_cache: Optional[int]
    c1_nr_cache_new: bool
    c1_nr_cache_delta: Optional[int]
    c1_retention: Optional[float]
    c1_retention_delta: Optional[float]
    c1_parent_cache_ratio: Optional[float]

    c2_nr_cache: Optional[int]
    c2_nr_cache_new: bool
    c2_nr_cache_delta: Optional[int]
    c2_retention: Optional[float]
    c2_parent_cache_ratio: Optional[float]

    c1_retention_low: bool
    c1_retention_healthy: bool
    c1_cache_falling: bool
    c2_cache_growing: bool
    c2_cache_dominant: bool

    p99_bad: bool
    p99_healthy: bool
    p99_slack: bool

    c2_psi_avg10: float
    c2_psi_total: int
    c2_psi_delta: int
    c2_psi_integral: int


class Senpai(object):
    def __init__(self, conf):
        self.conf = conf
        log('Configuration:')
        for key, val in vars(conf).items():
            log(f'  {key} = {val}')

        # Client2 (noisy neighbor): upstream PSI -> memory.high controller.
        self.cgroup = Cgroup(
            self.conf.cgpath,
            self.conf.min_size,
            self.conf.max_size,
        )
        self.last_total = self.cgroup.total()
        self.integral = 0
        self.grace_ticks = self.conf.interval

        # Shared parent is read-only from this controller's perspective.
        self.parent = None
        self.last_parent_max_events = 0
        if self.conf.parent_cgpath:
            self.parent = Cgroup(
                self.conf.parent_cgpath,
                manage_high=False,
                manage_low=False,
            )
            self.last_parent_max_events = self.parent.read_event("max")

        # Client1 (victim): memory.low from contention + retention + p99.
        self.victim = None
        self.low_grace_ticks = self.conf.low_interval

        self.last_p99_wall_time = None
        self.last_c1_cache_wall_time = None
        self.last_c2_cache_wall_time = None
        self.last_c1_nr_cache = None
        self.last_c2_nr_cache = None
        self.last_c1_retention = None

        self.c1_file_size = resolve_file_size(
            self.conf.victim_file,
            self.conf.victim_file_size,
        )
        self.c2_file_size = resolve_file_size(
            self.conf.noisy_file,
            self.conf.noisy_file_size,
        )

        if self.conf.victim_cgpath:
            self.victim = Cgroup(
                self.conf.victim_cgpath,
                self.conf.min_size,
                self.conf.max_size,
                manage_high=False,
                manage_low=True,
                low_min=self.conf.low_min_size,
                low_max=self.conf.low_max_size,
            )
            log(
                'Client1 fairness control armed '
                f'(p99 target={self.conf.p99_target:.1f}±{self.conf.p99_delta:.1f} us, '
                f'parent_hot>={self.conf.parent_util_threshold:.0%}, '
                f'retention_low<{self.conf.retention_low:.0%}, '
                f'retention_healthy>={self.conf.retention_healthy:.0%})'
            )
            log(f'Client1 target-file size={h(self.c1_file_size)}')
            log(f'Client2 target-file size={h(self.c2_file_size)}')
        else:
            log('No --victim-cgpath; Client1 memory.low control disabled')

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
            writer = csv.writer(f)
            writer.writerow([
                'wall_time',
                'parent_current', 'parent_max', 'parent_util',
                'parent_max_event_delta', 'parent_hot',
                'c1_p99_us', 'p99_bad', 'p99_healthy', 'p99_slack',
                'c1_nr_cache', 'c1_nr_cache_delta', 'c1_retention',
                'c1_retention_delta', 'c1_parent_cache_ratio',
                'c1_retention_low', 'c1_retention_healthy', 'c1_cache_falling',
                'c2_nr_cache', 'c2_nr_cache_delta', 'c2_retention',
                'c2_parent_cache_ratio', 'c2_cache_growing', 'c2_cache_dominant',
                'c2_psi_avg10', 'c2_psi_delta', 'c2_psi_integral',
                'fairness_active', 'c1_memory_low', 'c2_memory_high',
                'low_action', 'high_action',
            ])

    def run(self):
        while True:
            time.sleep(1)
            self.tick()

    def tick(self):
        sample = self.sample_state()
        fairness_active = self.classify_fairness(sample)

        # Protect the victim first, then decide whether Client2 is allowed to
        # expand memory.high on this tick.
        low_action = self.adjust_memory_low(sample, fairness_active)
        high_action = self.adjust_memory_high(sample, fairness_active)

        self.log_snapshot(sample, fairness_active, low_action, high_action)
        self.write_controller_csv(sample, fairness_active, low_action, high_action)

    def sample_state(self):
        now = int(time.time())

        # ---------- Parent contention ----------
        parent_current = None
        parent_max = None
        parent_util = None
        parent_max_event_delta = 0
        parent_hot = False

        if self.parent:
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

        # ---------- Client1 p99 ----------
        p99_sample = read_live_float(self.conf.victim_p99)
        c1_p99 = None
        c1_p99_new = False
        if sample_is_fresh(p99_sample, self.conf.sample_max_age):
            p99_wall, c1_p99 = p99_sample
            c1_p99_new = p99_wall != self.last_p99_wall_time
            if c1_p99_new:
                self.last_p99_wall_time = p99_wall

        target = max(self.conf.p99_target, 1e-6)
        bad_ceil = target + max(self.conf.p99_delta, 0.0)
        p99_bad = c1_p99 is not None and c1_p99 > bad_ceil
        p99_healthy = c1_p99 is not None and c1_p99 <= target
        p99_slack = (
            c1_p99 is not None
            and target < c1_p99 <= bad_ceil
        )

        # ---------- nr_cache live samples ----------
        c1_sample = read_live_int(self.conf.victim_nr_cache)
        c2_sample = read_live_int(self.conf.noisy_nr_cache)

        c1_nr_cache = None
        c1_nr_cache_new = False
        c1_nr_cache_delta = None
        c1_retention = None
        c1_retention_delta = None
        c1_parent_cache_ratio = None

        if sample_is_fresh(c1_sample, self.conf.sample_max_age):
            c1_wall, c1_nr_cache = c1_sample
            c1_nr_cache_new = c1_wall != self.last_c1_cache_wall_time

            if c1_nr_cache_new:
                if self.last_c1_nr_cache is not None:
                    c1_nr_cache_delta = c1_nr_cache - self.last_c1_nr_cache
                self.last_c1_nr_cache = c1_nr_cache
                self.last_c1_cache_wall_time = c1_wall

            if self.c1_file_size and self.c1_file_size > 0:
                c1_retention = min(1.0, c1_nr_cache / float(self.c1_file_size))
                if c1_nr_cache_new and self.last_c1_retention is not None:
                    c1_retention_delta = c1_retention - self.last_c1_retention
                if c1_nr_cache_new:
                    self.last_c1_retention = c1_retention

            if parent_max and parent_max > 0:
                c1_parent_cache_ratio = c1_nr_cache / float(parent_max)

        c2_nr_cache = None
        c2_nr_cache_new = False
        c2_nr_cache_delta = None
        c2_retention = None
        c2_parent_cache_ratio = None

        if sample_is_fresh(c2_sample, self.conf.sample_max_age):
            c2_wall, c2_nr_cache = c2_sample
            c2_nr_cache_new = c2_wall != self.last_c2_cache_wall_time

            if c2_nr_cache_new:
                if self.last_c2_nr_cache is not None:
                    c2_nr_cache_delta = c2_nr_cache - self.last_c2_nr_cache
                self.last_c2_nr_cache = c2_nr_cache
                self.last_c2_cache_wall_time = c2_wall

            if self.c2_file_size and self.c2_file_size > 0:
                c2_retention = min(1.0, c2_nr_cache / float(self.c2_file_size))

            if parent_max and parent_max > 0:
                c2_parent_cache_ratio = c2_nr_cache / float(parent_max)

        # ---------- Derived cache signals ----------
        c1_retention_low = (
            c1_retention is not None
            and c1_retention < self.conf.retention_low
        )
        c1_retention_healthy = (
            c1_retention is not None
            and c1_retention >= self.conf.retention_healthy
        )
        c1_cache_falling = (
            c1_nr_cache_delta is not None
            and c1_nr_cache_delta <= -self.conf.cache_drop_bytes
        ) or (
            c1_retention_delta is not None
            and c1_retention_delta <= -self.conf.retention_drop
        )
        c2_cache_growing = (
            c2_nr_cache_delta is not None
            and c2_nr_cache_delta >= self.conf.cache_growth_bytes
        )
        c2_cache_dominant = (
            c2_parent_cache_ratio is not None
            and c2_parent_cache_ratio >= self.conf.noisy_parent_cache_threshold
        )

        # ---------- Client2 PSI ----------
        total = self.cgroup.total()
        psi_delta = max(0, total - self.last_total)
        self.last_total = total
        self.integral += psi_delta
        psi_avg10 = self.cgroup.pressure()

        return Sample(
            wall_time=now,
            parent_current=parent_current,
            parent_max=parent_max,
            parent_util=parent_util,
            parent_max_event_delta=parent_max_event_delta,
            parent_hot=parent_hot,
            c1_p99=c1_p99,
            c1_p99_new=c1_p99_new,
            c1_nr_cache=c1_nr_cache,
            c1_nr_cache_new=c1_nr_cache_new,
            c1_nr_cache_delta=c1_nr_cache_delta,
            c1_retention=c1_retention,
            c1_retention_delta=c1_retention_delta,
            c1_parent_cache_ratio=c1_parent_cache_ratio,
            c2_nr_cache=c2_nr_cache,
            c2_nr_cache_new=c2_nr_cache_new,
            c2_nr_cache_delta=c2_nr_cache_delta,
            c2_retention=c2_retention,
            c2_parent_cache_ratio=c2_parent_cache_ratio,
            c1_retention_low=c1_retention_low,
            c1_retention_healthy=c1_retention_healthy,
            c1_cache_falling=c1_cache_falling,
            c2_cache_growing=c2_cache_growing,
            c2_cache_dominant=c2_cache_dominant,
            p99_bad=p99_bad,
            p99_healthy=p99_healthy,
            p99_slack=p99_slack,
            c2_psi_avg10=psi_avg10,
            c2_psi_total=total,
            c2_psi_delta=psi_delta,
            c2_psi_integral=self.integral,
        )

    def classify_fairness(self, s):
        """Return True only when evidence points to cross-cgroup cache contention."""
        victim_eviction = s.c1_retention_low or s.c1_cache_falling
        noisy_neighbor_evidence = s.c2_cache_growing or s.c2_cache_dominant

        # Require all three causal pieces:
        #   shared memory scarcity + victim cache loss + noisy-neighbor cache use.
        return s.parent_hot and victim_eviction and noisy_neighbor_evidence

    def adjust_memory_low(self, s, fairness_active):
        if not self.victim:
            return "disabled"

        # Only make a new decision when at least one live victim/noisy signal
        # advanced. This prevents one stale p99/nr_cache row from causing
        # repeated +32 MiB changes every controller tick.
        fresh_decision = s.c1_p99_new or s.c1_nr_cache_new or s.c2_nr_cache_new
        if not fresh_decision:
            return "hold_stale"

        low = self.victim.read_low()

        # Fast path: protect victim only after contention + displacement are
        # both established. p99 may already be bad, but residency loss itself
        # is allowed to act as an early warning before latency fully degrades.
        if fairness_active:
            old, new = self.victim.adjust_low_bytes(self.conf.low_up)
            self.low_grace_ticks = self.conf.low_interval
            if new != old:
                reasons = []
                if s.p99_bad:
                    reasons.append("p99_bad")
                if s.c1_cache_falling:
                    reasons.append("c1_nr_cache_falling")
                if s.c1_retention_low:
                    reasons.append("c1_retention_low")
                if s.c2_cache_growing:
                    reasons.append("c2_nr_cache_growing")
                if s.c2_cache_dominant:
                    reasons.append("c2_cache_share_high")
                log(
                    f'  adjust Client1 memory.low: {h(old)} -> {h(new)} '
                    f'(+{h(self.conf.low_up)}; {",".join(reasons)})'
                )
                return "raise_low"
            return "hold_low_ceiling"

        # A latency spike without memory-contention/cache-displacement evidence
        # is explicitly not blamed on page-cache unfairness.
        if s.p99_bad:
            log('  hold Client1 memory.low: p99 bad, but fairness gate is false')
            return "hold_bad_p99_no_contention"

        # Slack band: retain the current protection.
        if s.p99_slack:
            return "hold_slack"

        # Relax protection only when the victim is healthy, the shared parent
        # is not currently hot, and Client1's target-file cache has recovered.
        can_relax = (
            s.p99_healthy
            and not s.parent_hot
            and s.c1_retention_healthy
            and not s.c1_cache_falling
        )

        if not can_relax:
            return "hold_not_safe_to_relax"

        if self.low_grace_ticks > 0:
            self.low_grace_ticks -= 1
            return "hold_low_grace"

        old, new = self.victim.adjust_low_bytes(-self.conf.low_down)
        self.low_grace_ticks = self.conf.low_interval
        if new != old:
            log(
                f'  adjust Client1 memory.low: {h(old)} -> {h(new)} '
                f'(-{h(self.conf.low_down)}; healthy + cache recovered)'
            )
            return "lower_low"
        return "hold_low_floor"

    def adjust_memory_high(self, s, fairness_active):
        """Run upstream PSI policy, coordinating only the upward adjustment."""
        log(
            'c2_high=%s pressure=%f time_to_probe=%2d total=%d delta=%d integral=%d' %
            (h(self.cgroup.read_limit()), s.c2_psi_avg10,
             self.grace_ticks, s.c2_psi_total, s.c2_psi_delta, self.integral)
        )

        if self.integral > self.conf.pressure:
            if fairness_active:
                # Do not let C2 expand its high boundary while it is actively
                # displacing the victim. Reset the accumulated PSI budget so a
                # huge deferred upward jump does not occur later.
                log('  suppress Client2 memory.high increase: fairness_active')
                self.integral = 0
                self.grace_ticks = self.conf.interval - 1
                return "suppress_high_raise"

            err = self.integral / self.conf.pressure
            adj = (err / self.conf.coeff_backoff) ** 2
            adj = min(adj * self.conf.max_backoff, self.conf.max_backoff)
            self.adjust_high(adj)
            self.grace_ticks = self.conf.interval - 1
            return "raise_high"

        if self.grace_ticks:
            self.grace_ticks -= 1
            return "hold_high_grace"

        # Low PSI: preserve upstream's downward probe. This direction helps
        # fairness, so it is allowed even if the fairness gate is active.
        err = self.conf.pressure / max(self.integral, 1)
        adj = (err / self.conf.coeff_probe) ** 2
        adj = min(adj * self.conf.max_probe, self.conf.max_probe)
        self.adjust_high(-adj)
        self.grace_ticks = self.conf.interval - 1
        return "lower_high"

    def adjust_high(self, factor):
        log(f'  adjust Client2 memory.high: {factor:+.6f}')
        self.cgroup.scale_limit(factor)
        self.integral = 0

    def log_snapshot(self, s, fairness_active, low_action, high_action):
        log(
            'fairness parent=%s max_event+=%d '
            'c1_p99=%s c1_nr=%s c1_ret=%s c1_d=%s '
            'c2_nr=%s c2_parent_share=%s c2_d=%s '
            'gate=%s low_action=%s high_action=%s' % (
                pct(s.parent_util),
                s.parent_max_event_delta,
                'n/a' if s.c1_p99 is None else f'{s.c1_p99:.1f}us',
                h(s.c1_nr_cache),
                pct(s.c1_retention),
                h(s.c1_nr_cache_delta),
                h(s.c2_nr_cache),
                pct(s.c2_parent_cache_ratio),
                h(s.c2_nr_cache_delta),
                fairness_active,
                low_action,
                high_action,
            )
        )

    def write_controller_csv(self, s, fairness_active, low_action, high_action):
        if not self.conf.controller_csv:
            return
        with open(self.conf.controller_csv, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                s.wall_time,
                s.parent_current, s.parent_max, s.parent_util,
                s.parent_max_event_delta, int(s.parent_hot),
                s.c1_p99, int(s.p99_bad), int(s.p99_healthy), int(s.p99_slack),
                s.c1_nr_cache, s.c1_nr_cache_delta, s.c1_retention,
                s.c1_retention_delta, s.c1_parent_cache_ratio,
                int(s.c1_retention_low), int(s.c1_retention_healthy), int(s.c1_cache_falling),
                s.c2_nr_cache, s.c2_nr_cache_delta, s.c2_retention,
                s.c2_parent_cache_ratio, int(s.c2_cache_growing), int(s.c2_cache_dominant),
                s.c2_psi_avg10, s.c2_psi_delta, s.c2_psi_integral,
                int(fairness_active),
                self.victim.read_low() if self.victim else None,
                self.cgroup.read_limit(),
                low_action, high_action,
            ])


def validate_conf(parser, conf):
    if not conf.victim_cgpath:
        return

    required = {
        '--parent-cgpath': conf.parent_cgpath,
        '--victim-p99': conf.victim_p99,
        '--victim-nr-cache': conf.victim_nr_cache,
        '--noisy-nr-cache': conf.noisy_nr_cache,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error(
            'Client1 fairness control requires: ' + ', '.join(missing)
        )

    if resolve_file_size(conf.victim_file, conf.victim_file_size) is None:
        parser.error(
            'Client1 fairness control requires --victim-file or '
            '--victim-file-size so nr_cache can be converted to a retention ratio'
        )

    if not (0.0 < conf.retention_low <= conf.retention_healthy <= 1.0):
        parser.error(
            'Require 0 < --retention-low <= --retention-healthy <= 1'
        )

    if not (0.0 < conf.parent_util_threshold <= 1.0):
        parser.error('--parent-util-threshold must be in (0, 1]')


parser = argparse.ArgumentParser(
    description="""
Page-cache fairness Senpai (fork of facebookincubator/senpai).

Client2 (positional cgpath):
  Dynamically adjusts memory.high from PSI using upstream Senpai's control law.
  An upward memory.high adjustment is suppressed while the fairness gate says
  Client2 is displacing Client1 under a saturated shared parent.

Client1 (--victim-cgpath):
  Dynamically adjusts memory.low using parent memory utilization,
  memory.events.local:max, live p99, and live cachestat nr_cache samples.

Fairness gate:
  parent_hot
    AND (Client1 retention low OR Client1 nr_cache falling)
    AND (Client2 nr_cache growing OR Client2 nr_cache / parent memory.max high)

Client1 memory.low:
  fairness gate active     -> +LOW_UP (default 32 MiB)
  p99 slack / uncertain    -> hold
  healthy + parent cool + Client1 retention recovered for grace period
                           -> -LOW_DOWN (default 16 MiB)

The live nr_cache files are expected to contain one line:
  wall_time,nr_cache_bytes
where nr_cache_bytes is cachestat nr_cache * page_size for the target file.
""",
    formatter_class=argparse.RawDescriptionHelpFormatter,
)

# Client2 / upstream Senpai knobs
parser.add_argument('cgpath', type=str,
                    help='Client2 (noisy) cgroup path — memory.high from PSI')
parser.add_argument('--min-size', type=int, default=100 << 20)
parser.add_argument('--max-size', type=int, default=100 << 30)
parser.add_argument('--interval', type=int, default=6)
parser.add_argument('--pressure', type=int, default=10 * 1000)
parser.add_argument('--max-probe', type=float, default=0.01)
parser.add_argument('--max-backoff', type=float, default=1.0)
parser.add_argument('--coeff-probe', type=int, default=10)
parser.add_argument('--coeff-backoff', type=int, default=20)

# Client1 fairness / memory.low controller
parser.add_argument('--victim-cgpath', type=str, default=None,
                    help='Client1 (victim) cgroup path — memory.low actuator')
parser.add_argument('--parent-cgpath', type=str, default=None,
                    help='Shared parent cgroup, e.g. /sys/fs/cgroup/clients')

parser.add_argument('--victim-p99', '--victim-p99-csv', dest='victim_p99',
                    default=None,
                    help='One-line live p99 file: wall_time,p99_us')
parser.add_argument('--victim-nr-cache', '--victim-resident', dest='victim_nr_cache',
                    default=None,
                    help='One-line Client1 cachestat nr_cache file: wall_time,bytes')
parser.add_argument('--noisy-nr-cache', default=None,
                    help='One-line Client2 cachestat nr_cache file: wall_time,bytes')

parser.add_argument('--victim-file', default=None,
                    help='Client1 target file; used only to get file size')
parser.add_argument('--noisy-file', default=None,
                    help='Client2 target file; used only to get file size')
parser.add_argument('--victim-file-size', type=int, default=None,
                    help='Client1 target-file size in bytes; overrides --victim-file stat')
parser.add_argument('--noisy-file-size', type=int, default=None,
                    help='Client2 target-file size in bytes; overrides --noisy-file stat')

parser.add_argument('--parent-util-threshold', type=float, default=0.90,
                    help='Parent memory.current/memory.max threshold for contention '
                         '(default: 0.90)')
parser.add_argument('--retention-low', type=float, default=0.85,
                    help='Client1 nr_cache/file_size ratio considered low '
                         '(default: 0.85)')
parser.add_argument('--retention-healthy', type=float, default=0.95,
                    help='Client1 nr_cache/file_size ratio required before relaxing '
                         'memory.low (default: 0.95)')
parser.add_argument('--retention-drop', type=float, default=0.03,
                    help='One-sample Client1 retention-ratio drop considered eviction '
                         '(default: 0.03)')
parser.add_argument('--cache-drop-bytes', type=int, default=16 << 20,
                    help='Client1 nr_cache drop considered eviction (default: 16 MiB)')
parser.add_argument('--cache-growth-bytes', type=int, default=16 << 20,
                    help='Client2 nr_cache growth considered noisy growth '
                         '(default: 16 MiB)')
parser.add_argument('--noisy-parent-cache-threshold', type=float, default=0.50,
                    help='Client2 nr_cache / parent memory.max ratio considered large '
                         '(default: 0.50)')
parser.add_argument('--sample-max-age', type=float, default=3.0,
                    help='Ignore live p99/nr_cache samples older than this many seconds '
                         '(default: 3.0)')

parser.add_argument('--p99-target', type=float, default=3.0,
                    help='Client1 p99 SLO in microseconds (default: 3.0)')
parser.add_argument('--p99-delta', type=float, default=3.0,
                    help='Slack above p99 target; p99 > target+delta is bad '
                         '(default: 3.0 us)')

parser.add_argument('--low-min-size', type=int, default=0,
                    help='Floor for Client1 memory.low in bytes')
parser.add_argument('--low-max-size', type=int, default=2 << 30,
                    help='Ceiling for Client1 memory.low in bytes (default: 2 GiB)')
parser.add_argument('--low-up', type=int, default=32 << 20,
                    help='Bytes to add to memory.low on fairness activation '
                         '(default: 32 MiB)')
parser.add_argument('--low-down', type=int, default=16 << 20,
                    help='Bytes to subtract when safely relaxing protection '
                         '(default: 16 MiB)')
parser.add_argument('--low-interval', type=int, default=6,
                    help='Healthy samples required between memory.low downward probes '
                         '(default: 6)')

parser.add_argument('--controller-csv', default=None,
                    help='Optional CSV path for per-tick controller diagnostics')

conf = parser.parse_args()
validate_conf(parser, conf)
senpai = Senpai(conf)
senpai.run()

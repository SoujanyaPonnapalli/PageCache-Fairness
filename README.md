# Page Cache Fairness: Bounding p99 Latency Spikes in Multi-Tenant KV Stores

This project investigates and addresses performance isolation failures in the Linux OS page cache for co-located multi-tenant key-value store workloads. We focus specifically on bounding p99 read latency spikes — a problem that existing OS mechanisms leave unsolved.

---

## Context and Motivation

[Delta Fair Sharing](https://arxiv.org/abs/2601.20030) (SOSP '26) solves performance isolation for RocksDB's *internal* caches (write buffer, block cache) using a fairness-aware kernel module. For the **OS page cache**, the paper identifies interference as an open problem but does not provide a solution. That is the gap this project fills.

**The interference mechanism** *(hypothesis to be validated experimentally)*: When tenant B exceeds its fair share of page cache, it evicts tenant A's pages from the LRU. A then reads up to its fair share, encounters cache misses, and issues disk reads. If B is also write-heavy, kswapd/the flusher concurrently drains B's dirty pages to disk. A's reads land in the I/O queue *behind* B's writeback flushes. A's effective read latency = writeback drain time + disk read time — far higher than a baseline disk read alone. The dirty writeback does not bypass cache eviction; it **amplifies the per-miss penalty** on top of it.

**Why existing approaches miss this**: cgroup memory limits and PSI-based tools (Senpai) act on total cached pages — they can reduce how much B evicts A, but even with perfect memory sizing, B's dirty page flushes still contend with A's reads at the I/O scheduler. PSI fires correctly for A's elevated stall but triggers the wrong remedy (memory limit adjustment) when the root cause is I/O queue contention from B's writeback.

---

## Reading List

### Must-Read Papers
| Paper | Why |
|---|---|
| [Delta Fair Sharing](https://arxiv.org/abs/2601.20030) — SOSP '26 | Direct motivating paper; understand what they solve and what they leave open (OS page cache) |
| [cache_ext: Customizing the Page Cache with eBPF](https://www.asafcidon.com/uploads/5/9/7/0/59701649/cache_ext.pdf) — SOSP '25 · [ACM](https://dl.acm.org/doi/10.1145/3731569.3764820) | Most relevant mechanism paper; eBPF hooks into eviction path; read codebase at [github.com/cache-ext/cache_ext](https://github.com/cache-ext/cache_ext) |
| [RobinHood: Tail Latency Aware Caching](https://www.usenix.org/system/files/osdi18-berger.pdf) — OSDI '18 | Best prior art on p99-as-first-class-objective in caching; design pattern for latency-feedback-driven reallocation |
| [NyxCache](https://research.cs.wisc.edu/adsl/Publications/fast22-kan.pdf) — FAST '22 | Multi-tenant PM caching with explicit QoS latency guarantees; 5× better isolation than bandwidth-limiting |
| [StreamCache](https://www.usenix.org/system/files/atc24-li-zhiyue.pdf) — ATC '24 | Page cache for scan-heavy NVMe workloads; directly addresses scan-induced latency degradation |

### Background: Eviction Algorithms
| Algorithm | Link | Why Relevant |
|---|---|---|
| LIRS | [PDF](https://ranger.uta.edu/~sjiang/pubs/papers/jiang02_LIRS.pdf) | Scan-resistant; distinguishes hot vs. cold streams |
| ARC | [USENIX](https://www.usenix.org/conference/fast-03/arc-self-tuning-low-overhead-replacement-cache) | Balances recency + frequency; used in ZFS |
| W-TinyLFU | [arXiv](https://arxiv.org/abs/1512.00727) | Frequency-aware; used in RocksDB's block cache |
| CLOCK-Pro | [PDF](https://rcs.uwaterloo.ca/papers/clockpro.pdf) | Lightweight LIRS approximation |

### Dynamic / Adaptive Memory Management
| Resource | Why |
|---|---|
| [TMO: Transparent Memory Offloading in Datacenters](https://www.cs.cmu.edu/~dskarlat/publications/tmo_asplos22.pdf) — ASPLOS '22 (Best Paper) | Meta's PSI-feedback loop for runtime memory sizing; the production blueprint for Senpai-style control |
| [Senpai (Meta, GitHub)](https://github.com/facebookincubator/senpai) | Userspace daemon that polls `memory.pressure` and adjusts `memory.high` dynamically; most direct example of adaptive cgroup tuning |
| [Cuki: Adaptive Online Cache Capacity Optimization](https://www.usenix.org/system/files/atc23-gu.pdf) — ATC '23 | Lightweight working-set-size estimation at runtime; 79% latency reduction; directly relevant to sizing `memory.low` automatically |
| [eBPF-Based Working Set Size Estimation](https://arxiv.org/pdf/2303.05919) — arXiv | Uses eBPF to estimate per-cgroup WSS online; feeds into dynamic memory allocation decisions |
| [mPart: MRC-Guided Partitioning in KV Stores](https://dl.acm.org/doi/10.1145/3210563.3210571) — ISMM '18 | Online miss-ratio-curve construction to compute near-optimal memory split between tenants dynamically |
| [PSI — Pressure Stall Information (kernel docs)](https://docs.kernel.org/accounting/psi.html) | The core feedback signal used by all adaptive tools; `memory.pressure` per-cgroup file + eventfd triggers |
| [PSI overview (Facebook)](https://facebookmicrosites.github.io/psi/docs/overview) | Practical explanation of PSI metrics and how to use them for event-driven reconfiguration |

### Documentation
| Resource | Link |
|---|---|
| Linux cgroup v2 memory controller (canonical) | [kernel.org](https://docs.kernel.org/admin-guide/cgroup-v2.html) |
| cgroup v2 memory controller (practical guide) | [Facebook](https://facebookmicrosites.github.io/cgroup2/docs/memory-controller.html) |
| cgroup v2 + page cache deep-dive | [Biriukov](https://biriukov.dev/docs/page-cache/6-cgroup-v2-and-page-cache/) |
| `mm/vmscan.c` — core eviction logic | [Elixir](https://elixir.bootlin.com/linux/latest/source/mm/vmscan.c) |
| `mm/memcontrol.c` — per-cgroup accounting | [Elixir](https://elixir.bootlin.com/linux/latest/source/mm/memcontrol.c) |
| LWN: `memory.min` introduction | [LWN](https://lwn.net/Articles/752423/) |
| cache_ext codebase | [GitHub](https://github.com/cache-ext/cache_ext) |

---

## How the Linux Page Cache Works (and Why It Fails)

### Default Policy

The Linux page cache stores file-backed data in DRAM to avoid repeated disk reads. It is managed as a **two-list LRU** (inactive + active) per NUMA node, implemented in [`mm/vmscan.c`](https://elixir.bootlin.com/linux/latest/source/mm/vmscan.c):

- **Insertion:** A page read for the first time (`add_to_page_cache_lru()`) lands on the **inactive list**. On second access it is promoted to the **active list**.
- **Eviction under pressure:** `kswapd` wakes when free memory falls below a watermark, calls `shrink_lruvec()` → `shrink_page_list()`, and evicts cold pages from the tail of the inactive list.
- **Scan pollution:** A large sequential scan fills the inactive list and pushes out hot working-set pages before they can be promoted. This is the root cause of noisy-neighbor interference.
- **No tenant awareness:** All pages on the inactive list compete equally — no notion of per-tenant priority.

### How cgroups Are Applied Per Process

Memory cgroup (memcg) tracking is wired into the page fault and page cache insertion paths ([`mm/memcontrol.c`](https://elixir.bootlin.com/linux/latest/source/mm/memcontrol.c)):

1. **Charge on first access:** Each page is charged to the cgroup of the faulting process and retains that association until eviction.
2. **Per-cgroup LRU lists:** Each cgroup maintains its own inactive/active LRU (`mem_cgroup_lruvec()`). The global shrinker walks per-cgroup LRU vectors proportionally under memory pressure.
3. **`memory.low` / `memory.min` influence eviction priority:** `shrink_lruvec()` skips cgroups below their `memory.low` or `memory.min` thresholds — the *only* place tenant identity influences eviction order.
4. **Shared file pages:** A file-backed page is charged to the first-accessing cgroup; subsequent readers benefit without being charged.

### Why cgroup v2 Knobs Are Insufficient

| Knob | What it does | Why it's insufficient |
|---|---|---|
| `memory.min` | Hard-protect pages below threshold | Doesn't protect above the threshold; OOM-kills if nothing else is reclaimable |
| `memory.low` | Soft-deprioritize victim's pages for eviction | Advisory only; fails under heavy global pressure |
| `memory.high` | Throttle allocations above threshold | Throttles throughput, doesn't control *who* gets evicted; indirect effect on dirty pages — exceeding it forces reclaim from that cgroup, but not targeted dirty throttling |
| `memory.max` | Hard cap; OOM-kills if exceeded | Blunt — kills the noisy neighbor rather than gracefully degrading it |

**The key asymmetry:** `memory.low` protects the victim's pages but does nothing to penalize the noisy neighbor.

**The dirty/clean blind spot:** All four knobs count *total* file-backed pages — clean and dirty together. `memory.stat` exposes `file_dirty` per cgroup, but there is no per-cgroup *throttle* on dirty page accumulation (only global `vm.dirty_background_ratio` / `vm.dirty_ratio`). Clean-page eviction is free; dirty-page eviction requires writeback I/O that consumes disk bandwidth shared with other tenants' reads.

**Standard practice** ([Biriukov](https://biriukov.dev/docs/page-cache/6-cgroup-v2-and-page-cache/), [Kubernetes etcd guide](https://kubernetes.io/docs/tasks/administer-cluster/configure-upgrade-etcd/)): set `memory.low ≈ observed working set`, `memory.high ≈ 90% of container limit`, `memory.max` as hard ceiling. No universal percentages — requires monitoring actual usage.

### Dynamic / Adaptive cgroup Configuration

Rather than static limits, several systems use runtime PSI feedback:

**PSI as a feedback signal.** Linux 4.20+ exposes `/sys/fs/cgroup/<path>/memory.pressure` per cgroup. Subscribe to threshold-crossing events via `eventfd`:
```
# fire when memory stall exceeds 50ms in any 500ms window
echo "some 50000 500000" > /sys/fs/cgroup/tenant_a/memory.pressure
```

**Senpai (Meta):** Monitors `memory.pressure`; lowers `memory.high` when pressure is below target (page out cold memory) and raises it when pressure rises (give breathing room). Deployed across millions of Meta servers; 20–32% memory savings ([TMO, ASPLOS '22](https://www.cs.cmu.edu/~dskarlat/publications/tmo_asplos22.pdf)).

**WSS estimation for `memory.low` sizing:** [Cuki (ATC '23)](https://www.usenix.org/system/files/atc23-gu.pdf) and [eBPF-based WSS estimation](https://arxiv.org/pdf/2303.05919) estimate per-cgroup working set size online, enabling automatic `memory.low` sizing.

**MRC-guided partitioning:** [mPart (ISMM '18)](https://dl.acm.org/doi/10.1145/3210563.3210571) constructs online miss-ratio curves per tenant and solves for the near-optimal allocation split.

**What adaptive tuning still cannot do.** PSI and Senpai fail in two specific ways relevant to this project:

- **PSI fires for the right symptom but triggers the wrong fix.** When B evicts A's pages and A faults them back in, A's `memory.pressure` and `io.pressure` both rise. Senpai responds by resizing memory limits. But if B's dirty page flushes are still in the I/O queue, A's reads remain delayed even after memory limits are corrected. The fix is incomplete because it does not address the I/O queue contention.
- **PSI cannot attribute I/O queue delay to its source cgroup.** A's `io.pressure` rises whether its reads are delayed by its own workload or by B's dirty writes queued ahead. There is no kernel mechanism to say "X% of A's io.pressure is attributable to B's writeback." ⚠️ *Needs validation: measure A's p99 under (a) B clean-reads only, (b) B dirty-writes only — to quantify how much the dirty component adds beyond what memory resizing fixes.*
- **No per-cgroup dirty throttle targets the I/O queue.** `memory.high` reduces dirty accumulation indirectly; but there is no knob that deprioritizes B's writes vs. A's reads. That control sits in the blkio/io cgroup controller (`io.weight`, `io.latency`) — a separate control plane that existing memory isolation tools do not coordinate with.

---

## Experiments

### Setup
- **Tenant A (victim):** random-read KV workload; working set fits in cache (e.g., 3 GB on an 8 GB machine); measure p50/p99/p999
- **Tenant B (noisy neighbor):** two variants — see below

### B Variants: Isolating the Two Interference Mechanisms

| B workload | Pages generated | Mechanism tested |
|---|---|---|
| `fio --rw=read --bs=1M` (sequential scan) | Clean pages | **Mechanism 1:** B fills LRU → A's pages evicted → A reads from disk. No writeback contention. |
| `fio --rw=write --bs=4k` (random write) | Dirty pages | **Mechanism 2:** B evicts A's pages AND generates dirty pages → A's reads compete with B's flushes in the I/O queue. |
| `fio --rw=readwrite` (mixed) | Both | Combined effect |

⚠️ *If A's p99 is significantly higher under B-writes than B-reads at the same eviction rate, that confirms dirty writeback amplifies miss penalties beyond what memory sizing alone fixes.*

### Four Isolation Conditions (run for each B variant)

| Condition | Setup | Expected outcome |
|---|---|---|
| **Baseline** | A alone | p99 flat; cache-hit ratio ≈ 100%; negligible disk I/O |
| **Interference** | A + B, no isolation | A's p99 spikes; B-write case expected to show higher p99 than B-read at same eviction rate |
| **cgroup v2** | A's `memory.low` = working set size, no hard cap on B | Partial recovery; B-read case may largely recover; B-write case expected to have residual p99 elevation — the "existing mechanisms insufficient" result |
| **Proposed policy** | A + B under proposed eviction policy | A's p99 bounded within SLO for both B variants; B degrades gracefully |

### Tools
- `fio --rw=randread --bs=4k` (A); `fio --rw=read --bs=1M` or `--rw=write --bs=4k` (B)
- Reset between runs: `echo 3 > /proc/sys/vm/drop_caches`
- Key metrics:
  - `/proc/vmstat`: `pgpgin`, `nr_dirty`, `nr_writeback`, `pgscan_kswapd`
  - Per-cgroup: `cat /sys/fs/cgroup/tenant_b/memory.stat | grep file_dirty`
  - I/O queue: `iostat -x 1`, `blktrace` for per-operation read vs. write latency
  - eBPF: probe `submit_bio` per cgroup to attribute disk reads vs. writes; measure time-in-queue vs. time-on-device

### Key Figures
- **Fig 1 (motivation cliff):** X = B's scan intensity; Y = A's p99; two curves (B-reads vs. B-writes) to show the dirty component
- **Fig 2:** Latency CDF under all four isolation conditions — shows tail behavior
- **Fig 3:** A's `pgpgin` rate (miss rate) vs. B's `nr_writeback` rate — shows the two contributing factors

---

## Implementation Approaches

| Approach | How | Iteration speed | Kernel rebuild? | Best for |
|---|---|---|---|---|
| **Full kernel mod** | Modify `mm/vmscan.c`, `mm/memcontrol.c`; add per-cgroup data structures | 🔴 Slow (10–30 min/cycle) | Yes, often | Novel policy needing new kernel data structures |
| **Kernel module** | `register_shrinker()` + kprobes | 🟢 Fast (seconds, `rmmod`/`insmod`) | No | Observability only — hits a wall because `shrink_lruvec` is not `EXPORT_SYMBOL`'d |
| **eBPF via cache_ext** | Apply cache_ext patch set once; implement policy as BPF program | 🟢 Fast (seconds) | Once | Novel policy + "deployable" story; no recompile after initial setup |
| **Userspace + cgroup v2** | Tune `memory.low/min/high/max`, `posix_fadvise`, `mlock` | 🟢 Very fast (hours) | Never | Baseline experiments only; establishes inadequacy of existing tools |

**eBPF vs. full kernel mod:** cache_ext is the cleaner contribution story (deployable, safe, no recompile after one-time patch application), but expressiveness is bounded by existing hook points. Full kernel mod gives unlimited control but requires a VM and slow iteration. If the policy needs new per-page metadata (e.g., per-tenant I/O timestamps), full kernel mod is necessary.

---

## Novelty

**The gap:** Delta Fair Sharing identifies OS page cache interference as an open problem — they do not implement a solution. Existing tools (cgroup limits, PSI/Senpai) can reduce how much B evicts A but cannot bound p99 because they miss the second interference component: B's dirty-page flushes inflating A's per-miss I/O penalty.

**Two-component model of p99 latency spike:**

```
A's p99 spike = A's cache miss rate × per-miss disk read latency
                              where per-miss latency = baseline_read + writeback_queue_delay(B)
```

Memory sizing tools (Senpai, cgroup limits) address only the first factor. They cannot reduce `writeback_queue_delay(B)` — that is an I/O scheduler problem, not a memory sizing problem.

**Why prior work misses the second factor:**
| System | What it controls | What it cannot address |
|---|---|---|
| `memory.low` / `memory.min` | Total page allocation per cgroup | Per-miss I/O latency inflation from concurrent writeback |
| PSI / Senpai | Memory reclaim throttling via stall feedback | I/O queue contention attribution; no per-cgroup dirty-page rate limit |
| Delta Fair Sharing | RocksDB-layer write buffer + block cache fairness | OS page cache path; dirty page flush scheduling at block layer |
| LRU / cache_ext policies | Eviction ordering by recency | Dirty/clean status not an eviction criterion; no I/O layer coordination |

**Differentiators:**
1. **Dirty pages as an eviction criterion** — prefer evicting B's dirty pages first (force earlier writeback at lower cache pressure) rather than waiting until global dirty limits force a burst; distributes writeback more evenly over time rather than concentrating it into spikes
2. **Cross-layer coordination** — connect memory controller (observes `file_dirty` per cgroup) with io cgroup controller (`io.weight`, `io.latency`) to deprioritize B's writes vs. A's reads
3. **etcd/bbolt as clean case study** — no internal block cache, so OS page cache is the only caching layer; dirty writeback path is directly observable
4. **p99 as the direct objective** — target SLO bounds, not fairness (cf. RobinHood)

**Draft contribution statement** *(contingent on experimental validation of the two-component model)*:
> *"We show that p99 read latency spikes in co-located multi-tenant KV stores have two components: cache eviction (addressed by prior work) and dirty-page writeback contention (unaddressed). Existing isolation mechanisms including cgroup memory limits, PSI-based adaptive tools, and Delta Fair Sharing reduce eviction pressure but cannot bound the per-miss latency inflation caused by concurrent dirty-page flushes. We present [name], which [preferentially evicts B's dirty pages early / coordinates memory and I/O cgroup controllers] to bound p99 within SLOs for co-located readers."*

---

## Repository Status

The benchmark infrastructure in this repo has the right skeleton. Here is where it currently stands against the experiment plan above.

### What works
- Dual-client concurrent execution (`fairness_benchmark.cpp`) — correct foundation
- Multi-phase workload with per-second fio logging (IOPS, p99, max latency)
- fio JSON output captures `clat_ns.percentile.99` — the right metric
- cgroup v2 integration (both shared and isolated config files present)
- Direct I/O mode as a "no page cache" reference baseline
- iostat monitoring alongside fio

### Current validation status

| Experiment condition | Status |
|---|---|
| Condition 1 — A alone | ✅ Single-workload `cached` runs cover this |
| Condition 2 — A + B, no isolation | ⚠️ `dual` mode exists but B is a pure **reader** — only tests clean-page eviction, not dirty writeback |
| Condition 3 — cgroup v2 soft protection | ❌ **Config is wrong:** `cgroup_isolated.ini` sets `memory.min = memory.max = memory.high = 1G` — this hard-partitions memory and shows isolation *working*, the opposite of the argument needed |
| Fig 1 motivation cliff (sweep B intensity) | ❌ B's `rate_iops` is fixed; no intensity sweep |
| Dirty writeback hypothesis | ❌ B never writes; `nr_dirty`/`nr_writeback` never instrumented |

### What needs to change

1. **Add a B-writer config** in `fairness_configs.ini` with `pattern=write` or `randwrite`. Run A+B-reader vs. A+B-writer at the same eviction rate; if A's p99 is higher under B-writer, that validates the dirty writeback amplification claim.
2. **Add an A-alone baseline run** (`client1_alone`, no B) and use that delta as the interference measurement.
3. **Fix the cgroup v2 condition:** replace hard partition with `memory.low = working_set_size` (soft floor, no hard cap on B); sweep B intensity; show A's p99 still violates SLO.
4. **Fix phase result merging** (line 578–597 of `fairness_benchmark.cpp`): currently copies only last phase's JSON; preserve all `_phase{N}.json` and plot as time series.
5. **Add a sweep driver script:** run `dual` with varying `phase_1_rate_iops` for B to produce Fig 1.
6. **Log dirty page metrics:** add `/proc/vmstat` (`nr_dirty`, `nr_writeback`) and per-cgroup `file_dirty` at 1-second intervals.
7. **Change the fairness metric:** replace IOPS ratio with fraction of A's requests exceeding a latency SLO (e.g., 2× A's baseline p99).

See [`BENCHMARK.md`](BENCHMARK.md) for build and run instructions.

---

## Open Questions

1. **Isolation unit:** cgroup, Kubernetes namespace, or application-defined tenant ID?
2. **Latency measurement:** How to observe per-tenant read latency as an eviction signal — inside the kernel (hard) or via a userspace feedback loop (simpler, but laggy)?
3. **Application transparency:** Work with unmodified KV stores, or allow applications to annotate file regions with priority hints?
4. **Block cache interaction:** Focus on bbolt/etcd (no internal block cache, cleanest case) or handle RocksDB's dual-cache complexity too?
5. **Workload distribution:** Uniform random vs. Zipfian — the hot working set is much smaller under Zipfian, which changes how much cache protection matters.
6. **Device type:** On NVMe SSDs, read/write parallelism is higher than HDDs — the dirty writeback amplification effect may vary significantly. Experiments should test on the target device class.

---

## Roadmap

| Phase | Work | Time |
|---|---|---|
| 1 — Characterize | Fix B-writer config; add A-alone baseline; fix cgroup v2 condition; add dirty page instrumentation; produce Figs 1–3 | 2–3 weeks |
| 2 — Design | Read cache_ext + RobinHood closely; finalize novelty angle; pick implementation approach (eBPF vs. kernel mod) | 1 week |
| 3 — Implement | eBPF policy (cache_ext) or full kernel mod; per-cgroup eviction + I/O coordination logic | 4–6 weeks |
| 4 — Evaluate | All 4 conditions; vary tenant count + pressure; compute p99, SLO violation rate, Jain fairness index | 2–3 weeks |
| 5 — Write | Motivation → background → design → impl → evaluation | 2–3 weeks |

---

## Contact

**Soujanya Ponnapalli** — soujanya@berkeley.edu

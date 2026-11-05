/*
 * benchmark.c — Page-Cache Fairness Benchmark harness.
 *
 * Pairs a latency-sensitive victim (Tenant A / client1_steady) against a noisy
 * neighbor (Tenant B / client2_noisy) under cgroup v2 and measures A's p99
 * read latency, decomposing the spike into:
 *   Mechanism 1 — LRU eviction / refaults (clean scan)
 *   Mechanism 2 — eviction + dirty writeback contention (buffered writer)
 *
 * This file drives fio, sets up cgroups, samples pressure/dirty-page state,
 * snapshots per-cgroup memory.stat + io.stat before/after each phase, and
 * records 1 Hz timeseries (client1/client2 clat percentiles, cgroup memory/PSI).
 *
 * Measurement hygiene: memory.stat:file counts *all* page cache charged to the
 * tenant, including files the harness itself writes (fio logs). The target
 * test file's own residency is therefore measured separately via mincore(2)
 * (target_file_resident_bytes) and cachestat(2) nr_cache
 * (target_file_nr_cache_bytes, Linux 6.5+), and fio latency is logged as 1 s
 * rather than per-IO samples, so logging does not consume the memory domain
 * memory.low / memory.min are supposed to protect.
 *
 * Optional bpftrace debug: entry counts (psi_memstall_enter + sched_switch
 *   in_iowait), on by default; disable with --no-bpftrace. Primary PSI metrics
 *   remain memory.pressure / io.pressure duration deltas; entry counts are
 *   not miss rates. Skipped cleanly if bpftrace/root/BTF are unavailable.
 *
 * Platform: cgroups, PSI, memory.stat, and /proc/vmstat require Linux
 * (cgroup v2). macOS is build-only; the sampler/cgroup paths no-op there.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <stdbool.h>
#include <ctype.h>
#include <errno.h>
#include <unistd.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <dirent.h>
#include <time.h>
#include <math.h>
#include <limits.h>
#include <stdint.h>
#include <sys/syscall.h>

#define MAX_PHASES        16
#define MAX_CLIENTS       16
#define MAX_CGROUPS       16
#define MAX_STR           256
#define MAX_CMD           4096
#define SAMPLE_INTERVAL_S 1

/* ------------------------------------------------------------------ *
 * Configuration model
 * ------------------------------------------------------------------ */

typedef struct {
    bool  present;                    /* was this phase defined at all?      */
    char  pattern[MAX_STR];           /* randread/read/randwrite/write/randrw*/
    char  block_size[MAX_STR];        /* e.g. 4k, 1M                         */
    char  ioengine[MAX_STR];          /* libaio, sync, ...                   */
    int   rate_iops;                  /* 0 = unlimited                       */
    int   iodepth;
    int   numjobs;
    int   runtime;                    /* seconds                             */
    int   rwmixread;                  /* for randrw; -1 = unset              */

    /* read a fixed amount then stop (not time_based); "" = unset */
    char  io_size[MAX_STR];           /* e.g. 1G -> fio --io_size            */
    char  rate_bw[MAX_STR];           /* bandwidth cap e.g. 100m -> --rate   */

    /* victim access-distribution skew */
    char  random_distribution[MAX_STR]; /* "" or e.g. "zipf:1.2"             */

    /* flush cadence for checkpoint / WAL B variants */
    int   fdatasync;                  /* fdatasync every N ops; 0 = unset    */
    int   fsync;                      /* fsync every N ops; 0 = unset        */
} PhaseConfig;

typedef struct {
    char        name[MAX_STR];
    char        description[MAX_STR];
    char        file_size[MAX_STR];   /* e.g. 1G, 8G                         */
    int         num_phases;
    PhaseConfig phases[MAX_PHASES];
} ClientConfig;

typedef struct {
    char cgroup_name[MAX_STR];        /* section key -> cgroup leaf name     */
    char section[MAX_STR];            /* ini section name                    */
    char memory_max[MAX_STR];         /* "" if unset                         */
    char memory_low[MAX_STR];         /* "" if unset                         */
    char memory_min[MAX_STR];         /* "" if unset                         */
    char io_weight[MAX_STR];          /* "" if unset                         */
} CgroupConfig;

typedef struct {
    ClientConfig clients[MAX_CLIENTS];
    int          num_clients;
} Config;

/* ------------------------------------------------------------------ *
 * Global options
 * ------------------------------------------------------------------ */

typedef enum { MODE_BOTH, MODE_CACHED, MODE_DIRECT } CacheMode;

static struct {
    const char *config_path;
    const char *cgroup_config_path;
    const char *output_dir;
    CacheMode   mode;
    bool        verbose;
    bool        use_cgroups;
    bool        use_psi;
    bool        use_bpftrace;      /* debug: memstall/iowait entry counts (default on) */
    bool        drop_once;         /* drop page cache before phase 0 only, then let it persist */
    bool        write_clat_log;    /* fio write_lat_log for client1+client2 → 1 Hz p99 */
    bool        use_timeseries;    /* 1 Hz cgroup + clat logs */
    bool        use_senpai_high;   /* fork senpai_high.py after phase 0 */
    const char *senpai_script;     /* path to senpai_high.py */
    const char *senpai_client;     /* cgroup whose memory.high Senpai sizes */
    long        senpai_min_size;
    long        senpai_max_size;
    int         senpai_interval;
    int         senpai_pressure;
} opt = {
    .config_path        = "fairness_configs.ini",
    .cgroup_config_path = NULL,
    .output_dir         = "benchmark_results",
    .mode               = MODE_BOTH,
    .verbose            = false,
    .use_cgroups        = true,
    .use_psi            = true,
    .use_bpftrace       = true,
    .drop_once          = false,
    .write_clat_log     = true,
    .use_timeseries     = true,
    .use_senpai_high    = false,
    .senpai_script      = "./senpai_high.py",
    .senpai_client      = "client2_noisy",
    .senpai_min_size    = 100L << 20,
    .senpai_max_size    = 2L << 30,
    .senpai_interval    = 6,
    .senpai_pressure    = 10000,
};

#define VLOG(...) do { if (opt.verbose) { fprintf(stderr, "[v] " __VA_ARGS__); fprintf(stderr, "\n"); } } while (0)
#define INFO(...) do { fprintf(stderr, __VA_ARGS__); fprintf(stderr, "\n"); } while (0)

static const char CGROUP_ROOT[] = "/sys/fs/cgroup";

/* ------------------------------------------------------------------ *
 * Small helpers
 * ------------------------------------------------------------------ */

static char *trim(char *s) {
    /* trim leading and trailing whitespace */
    while (*s && isspace((unsigned char)*s)) s++;
    if (!*s) return s;
    char *end = s + strlen(s) - 1;
    while (end > s && isspace((unsigned char)*end)) *end-- = '\0';
    return s;
}

static bool is_linux(void) {
    /* check if the platform is Linux (required for cgroup v2) */
#ifdef __linux__
    return true;
#else
    return false;
#endif
}

static void ensure_dir(const char *path) {
    /* create the directory if it doesn't exist */
    if (!path || !*path) return;

    char buf[MAX_CMD];
    if (snprintf(buf, sizeof(buf), "%s", path) >= (int)sizeof(buf)) {
        INFO("warning: mkdir path too long: %s", path);
        return;
    }

    size_t len = strlen(buf);
    if (len > 1 && buf[len - 1] == '/')
        buf[len - 1] = '\0';

    for (char *p = buf + 1; *p; p++) {
        if (*p != '/') continue;
        *p = '\0';
        if (mkdir(buf, 0755) != 0 && errno != EEXIST)
            INFO("warning: mkdir %s: %s", buf, strerror(errno));
        *p = '/';
    }
    if (mkdir(buf, 0755) != 0 && errno != EEXIST)
        INFO("warning: mkdir %s: %s", buf, strerror(errno));
}

/* mkdir -p for a subdir under the results dir */
static void ensure_subdir(const char *base, const char *sub, char *out, size_t n) {
    /* builds base/sub and creates the directory into out if it doesn't exist */
    snprintf(out, n, "%s/%s", base, sub);
    ensure_dir(out);
}

/* Flatten cgroup path for result filenames (clients/a -> clients_a). */
static void cgroup_file_label(const char *cg, char *out, size_t n) {
    size_t j = 0;
    for (size_t i = 0; cg[i] && j + 1 < n; i++) {
        char c = cg[i];
        if (c == '/') c = '_';
        out[j++] = c;
    }
    out[j] = '\0';
}

/* ------------------------------------------------------------------ *
 * INI parsing
 * ------------------------------------------------------------------ */

/* Route a "phase_N_key" or bare key onto the right PhaseConfig field. */
static void apply_phase_key(PhaseConfig *p, const char *key, const char *val) {
    if      (!strcmp(key, "pattern"))             snprintf(p->pattern, MAX_STR, "%s", val);
    else if (!strcmp(key, "block_size"))          snprintf(p->block_size, MAX_STR, "%s", val);
    else if (!strcmp(key, "ioengine"))            snprintf(p->ioengine, MAX_STR, "%s", val);
    else if (!strcmp(key, "rate_iops"))           p->rate_iops = atoi(val);
    else if (!strcmp(key, "iodepth"))             p->iodepth   = atoi(val);
    else if (!strcmp(key, "numjobs"))             p->numjobs   = atoi(val);
    else if (!strcmp(key, "runtime"))             p->runtime   = atoi(val);
    else if (!strcmp(key, "rwmixread"))           p->rwmixread = atoi(val);
    else if (!strcmp(key, "io_size"))             snprintf(p->io_size, MAX_STR, "%s", val);
    else if (!strcmp(key, "rate_bw"))             snprintf(p->rate_bw, MAX_STR, "%s", val);
    /* victim access-distribution skew */
    else if (!strcmp(key, "random_distribution")) snprintf(p->random_distribution, MAX_STR, "%s", val);
    /* flush cadence for checkpoint / WAL B variants */
    else if (!strcmp(key, "fdatasync"))           p->fdatasync = atoi(val);
    else if (!strcmp(key, "fsync"))               p->fsync     = atoi(val);
    else VLOG("unknown phase key '%s' (ignored)", key);
    p->present = true;
}

static void init_phase(PhaseConfig *p) {
    /* default values */
    memset(p, 0, sizeof(*p));
    p->rwmixread = -1;
    p->iodepth   = 1;
    p->numjobs   = 1;
    p->runtime   = 60;
    snprintf(p->pattern, MAX_STR, "randread");
    snprintf(p->block_size, MAX_STR, "4k");
    snprintf(p->ioengine, MAX_STR, "libaio");
}

static ClientConfig *find_or_add_client(Config *cfg, const char *name) {
    for (int i = 0; i < cfg->num_clients; i++)
    /* check if the client name already exists and return the client config */
    if (!strcmp(cfg->clients[i].name, name)) return &cfg->clients[i];
    if (cfg->num_clients >= MAX_CLIENTS) {
        INFO("error: too many client sections (max %d)", MAX_CLIENTS);
        exit(1);
    }
    /* add the new client config */
    ClientConfig *c = &cfg->clients[cfg->num_clients++];
    memset(c, 0, sizeof(*c));
    snprintf(c->name, MAX_STR, "%s", name);
    for (int i = 0; i < MAX_PHASES; i++) init_phase(&c->phases[i]);
    return c;
}

static bool parse_config(const char *path, Config *cfg) {
    FILE *f = fopen(path, "r");
    if (!f) { INFO("error: cannot open config '%s': %s", path, strerror(errno)); return false; }

    memset(cfg, 0, sizeof(*cfg));
    char line[1024];
    ClientConfig *cur = NULL;

    while (fgets(line, sizeof(line), f)) {
        char *s = trim(line);
        if (!*s || *s == '#' || *s == ';') continue;

        if (*s == '[') {
            char *end = strchr(s, ']');
            if (!end) continue;
            *end = '\0';
            cur = find_or_add_client(cfg, s + 1);
            continue;
        }
        if (!cur) continue;

        char *eq = strchr(s, '=');
        if (!eq) continue;
        *eq = '\0';
        char *key = trim(s);
        char *val = trim(eq + 1);
        /* strip inline comments */
        char *sc = strchr(val, ';'); if (sc) { *sc = '\0'; val = trim(val); }

        if (!strcmp(key, "description")) { snprintf(cur->description, MAX_STR, "%s", val); continue; }
        if (!strcmp(key, "file_size"))   { snprintf(cur->file_size, MAX_STR, "%s", val); continue; }

        if (!strncmp(key, "phase_", 6)) {
            int idx = atoi(key + 6);
            char *us = strchr(key + 6, '_');
            if (!us || idx < 0 || idx >= MAX_PHASES) { VLOG("bad phase key '%s'", key); continue; }
            apply_phase_key(&cur->phases[idx], us + 1, val);
            if (idx + 1 > cur->num_phases) cur->num_phases = idx + 1;
        } else {
            /* non-phased single-phase shorthand -> phase 0 */
            apply_phase_key(&cur->phases[0], key, val);
            if (cur->num_phases < 1) cur->num_phases = 1;
        }
    }
    fclose(f);
    return true;
}

static ClientConfig *find_client(Config *cfg, const char *name) {
    for (int i = 0; i < cfg->num_clients; i++)
        if (!strcmp(cfg->clients[i].name, name)) return &cfg->clients[i];
    return NULL;
}

/* ------------------------------------------------------------------ *
 * cgroup config parsing (separate ini)
 * ------------------------------------------------------------------ */

typedef struct { CgroupConfig groups[MAX_CGROUPS]; int n; } CgroupSet;

static bool parse_cgroup_config(const char *path, CgroupSet *set) {
    FILE *f = fopen(path, "r");
    if (!f) { INFO("error: cannot open cgroup config '%s': %s", path, strerror(errno)); return false; }

    memset(set, 0, sizeof(*set));
    char line[1024];
    CgroupConfig *cur = NULL;

    while (fgets(line, sizeof(line), f)) {
        char *s = trim(line);
        if (!*s || *s == '#' || *s == ';') continue;
        if (*s == '[') {
            char *end = strchr(s, ']'); if (!end) continue; *end = '\0';
            if (set->n >= MAX_CGROUPS) { INFO("too many cgroups"); break; }
            cur = &set->groups[set->n++];
            memset(cur, 0, sizeof(*cur));
            snprintf(cur->section, MAX_STR, "%s", s + 1);
            snprintf(cur->cgroup_name, MAX_STR, "%s", s + 1); /* default = section */
            continue;
        }
        if (!cur) continue;
        char *eq = strchr(s, '='); if (!eq) continue; *eq = '\0';
        char *key = trim(s), *val = trim(eq + 1);
        char *sc = strchr(val, ';'); if (sc) { *sc = '\0'; val = trim(val); }
        if      (!strcmp(key, "cgroup_name")) snprintf(cur->cgroup_name, MAX_STR, "%s", val);
        else if (!strcmp(key, "memory.max"))  snprintf(cur->memory_max, MAX_STR, "%s", val);
        else if (!strcmp(key, "memory.low"))  snprintf(cur->memory_low, MAX_STR, "%s", val);
        else if (!strcmp(key, "memory.min"))  snprintf(cur->memory_min, MAX_STR, "%s", val);
        else if (!strcmp(key, "io.weight"))   snprintf(cur->io_weight, MAX_STR, "%s", val);
    }
    fclose(f);
    return true;
}

static void write_cgroup_file(const char *cg_path, const char *file, const char *val) {
    if (!val || !*val) return;
    char p[MAX_CMD];
    snprintf(p, sizeof(p), "%s/%s", cg_path, file);
    FILE *f = fopen(p, "w");
    if (!f) { INFO("warning: open %s: %s", p, strerror(errno)); return; }
    fprintf(f, "%s\n", val);
    if (fclose(f) != 0) INFO("warning: write %s: %s", p, strerror(errno));
    VLOG("set %s = %s", p, val);
}

/* Enable the memory + io controllers in a cgroup's subtree_control so that
 * its children expose memory.max / io.weight etc. Best-effort: on many hosts
 * systemd has already delegated these, and a redundant write is harmless. */
static void enable_controllers(const char *cg_path) {
    char p[MAX_CMD];
    snprintf(p, sizeof(p), "%s/cgroup.subtree_control", cg_path);
    FILE *f = fopen(p, "w");
    if (!f) { VLOG("subtree_control %s not writable: %s", p, strerror(errno)); return; }
    fprintf(f, "+memory +io\n");
    if (fclose(f) != 0) VLOG("subtree_control %s write: %s", p, strerror(errno));
    else VLOG("enabled +memory +io in %s", p);
}

/* Count '/' separators so we can destroy deepest leaves before parents. */
static int cgroup_rel_depth(const char *rel) {
    int d = 0;
    for (const char *p = rel; *p; p++)
        if (*p == '/') d++;
    return d;
}

/* Move every member of cg_path into its parent cgroup so rmdir can succeed. */
static void evacuate_cgroup_procs(const char *cg_path) {
    char procs_path[MAX_CMD], parent[MAX_CMD], parent_procs[MAX_CMD];
    if (snprintf(procs_path, sizeof(procs_path), "%s/cgroup.procs", cg_path) >= (int)sizeof(procs_path))
        return;
    if (snprintf(parent, sizeof(parent), "%s", cg_path) >= (int)sizeof(parent))
        return;
    char *slash = strrchr(parent, '/');
    if (!slash || slash == parent) return; /* refuse to evacuate into / */
    *slash = '\0';
    if (snprintf(parent_procs, sizeof(parent_procs), "%s/cgroup.procs", parent) >= (int)sizeof(parent_procs))
        return;

    FILE *f = fopen(procs_path, "r");
    if (!f) return;

    int pids[256];
    int n = 0;
    int pid;
    while (n < (int)(sizeof(pids) / sizeof(pids[0])) && fscanf(f, "%d", &pid) == 1)
        pids[n++] = pid;
    fclose(f);

    for (int i = 0; i < n; i++) {
        FILE *w = fopen(parent_procs, "w");
        if (!w) {
            INFO("warning: cannot move pid %d out of %s: %s", pids[i], cg_path, strerror(errno));
            continue;
        }
        if (fprintf(w, "%d\n", pids[i]) < 0)
            INFO("warning: move pid %d out of %s: %s", pids[i], cg_path, strerror(errno));
        fclose(w);
        VLOG("moved pid %d from %s -> %s", pids[i], cg_path, parent);
    }
}

/* Recursively delete a cgroup directory (children first). Resets lifetime
 * counters such as workingset_refault_file for the next run. */
static bool remove_cgroup_recursive(const char *cg_path) {
    struct stat st;
    if (stat(cg_path, &st) != 0) {
        if (errno == ENOENT) return true;
        INFO("error: stat %s: %s", cg_path, strerror(errno));
        return false;
    }
    if (!S_ISDIR(st.st_mode)) return true;

    DIR *d = opendir(cg_path);
    if (!d) {
        INFO("error: opendir %s: %s", cg_path, strerror(errno));
        return false;
    }
    struct dirent *ent;
    bool ok = true;
    while ((ent = readdir(d)) != NULL) {
        if (!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) continue;
        char child[MAX_CMD];
        if (snprintf(child, sizeof(child), "%s/%s", cg_path, ent->d_name) >= (int)sizeof(child)) {
            INFO("error: path too long under %s", cg_path);
            ok = false;
            break;
        }
        if (stat(child, &st) == 0 && S_ISDIR(st.st_mode)) {
            if (!remove_cgroup_recursive(child)) ok = false;
        }
    }
    closedir(d);
    if (!ok) return false;

    evacuate_cgroup_procs(cg_path);
    if (rmdir(cg_path) != 0) {
        if (errno == ENOENT) return true;
        INFO("error: rmdir %s: %s (is a process still in this cgroup?)", cg_path, strerror(errno));
        return false;
    }
    INFO("removed stale cgroup %s", cg_path);
    return true;
}

/* Tear down configured cgroups (deepest first) so the next create starts with
 * fresh memory.stat counters. */
static bool destroy_cgroups(const CgroupSet *set) {
    int order[MAX_CGROUPS];
    for (int i = 0; i < set->n; i++) order[i] = i;
    /* deepest relative paths first */
    for (int i = 0; i < set->n; i++) {
        for (int j = i + 1; j < set->n; j++) {
            if (cgroup_rel_depth(set->groups[order[j]].cgroup_name) >
                cgroup_rel_depth(set->groups[order[i]].cgroup_name)) {
                int tmp = order[i]; order[i] = order[j]; order[j] = tmp;
            }
        }
    }

    bool ok = true;
    for (int k = 0; k < set->n; k++) {
        const CgroupConfig *g = &set->groups[order[k]];
        char path[MAX_CMD];
        if (snprintf(path, sizeof(path), "%s/%s", CGROUP_ROOT, g->cgroup_name) >= (int)sizeof(path)) {
            INFO("error: cgroup path too long: %s/%s", CGROUP_ROOT, g->cgroup_name);
            ok = false;
            continue;
        }
        if (!remove_cgroup_recursive(path)) ok = false;
    }
    return ok;
}

/* mkdir -p for a cgroup path under CGROUP_ROOT, enabling controllers on each
 * intermediate directory top-down (a controller can only be delegated by a
 * parent that already has it). `rel` is the path relative to CGROUP_ROOT. */
static void mkdir_p_cgroup(const char *rel) {
    /* root first: delegate memory+io down into our tree */
    enable_controllers(CGROUP_ROOT);

    char acc[MAX_CMD];
    snprintf(acc, sizeof(acc), "%s", CGROUP_ROOT);

    char work[MAX_STR];
    snprintf(work, sizeof(work), "%s", rel);
    char *save = NULL;
    for (char *tok = strtok_r(work, "/", &save); tok; ) {
        size_t len = strlen(acc);
        snprintf(acc + len, sizeof(acc) - len, "/%s", tok);
        if (mkdir(acc, 0755) != 0 && errno != EEXIST)
            INFO("warning: mkdir cgroup %s: %s", acc, strerror(errno));
        char *next = strtok_r(NULL, "/", &save);
        /* enable controllers on every non-leaf dir so children get the files */
        if (next) enable_controllers(acc);
        tok = next;
    }
}

/* Destroy any leftover cgroups from prior runs, then recreate and apply limits.
 * Fresh cgroup directories reset lifetime counters (e.g. workingset_refault_file). */
static bool setup_cgroups(const CgroupSet *set) {
    if (!is_linux()) { INFO("note: not Linux — skipping cgroup setup"); return true; }

    INFO("resetting cgroups from config (delete + recreate to clear stale counters)");
    if (!destroy_cgroups(set)) {
        INFO("error: failed to remove stale cgroups — kill leftover fio/benchmark "
             "processes and retry");
        return false;
    }

    for (int i = 0; i < set->n; i++) {
        const CgroupConfig *g = &set->groups[i];
        mkdir_p_cgroup(g->cgroup_name);
        char path[MAX_CMD];
        snprintf(path, sizeof(path), "%s/%s", CGROUP_ROOT, g->cgroup_name);
        write_cgroup_file(path, "memory.max", g->memory_max);
        write_cgroup_file(path, "memory.low", g->memory_low);
        write_cgroup_file(path, "memory.min", g->memory_min);
        write_cgroup_file(path, "io.weight",  g->io_weight);
    }
    return true;
}

static const CgroupConfig *cgroup_for_client(const CgroupSet *set, const char *client) {
    for (int i = 0; i < set->n; i++)
        if (!strcmp(set->groups[i].section, client)) return &set->groups[i];
    return NULL;
}

/* ------------------------------------------------------------------ *
 * memory.stat snapshots + workingset_refault_file delta
 * ------------------------------------------------------------------ */

static long read_memstat_field(const char *cgroup_name, const char *field) {
    if (!is_linux()) return -1;
    char p[MAX_CMD];
    snprintf(p, sizeof(p), "%s/%s/memory.stat", CGROUP_ROOT, cgroup_name);
    FILE *f = fopen(p, "r");
    if (!f) return -1;
    char key[128]; long val;
    long found = -1;
    while (fscanf(f, "%127s %ld", key, &val) == 2)
        if (!strcmp(key, field)) { found = val; break; }
    fclose(f);
    return found;
}

/* Read a single-integer cgroup control file, e.g. memory.current. */
static long read_cgroup_scalar(const char *cgroup_name, const char *filename) {
    if (!is_linux()) return -1;
    char p[MAX_CMD];
    snprintf(p, sizeof(p), "%s/%s/%s", CGROUP_ROOT, cgroup_name, filename);
    FILE *f = fopen(p, "r");
    if (!f) return -1;
    long val;
    long found = (fscanf(f, "%ld", &val) == 1) ? val : -1;
    fclose(f);
    return found;
}

/* memory.high: integer bytes, or -1 for "max" / unreadable. */
static long read_memory_high(const char *cgroup_name) {
    if (!is_linux()) return -1;
    char p[MAX_CMD];
    snprintf(p, sizeof(p), "%s/%s/memory.high", CGROUP_ROOT, cgroup_name);
    FILE *f = fopen(p, "r");
    if (!f) return -1;
    char buf[64];
    if (!fgets(buf, sizeof(buf), f)) { fclose(f); return -1; }
    fclose(f);
    char *s = trim(buf);
    if (!strcmp(s, "max")) return -1;
    return atol(s);
}

/* Sum completed IO counters across all devices in cgroup io.stat.
 * Sets outputs to 0 when the file exists but lists no devices;
 * returns -1 if unreadable. Omitted out pointers are ignored. */
static int read_iostat_totals(const char *cgroup_name,
                              long *out_rios, long *out_wios,
                              long *out_rbytes, long *out_wbytes) {
    if (!is_linux()) return -1;
    char p[MAX_CMD];
    snprintf(p, sizeof(p), "%s/%s/io.stat", CGROUP_ROOT, cgroup_name);
    FILE *f = fopen(p, "r");
    if (!f) return -1;
    long rios = 0, wios = 0, rbytes = 0, wbytes = 0;
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        char *save = NULL;
        char *tok = strtok_r(line, " \t\n", &save); /* maj:min */
        if (!tok) continue;
        while ((tok = strtok_r(NULL, " \t\n", &save)) != NULL) {
            if (!strncmp(tok, "rios=", 5))
                rios += strtol(tok + 5, NULL, 10);
            else if (!strncmp(tok, "wios=", 5))
                wios += strtol(tok + 5, NULL, 10);
            else if (!strncmp(tok, "rbytes=", 7))
                rbytes += strtol(tok + 7, NULL, 10);
            else if (!strncmp(tok, "wbytes=", 7))
                wbytes += strtol(tok + 7, NULL, 10);
        }
    }
    fclose(f);
    if (out_rios) *out_rios = rios;
    if (out_wios) *out_wios = wios;
    if (out_rbytes) *out_rbytes = rbytes;
    if (out_wbytes) *out_wbytes = wbytes;
    return 0;
}

static long read_iostat_rios(const char *cgroup_name) {
    long rios = -1;
    return read_iostat_totals(cgroup_name, &rios, NULL, NULL, NULL) == 0
           ? rios : -1;
}

/* ------------------------------------------------------------------ *
 * Target-file page-cache residency (mincore + cachestat nr_cache)
 * ------------------------------------------------------------------ */

/* cachestat(2) — Linux 6.5+; no libc wrapper yet */
#ifdef __linux__
#ifndef __NR_cachestat
#if defined(__x86_64__) || defined(__aarch64__) || \
    (defined(__riscv) && __riscv_xlen == 64)
#define __NR_cachestat 451
#else
#define __NR_cachestat -1
#endif
#endif

typedef struct {
    uint64_t off;
    uint64_t len;
} CachestatRange;

typedef struct {
    uint64_t nr_cache;
    uint64_t nr_dirty;
    uint64_t nr_writeback;
    uint64_t nr_evicted;
    uint64_t nr_recently_evicted;
} CachestatInfo;

/* 0 = untested, 1 = available, -1 = unavailable (kernel < 6.5 or ENOSYS) */
static int g_cachestat_state;

/*
 * Check if cachestat(2) is available.
 * Since cachestat(2) is a syscall and a properties of the running kernel
 * rather than the test file, we need to check if it is available by opening
 * the executable file. Actual cachestat measurements are in file_nr_cache_bytes().
 * Returns true if available, false otherwise.
 */
static bool cachestat_usable(void) {
    if (g_cachestat_state != 0) return g_cachestat_state > 0;
    if (!is_linux() || __NR_cachestat < 0) {
        g_cachestat_state = -1;
        return false;
    }
    // Open the executable file for the benchmark to check that kernel has cachestat(2) syscall
    int fd = open("/proc/self/exe", O_RDONLY);
    if (fd < 0) { g_cachestat_state = -1; return false; }
    CachestatRange range = {0, 0};
    CachestatInfo cs = {0};
    // During the test, the kernel will be inspecting the benchmark executable file just
    // to cache whether the syscall is available or not.
    long r = syscall(__NR_cachestat, fd, &range, &cs, 0);
    close(fd);
    if (r == 0) { g_cachestat_state = 1; return true; }
    if (errno == ENOSYS) { g_cachestat_state = -1; return false; }
    /* other probe errors: still attempt on workload files */
    g_cachestat_state = 1;
    return true;
}

/*
 * Resident page-cache bytes via cachestat(2) nr_cache (pages × page size).
 * Same scope as mincore — one file only, no mmap, no cache disturbance.
 * Returns -1 when unavailable (kernel < 6.5) or on error.
 */
static long file_nr_cache_bytes(const char *path) {
    if (!is_linux() || !path || !*path || !cachestat_usable()) return -1;

    // Open the test file for reading
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;

    CachestatRange range = { .off = 0, .len = 0 }; /* len 0 -> entire file */
    CachestatInfo cs = {0};
    // Invokes Linux cachestat(2) syscall on the file descriptor with the test file
    // Results stored in cs
    if (syscall(__NR_cachestat, fd, &range, &cs, 0) != 0) {
        close(fd);
        return -1;
    }
    close(fd);

    // Get page size in bytes
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0) page = 4096; // Default page size is 4096 bytes
    // nr_cache is the number of pages in the cache
    if (cs.nr_cache > (uint64_t)LONG_MAX / (uint64_t)page) return -1;
    return (long)(cs.nr_cache * (uint64_t)page);
}
#else  /* !__linux__ */
static long file_nr_cache_bytes(const char *path)
{
    // Error when cachestat is not supported (non-Linux systems)
    (void)path;
    return -1;
}
#endif /* __linux__ */

/* mmap window per mincore() pass: bounds the status vector while keeping
 * syscall count low for multi-GiB files (a 32G file is 32 passes) */
#define MINCORE_WINDOW ((size_t)1 << 30)

/*
 * Resident page-cache bytes of one file, the way `fincore` measures it.
 *
 * Unlike memory.stat:file this counts only the workload's target file, so it
 * is not inflated by fio logs, json output, or anything else the harness
 * writes inside the tenant cgroup. mincore() reports residency without
 * faulting pages in, and the mapping is never touched, so sampling neither
 * populates the cache nor charges pages to the caller. May impose overhead
 * due to at least six syscalls per file (open, fstat, mmap, mincore, munmap, close).
 *
 * Returns -1 if the file is missing/unreadable or mincore is unavailable.
 */
static long file_resident_bytes(const char *path) {
    if (!is_linux() || !path || !*path) return -1;

    // open target file for reading
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;

    struct stat st;
    if (fstat(fd, &st) != 0 || st.st_size <= 0) { close(fd); return -1; }

    // Get page size
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0) page = 4096;

    // Bound the status vector while keeping
    // syscall count low for multi-GiB files (in 1 GiB chunks for large files)
    unsigned char *vec = malloc(MINCORE_WINDOW / (size_t)page);
    if (!vec) { close(fd); return -1; }

    long resident_pages = 0;
    bool ok = true;
    for (off_t off = 0; off < st.st_size; off += (off_t)MINCORE_WINDOW) {
        size_t remain = (size_t)(st.st_size - off);
        size_t len = remain < MINCORE_WINDOW ? remain : MINCORE_WINDOW;

        // Create a virtual address range of the pages in the file
        void *m = mmap(NULL, len, PROT_READ, MAP_SHARED, fd, off);
        if (m == MAP_FAILED) { ok = false; break; }

        /* mincore's vector is char * on macOS, unsigned char * on Linux */
        // mincore() takes in the mapping, and the kernel consults its virtual
        // memory and page cache structures to determine wheter a page is resident
        // and writes results into a vector with resident bits set for each page
        if (mincore(m, len, (void *)vec) == 0) {
            size_t pages = (len + (size_t)page - 1) / (size_t)page;
            for (size_t i = 0; i < pages; i++)
                if (vec[i] & 1) resident_pages++;
        } else {
            ok = false;
        }
        // Unmap the virtual address range
        munmap(m, len);
        if (!ok) break;
    }

    free(vec);
    close(fd);
    return ok ? resident_pages * page : -1;
}

/* Append a before/after memstat row to memstat/<client>_<mode>.csv
 *
 *   prev_refault : previous phase-"before" absolute refault (for per-phase delta)
 *   base_refault : run-start absolute refault (for cumulative total per phase);
 *                  pass -1 to leave the total column as -1.
 */
static void record_memstat(const char *cgroup_name, const char *client,
                           const char *mode, int phase, const char *when,
                           long prev_refault, long *out_refault,
                           long prev_memcur, long *out_memcur,
                           long base_refault,
                           long prev_pgfault, long *out_pgfault,
                           long prev_pgmajfault, long *out_pgmajfault,
                           long prev_rios, long *out_rios,
                           long prev_file, long *out_file,
                           const char *target_file,
                           long prev_resident, long *out_resident,
                           long prev_nr_cache, long *out_nr_cache) {
    long refault = read_memstat_field(cgroup_name, "workingset_refault_file");
    if (out_refault) *out_refault = refault;
    long memcur = read_cgroup_scalar(cgroup_name, "memory.current");
    if (out_memcur) *out_memcur = memcur;

    /* cgroup memory.stat page-fault counters (cumulative) */
    long pgfault = read_memstat_field(cgroup_name, "pgfault");
    if (out_pgfault) *out_pgfault = pgfault;
    long pgmajfault = read_memstat_field(cgroup_name, "pgmajfault");
    if (out_pgmajfault) *out_pgmajfault = pgmajfault;

    /* cgroup memory.stat file = page-cache bytes charged to this cgroup */
    long file_bytes = read_memstat_field(cgroup_name, "file");
    if (out_file) *out_file = file_bytes;

    /* residency of the workload's own file only — excludes harness/log cache */
    long resident = file_resident_bytes(target_file);
    if (out_resident) *out_resident = resident;
    long nr_cache = file_nr_cache_bytes(target_file);
    if (out_nr_cache) *out_nr_cache = nr_cache;

    /* cgroup io.stat completed read IOs (summed across devices) */
    long rios = read_iostat_rios(cgroup_name);
    if (out_rios) *out_rios = rios;

    char dir[MAX_CMD], path[MAX_CMD];
    ensure_subdir(opt.output_dir, "memstat", dir, sizeof(dir));
    snprintf(path, sizeof(path), "%s/%s_%s.csv", dir, client, mode);

    bool exists = access(path, F_OK) == 0;
    FILE *f = fopen(path, "a");
    if (!f) { INFO("warning: memstat csv %s: %s", path, strerror(errno)); return; }
    if (!exists)
        fprintf(f, "phase,when,wall_time,workingset_refault_file,"
                   "workingset_refault_file_delta,workingset_refault_file_total,"
                   "memory_current_bytes,memory_current_bytes_delta,"
                   "pgfault,pgfault_delta,pgmajfault,pgmajfault_delta,"
                   "rios,rios_delta,file_bytes,file_bytes_delta,"
                   "target_file_resident_bytes,"
                   "target_file_resident_bytes_delta,"
                   "target_file_nr_cache_bytes,"
                   "target_file_nr_cache_bytes_delta\n");
    bool is_after = when && !strcmp(when, "after");
    long delta = (is_after && prev_refault >= 0 && refault >= 0)
                     ? refault - prev_refault : -1;
    /* cumulative refaults for this run through this snapshot (>=0 only when a
     * run-start baseline was captured and the counter is readable) */
    long total = (base_refault >= 0 && refault >= 0) ? refault - base_refault : -1;
    long memdelta = (is_after && prev_memcur >= 0 && memcur >= 0)
                     ? memcur - prev_memcur : -1;
    long pgfdelta = (is_after && prev_pgfault >= 0 && pgfault >= 0)
                     ? pgfault - prev_pgfault : -1;
    long pgmajdelta = (is_after && prev_pgmajfault >= 0 && pgmajfault >= 0)
                     ? pgmajfault - prev_pgmajfault : -1;
    long riosdelta = (is_after && prev_rios >= 0 && rios >= 0)
                     ? rios - prev_rios : -1;
    long filedelta = (is_after && prev_file >= 0 && file_bytes >= 0)
                     ? file_bytes - prev_file : -1;
    long resdelta = (is_after && prev_resident >= 0 && resident >= 0)
                     ? resident - prev_resident : -1;
    long ncdelta = (is_after && prev_nr_cache >= 0 && nr_cache >= 0)
                     ? nr_cache - prev_nr_cache : -1;
    fprintf(f, "%d,%s,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,"
               "%ld,%ld,%ld,%ld\n",
            phase, when, (long)time(NULL), refault, delta, total, memcur, memdelta,
            pgfault, pgfdelta, pgmajfault, pgmajdelta, rios, riosdelta,
            file_bytes, filedelta, resident, resdelta, nr_cache, ncdelta);
    fclose(f);
}

/* ------------------------------------------------------------------ *
 * Telemetry sampler: /proc/vmstat + per-cgroup file_dirty,
 * plus per-cgroup PSI (memory.pressure / io.pressure) time series.
 * ------------------------------------------------------------------ */

static volatile sig_atomic_t sampler_stop = 0;
static void sampler_sig(int sig) { (void)sig; sampler_stop = 1; }

/* System-wide memory counters
 * nr_dirty: number of pages changes but not yet flushed to disk
 * nr_writeback: number of pages actively flushed to disk
 * pgpgin: number of pages read from disk
 * pgscan_kswapd: number of pages scanned in the LRU list for eviction
 */
static long read_vmstat_field(const char *field) {
    FILE *f = fopen("/proc/vmstat", "r");
    if (!f) return -1;
    char key[128]; long val, found = -1;
    while (fscanf(f, "%127s %ld", key, &val) == 2)
        if (!strcmp(key, field)) { found = val; break; }
    fclose(f);
    return found;
}

/* Parse "some avg10=.. avg60=.. avg300=.. total=N" from a PSI file and return
 * the `some` cumulative total (microseconds stalled). -1 on failure. */
static long read_psi_total(const char *cgroup_name, const char *resource) {
    char p[MAX_CMD];
    snprintf(p, sizeof(p), "%s/%s/%s.pressure", CGROUP_ROOT, cgroup_name, resource);
    FILE *f = fopen(p, "r");
    if (!f) return -1;
    char line[512];
    long total = -1;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "some", 4) == 0) {
            char *t = strstr(line, "total=");
            if (t) total = atol(t + 6);
            break;
        }
    }
    fclose(f);
    return total;
}

/* Overwrite a one-line live sample file (wall_time,value) for Senpai. Atomic via rename. */
static void write_live_sample(const char *path, long sec, const char *value) {
    if (!path || !*path || !value) return;
    char tmp[MAX_CMD];
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    FILE *f = fopen(tmp, "w");
    if (!f) return;
    fprintf(f, "%ld,%s\n", sec, value);
    fclose(f);
    if (rename(tmp, path) != 0)
        unlink(tmp);
}

static void write_live_p99(const char *path, long sec, double p99_us) {
    char buf[64];
    snprintf(buf, sizeof(buf), "%.3f", p99_us);
    write_live_sample(path, sec, buf);
}

static void write_live_bytes(const char *path, long sec, long bytes) {
    if (bytes < 0) return;
    char buf[64];
    snprintf(buf, sizeof(buf), "%ld", bytes);
    write_live_sample(path, sec, buf);
}

/* <ts_dir>/<client>_nr_cache.live — Senpai's per-tenant target-file cache status updated every interval. */
static void live_nr_cache_path(char *out, size_t n, const char *ts_dir, const char *client) {
    snprintf(out, n, "%s/%s_nr_cache.live", ts_dir, client);
}

/*
 * Runs in a forked child. Every SAMPLE_INTERVAL_S seconds appends:
 *   dirty/vmstat_<mode>.csv       : ts, nr_dirty, nr_writeback, pgpgin, pgscan_kswapd
 *   dirty/<cg>_<mode>_dirty.csv   : ts, file_dirty, file_writeback (per cgroup)
 *   psi/<cg>_<mode>.csv           : ts, wall_time, mem_some_total_us, io_some_total_us
 *   timeseries/<client>_<mode>.csv: 1 Hz cgroup memory/PSI/io.stat (client2 includes memory.high)
 * Exits on SIGTERM/SIGINT.
 */
static void run_sampler(const char *mode, char client_names[][MAX_STR],
                        char cgroup_names[][MAX_STR],
                        char target_files[][MAX_STR], int n_cg) {
    signal(SIGTERM, sampler_sig);
    signal(SIGINT,  sampler_sig);

    char psi_dir[MAX_CMD], dirty_dir[MAX_CMD], ts_dir[MAX_CMD] = "";
    ensure_subdir(opt.output_dir, "psi",   psi_dir,   sizeof(psi_dir));
    ensure_subdir(opt.output_dir, "dirty", dirty_dir, sizeof(dirty_dir));
    if (opt.use_timeseries)
        ensure_subdir(opt.output_dir, "timeseries", ts_dir, sizeof(ts_dir));

    char vmstat_path[MAX_CMD];
    snprintf(vmstat_path, sizeof(vmstat_path), "%s/vmstat_%s.csv", dirty_dir, mode);
    FILE *vf = fopen(vmstat_path, "w");
    if (vf) fprintf(vf, "ts,nr_dirty,nr_writeback,pgpgin,pgscan_kswapd\n");

    FILE *cgf[MAX_CGROUPS] = {0};
    FILE *psf[MAX_CGROUPS] = {0};
    FILE *tsf[MAX_CGROUPS] = {0};
    for (int i = 0; i < n_cg && i < MAX_CGROUPS; i++) {
        char label[MAX_STR];
        cgroup_file_label(cgroup_names[i], label, sizeof(label));
        char p[MAX_CMD];
        snprintf(p, sizeof(p), "%s/%s_%s_dirty.csv", dirty_dir, label, mode);
        cgf[i] = fopen(p, "w");
        if (cgf[i]) fprintf(cgf[i], "ts,file_dirty,file_writeback\n");
        else VLOG("could not open dirty csv %s: %s", p, strerror(errno));

        if (opt.use_psi) {
            snprintf(p, sizeof(p), "%s/%s_%s.csv", psi_dir, label, mode);
            psf[i] = fopen(p, "w");
            if (psf[i]) fprintf(psf[i], "ts,wall_time,mem_some_total_us,io_some_total_us\n");
            else VLOG("could not open psi csv %s: %s", p, strerror(errno));
        }

        if (opt.use_timeseries) {
            snprintf(p, sizeof(p), "%s/%s_%s.csv", ts_dir, client_names[i], mode);
            tsf[i] = fopen(p, "w");
            if (tsf[i]) {
                if (!strcmp(client_names[i], "client2_noisy"))
                    fprintf(tsf[i], "wall_time,memory_current_bytes,file_bytes,"
                            "target_file_resident_bytes,target_file_nr_cache_bytes,"
                            "memory_high_bytes,workingset_refault_file,"
                            "mem_psi_some_total_us,io_psi_some_total_us,"
                            "rios,wios,rbytes,wbytes\n");
                else
                    fprintf(tsf[i], "wall_time,memory_current_bytes,file_bytes,"
                            "target_file_resident_bytes,target_file_nr_cache_bytes,"
                            "workingset_refault_file,mem_psi_some_total_us,"
                            "io_psi_some_total_us,rios,wios,rbytes,wbytes\n");
            } else {
                VLOG("could not open timeseries csv %s: %s", p, strerror(errno));
            }
        }
    }

    /* wall-clock anchor for aligning timeseries with clat logs */
    if (opt.use_timeseries) {
        char start_path[MAX_CMD];
        snprintf(start_path, sizeof(start_path), "%s/run_%s.start_ts", ts_dir, mode);
        FILE *sf = fopen(start_path, "w");
        if (sf) { fprintf(sf, "%ld\n", (long)time(NULL)); fclose(sf); }
    }

    long ts = 0;
    while (!sampler_stop) {
        long wall = (long)time(NULL);
        long nd = read_vmstat_field("nr_dirty");
        long nw = read_vmstat_field("nr_writeback");
        long pi = read_vmstat_field("pgpgin");
        long pk = read_vmstat_field("pgscan_kswapd");
        if (vf) { fprintf(vf, "%ld,%ld,%ld,%ld,%ld\n", ts, nd, nw, pi, pk); fflush(vf); }

        for (int i = 0; i < n_cg && i < MAX_CGROUPS; i++) {
            if (cgf[i]) {
                long fd = read_memstat_field(cgroup_names[i], "file_dirty");
                long fw = read_memstat_field(cgroup_names[i], "file_writeback");
                fprintf(cgf[i], "%ld,%ld,%ld\n", ts, fd, fw);
                fflush(cgf[i]);
            }
            long mem_psi = -1, io_psi = -1;
            if (psf[i] || tsf[i]) {
                mem_psi = read_psi_total(cgroup_names[i], "memory");
                io_psi  = read_psi_total(cgroup_names[i], "io");
            }
            if (psf[i])
                fprintf(psf[i], "%ld,%ld,%ld,%ld\n", ts, wall, mem_psi, io_psi);
            if (tsf[i]) {
                long memcur = read_cgroup_scalar(cgroup_names[i], "memory.current");
                long file_b = read_memstat_field(cgroup_names[i], "file");
                long resident = file_resident_bytes(target_files[i]);
                long nr_cache = file_nr_cache_bytes(target_files[i]);
                long refault = read_memstat_field(cgroup_names[i], "workingset_refault_file");
                long rios = -1, wios = -1, rbytes = -1, wbytes = -1;
                read_iostat_totals(cgroup_names[i], &rios, &wios, &rbytes, &wbytes);
                if (!strcmp(client_names[i], "client2_noisy")) {
                    long mhigh = read_memory_high(cgroup_names[i]);
                    fprintf(tsf[i], "%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld\n",
                            wall, memcur, file_b, resident, nr_cache, mhigh, refault,
                            mem_psi, io_psi, rios, wios, rbytes, wbytes);
                } else {
                    fprintf(tsf[i], "%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld\n",
                            wall, memcur, file_b, resident, nr_cache, refault, mem_psi, io_psi,
                            rios, wios, rbytes, wbytes);
                }
                /* cachestat needs Linux 6.5+; mincore residency covers older kernels */
                char live_path[MAX_CMD];
                live_nr_cache_path(live_path, sizeof(live_path), ts_dir, client_names[i]);
                // If avaliable, use nr_cache to track cache usage
                write_live_bytes(live_path, wall, nr_cache >= 0 ? nr_cache : resident);
                fflush(tsf[i]);
            }
            if (psf[i]) fflush(psf[i]);
        }
        sleep(SAMPLE_INTERVAL_S);
        ts += SAMPLE_INTERVAL_S;
    }
    if (vf) fclose(vf);
    for (int i = 0; i < n_cg; i++) {
        if (cgf[i]) fclose(cgf[i]);
        if (psf[i]) fclose(psf[i]);
        if (tsf[i]) fclose(tsf[i]);
    }
    _exit(0);
}

/* fork a child process to sample /proc/vmstat, memory.stat, and PSI */
static pid_t start_sampler(const char *mode, char client_names[][MAX_STR],
                           char cgroup_names[][MAX_STR],
                           char target_files[][MAX_STR], int n_cg) {
    if (!is_linux()) { VLOG("sampler skipped (not Linux)"); return -1; }
    pid_t pid = fork();
    if (pid == 0) {
        run_sampler(mode, client_names, cgroup_names, target_files, n_cg);
        _exit(0);
    }
    if (pid < 0) INFO("warning: fork sampler: %s", strerror(errno));
    return pid;
}

static void stop_sampler(pid_t pid) {
    if (pid <= 0) return;
    kill(pid, SIGTERM);
    waitpid(pid, NULL, 0);
}

/* ------------------------------------------------------------------ *
 * Per-client clat log tailer → 1 Hz p50/p95/p99/p99.9 + IOPS
 * (client1_steady and client2_noisy when write_clat_log is on)
 * ------------------------------------------------------------------ */

/*
 * fio's completion-latency histogram layout (stat.h): FIO_IO_U_PLAT_GROUP_NR
 * groups of FIO_IO_U_PLAT_VAL buckets holding nanosecond values, logged with
 * log_hist_coarseness=0 so bins are not merged.
 *
 * The group count is version-dependent (19 in older fio, 29 since ~fio 3.x),
 * so the record's bin count is taken from the log itself and only the cap here
 * is fixed. Bins past the cap are folded into the last one, which biases a
 * percentile high rather than silently dropping the slowest samples.
 */
#define FIO_IO_U_PLAT_BITS      6
#define FIO_IO_U_PLAT_VAL       (1u << FIO_IO_U_PLAT_BITS)
#define FIO_IO_U_PLAT_GROUP_MAX 32
#define FIO_IO_U_PLAT_NR        (FIO_IO_U_PLAT_GROUP_MAX * FIO_IO_U_PLAT_VAL)

#define HIST_PENDING   16   /* seconds held open for late/interleaved job logs */
#define HIST_MAX_FILES 32   /* one clat_hist log per fio job */

/* Midpoint of histogram bucket idx, in nanoseconds (fio's plat_idx_to_val). */
static double hist_bin_to_ns(unsigned int idx) {
    if (idx < (FIO_IO_U_PLAT_VAL << 1))
        return (double)idx;
    unsigned int error_bits = (idx >> FIO_IO_U_PLAT_BITS) - 1;
    double base = (double)(1ULL << (error_bits + FIO_IO_U_PLAT_BITS));
    unsigned int k = idx % FIO_IO_U_PLAT_VAL;
    return base + ((double)k + 0.5) * (double)(1u << error_bits);
}

typedef struct {
    long               sec;      /* -1 when the slot is free */
    unsigned long long bins[FIO_IO_U_PLAT_NR];
    unsigned long long total;
} HistBucket;

/* Interpolation-free percentile: the bucket containing the target rank. */
static double hist_percentile_us(const HistBucket *b, double p) {
    if (b->total == 0) return -1.0;
    double target = (p / 100.0) * (double)b->total;
    unsigned long long cum = 0;
    for (unsigned int i = 0; i < FIO_IO_U_PLAT_NR; i++) {
        cum += b->bins[i];
        if ((double)cum >= target) return hist_bin_to_ns(i) / 1000.0;
    }
    return hist_bin_to_ns(FIO_IO_U_PLAT_NR - 1) / 1000.0;
}

static void hist_flush(FILE *out, HistBucket *b, const char *live_p99_path) {
    if (out && b->total > 0) {
        double p99 = hist_percentile_us(b, 99.0);
        fprintf(out, "%ld,%.3f,%.3f,%.3f,%.3f,%llu\n",
                b->sec,
                hist_percentile_us(b, 50.0),
                hist_percentile_us(b, 95.0),
                p99,
                hist_percentile_us(b, 99.9),
                b->total);
        write_live_p99(live_p99_path, b->sec, p99);
    }
    b->sec = -1;
    b->total = 0;
    memset(b->bins, 0, sizeof(b->bins));
}

/* Slot for sec, evicting the oldest bucket if the table is full. */
static HistBucket *hist_slot(HistBucket *tab, long sec, FILE *out,
                             const char *live_p99_path) {
    HistBucket *free_slot = NULL, *oldest = &tab[0];
    for (int i = 0; i < HIST_PENDING; i++) {
        if (tab[i].sec == sec) return &tab[i];
        if (tab[i].sec < 0) { if (!free_slot) free_slot = &tab[i]; }
        else if (oldest->sec < 0 || tab[i].sec < oldest->sec) oldest = &tab[i];
    }
    if (!free_slot) { hist_flush(out, oldest, live_p99_path); free_slot = oldest; }
    free_slot->sec = sec;
    return free_slot;
}

/* Emit every bucket older than cutoff, oldest first. */
static void hist_flush_before(FILE *out, HistBucket *tab, long cutoff,
                              const char *live_p99_path) {
    for (;;) {
        HistBucket *oldest = NULL;
        for (int i = 0; i < HIST_PENDING; i++)
            if (tab[i].sec >= 0 && tab[i].sec < cutoff &&
                (!oldest || tab[i].sec < oldest->sec))
                oldest = &tab[i];
        if (!oldest) return;
        hist_flush(out, oldest, live_p99_path);
    }
}

/*
 * Each line is "time_ms, direction, block_size, bin0, ..., bin1215" and holds
 * the histogram for that interval only, so bins from every job's log can be
 * summed per second to get one true p99 for the client.
 */
static void hist_parse_line(char *line, HistBucket *tab, FILE *out,
                            const char *live_p99_path) {
    char *save = NULL;
    char *tok = strtok_r(line, ",", &save);
    if (!tok) return;
    long t_ms = atol(tok);
    if (t_ms <= 0) return;
    if (!strtok_r(NULL, ",", &save)) return;   /* direction */
    if (!strtok_r(NULL, ",", &save)) return;   /* block size */

    HistBucket *b = hist_slot(tab, t_ms / 1000, out, live_p99_path);
    unsigned int i = 0;
    while ((tok = strtok_r(NULL, ",", &save)) != NULL) {
        unsigned long long c = strtoull(tok, NULL, 10);
        b->bins[i < FIO_IO_U_PLAT_NR ? i : FIO_IO_U_PLAT_NR - 1] += c;
        b->total += c;
        i++;
    }
}

static void run_clat_aggregator(const char *mode, const char *client) {
    signal(SIGTERM, sampler_sig);
    signal(SIGINT,  sampler_sig);

    char clat_dir[MAX_CMD], ts_dir[MAX_CMD], out_path[MAX_CMD], live_path[MAX_CMD];
    ensure_subdir(opt.output_dir, "clat", clat_dir, sizeof(clat_dir));
    ensure_subdir(opt.output_dir, "timeseries", ts_dir, sizeof(ts_dir));
    snprintf(out_path, sizeof(out_path), "%s/%s_latency_%s.csv",
             ts_dir, client, mode);
    /* Tiny one-line file Senpai reads each tick: wall_time,p99 */
    snprintf(live_path, sizeof(live_path), "%s/%s_p99.live", ts_dir, client);
    const char *live_p99 = !strcmp(client, "client1_steady") ? live_path : NULL;

    FILE *out = fopen(out_path, "w");
    if (!out) { _exit(1); }
    fprintf(out, "wall_time,p50_us,p95_us,p99_us,p999_us,iops\n");

    static char tracked[HIST_MAX_FILES][MAX_CMD];
    static long  offsets[HIST_MAX_FILES];
    static char  line[256 * 1024];  /* one record = every latency bin */
    int n_tracked = 0;

    static HistBucket pending[HIST_PENDING];
    for (int i = 0; i < HIST_PENDING; i++) pending[i].sec = -1;

    while (!sampler_stop) {
        /* pick up logs as fio creates them (one per job, one set per phase) */
        DIR *d = opendir(clat_dir);
        if (d) {
            struct dirent *de;
            while ((de = readdir(d)) != NULL && n_tracked < HIST_MAX_FILES) {
                if (!strstr(de->d_name, client) ||
                    !strstr(de->d_name, "_clat_hist.") ||
                    !strstr(de->d_name, ".log"))
                    continue;
                char path[MAX_CMD];
                snprintf(path, sizeof(path), "%s/%s", clat_dir, de->d_name);
                bool known = false;
                for (int i = 0; i < n_tracked; i++)
                    if (!strcmp(tracked[i], path)) { known = true; break; }
                if (known) continue;
                snprintf(tracked[n_tracked], MAX_CMD, "%s", path);
                offsets[n_tracked] = 0;
                n_tracked++;
            }
            closedir(d);
        }

        for (int i = 0; i < n_tracked; i++) {
            FILE *fp = fopen(tracked[i], "r");
            if (!fp) continue;
            if (fseek(fp, offsets[i], SEEK_SET) == 0) {
                while (fgets(line, sizeof(line), fp)) {
                    /* skip a record truncated by fio's in-progress write */
                    if (!strchr(line, '\n')) break;
                    offsets[i] = ftell(fp);
                    hist_parse_line(line, pending, out, live_p99);
                }
            }
            fclose(fp);
        }

        /* a second is complete once fio has had time to write its record */
        hist_flush_before(out, pending, (long)time(NULL) - 2, live_p99);
        fflush(out);

        sleep(SAMPLE_INTERVAL_S);
    }

    hist_flush_before(out, pending, LONG_MAX, live_p99);
    fclose(out);
    _exit(0);
}

static pid_t start_clat_aggregator(const char *mode, const char *client) {
    if (!is_linux() || !opt.write_clat_log || !opt.use_timeseries || !client)
        return -1;
    pid_t pid = fork();
    if (pid == 0) { run_clat_aggregator(mode, client); _exit(0); }
    if (pid < 0) INFO("warning: fork clat aggregator (%s): %s",
                      client, strerror(errno));
    return pid;
}

static void stop_clat_aggregator(pid_t pid) {
    if (pid <= 0) return;
    kill(pid, SIGTERM);
    waitpid(pid, NULL, 0);
}

/* ------------------------------------------------------------------ *
 * Optional upstream Senpai (senpai_high.py): Client2 memory.high only.
 * Started after phase 0 so Client1 warmup is undisturbed.
 * ------------------------------------------------------------------ */

static long parse_byte_size(const char *s) {
    if (!s || !*s) return 0;
    char *end = NULL;
    long v = strtol(s, &end, 10);
    if (!end || !*end) return v;
    switch (*end) {
    case 'k': case 'K': return v << 10;
    case 'm': case 'M': return v << 20;
    case 'g': case 'G': return v << 30;
    default: return v;
    }
}

static pid_t start_senpai_high(const char *cgroup_name, const char *mode) {
    if (!is_linux() || !opt.use_senpai_high)
        return -1;
    if (!cgroup_name || !*cgroup_name) {
        INFO("warning: --senpai-high needs --cgroup-config with [%s]", opt.senpai_client);
        return -1;
    }
    if (access(opt.senpai_script, R_OK) != 0) {
        INFO("warning: --senpai-high set but cannot read %s (%s)",
             opt.senpai_script, strerror(errno));
        return -1;
    }

    char log_dir[MAX_CMD], log_path[MAX_CMD], cg_path[MAX_CMD], cmd[MAX_CMD];
    ensure_subdir(opt.output_dir, "senpai", log_dir, sizeof(log_dir));
    snprintf(log_path, sizeof(log_path), "%s/senpai_%s_%s.log",
             log_dir, opt.senpai_client, mode);
    snprintf(cg_path, sizeof(cg_path), "%s/%s", CGROUP_ROOT, cgroup_name);

    int n = snprintf(cmd, sizeof(cmd),
             "exec python3 '%s' '%s' --min-size %ld --max-size %ld "
             "--interval %d --pressure %d >> '%s' 2>&1",
             opt.senpai_script, cg_path,
             opt.senpai_min_size, opt.senpai_max_size,
             opt.senpai_interval, opt.senpai_pressure, log_path);
    if (n < 0 || (size_t)n >= sizeof(cmd)) {
        INFO("warning: senpai-high command truncated");
        return -1;
    }

    pid_t pid = fork();
    if (pid == 0) {
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    if (pid < 0) {
        INFO("warning: fork senpai-high: %s", strerror(errno));
        return -1;
    }
    INFO("senpai-high armed after phase 0: memory.high %s -> %s", cg_path, log_path);
    return pid;
}

static void stop_senpai_high(pid_t pid) {
    if (pid <= 0) return;
    kill(pid, SIGTERM);
    waitpid(pid, NULL, 0);
}

/* ------------------------------------------------------------------ *
 * iostat -dx logger (device read/write latency + queue depth)
 * ------------------------------------------------------------------ */

static pid_t start_iostat(const char *mode) {
    if (!is_linux()) { VLOG("iostat skipped (not Linux)"); return -1; }
    char dir[MAX_CMD], path[MAX_CMD], cmd[MAX_CMD];
    ensure_subdir(opt.output_dir, "iostat", dir, sizeof(dir));
    snprintf(path, sizeof(path), "%s/run_%s.iostat", dir, mode);

    /* Record iostat's own start time so analysis can map each 1s report
     * (report 0 is the since-boot average; reports 1..N are the 1s
     * intervals) back to a wall-clock time and line it up against the
     * per-phase before/after wall_time in memstat CSV files. */
    char ts_path[MAX_CMD];
    snprintf(ts_path, sizeof(ts_path), "%s/run_%s.start_ts", dir, mode);
    FILE *tf = fopen(ts_path, "w");
    if (tf) { fprintf(tf, "%ld\n", (long)time(NULL)); fclose(tf); }

    /* iostat -dx 1 : extended device stats every second, redirected to file */
    snprintf(cmd, sizeof(cmd), "exec iostat -dx 1 > '%s' 2>/dev/null", path);

    pid_t pid = fork();
    if (pid == 0) {
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    if (pid < 0) INFO("warning: fork iostat: %s", strerror(errno));
    else VLOG("iostat -dx 1 -> %s", path);
    return pid;
}

static void stop_iostat(pid_t pid) {
    if (pid <= 0) return;
    kill(pid, SIGTERM);
    waitpid(pid, NULL, 0);
}

/* ------------------------------------------------------------------ *
 * Optional bpftrace debug: memstall / iowait entry counts per cgroup
 *
 * Counts stall-*entry* events (not duration, not cache misses). Primary
 * PSI metrics remain memory.pressure / io.pressure some-total deltas.
 * On by default; disable with --no-bpftrace. Skipped if bpftrace missing,
 * not root, or no cgroups. Requires kernel symbols for psi_memstall_enter
 * and BTF for task_struct->in_iowait.
 * ------------------------------------------------------------------ */

/* cgroup v2 id ≈ directory inode; matches bpftrace's `cgroup` builtin. */
static long read_cgroup_id(const char *cgroup_name) {
    char p[MAX_CMD];
    struct stat st;
    snprintf(p, sizeof(p), "%s/%s", CGROUP_ROOT, cgroup_name);
    if (stat(p, &st) != 0) return -1;
    return (long)st.st_ino;
}

static bool bpftrace_on_path(void) {
    return system("command -v bpftrace >/dev/null 2>&1") == 0;
}

/*
 * Write psi_entries/probe_<mode>.bt and start bpftrace, appending CSV rows:
 *   ts,client,memstall_entries,iowait_entries
 * Counters are cumulative from bpftrace start (no per-second clear) so
 * analysis can take before/after deltas using memstat phase wall_times.
 */
static pid_t start_bpftrace(const char *mode,
                            char client_names[][MAX_STR],
                            char cgroup_names[][MAX_STR],
                            int n) {
    if (!opt.use_bpftrace) return -1;
    if (!is_linux()) { VLOG("bpftrace skipped (not Linux)"); return -1; }
    if (n <= 0) {
        INFO("warning: bpftrace needs cgroups; skipping entry-count tracing");
        return -1;
    }
    if (geteuid() != 0) {
        INFO("warning: bpftrace requires root; skipping entry-count tracing");
        return -1;
    }
    if (!bpftrace_on_path()) {
        INFO("warning: bpftrace not found on PATH; skipping entry-count tracing");
        return -1;
    }

    long ids[MAX_CGROUPS];
    for (int i = 0; i < n; i++) {
        ids[i] = read_cgroup_id(cgroup_names[i]);
        if (ids[i] < 0) {
            INFO("warning: cannot stat cgroup %s for bpftrace; skipping", cgroup_names[i]);
            return -1;
        }
        VLOG("bpftrace cgroup %s (%s) id=%ld", client_names[i], cgroup_names[i], ids[i]);
    }

    char dir[MAX_CMD], script[MAX_CMD], csv[MAX_CMD], idmap[MAX_CMD], errpath[MAX_CMD];
    ensure_subdir(opt.output_dir, "psi_entries", dir, sizeof(dir));
    snprintf(script, sizeof(script), "%s/probe_%s.bt", dir, mode);
    snprintf(csv, sizeof(csv), "%s/run_%s.csv", dir, mode);
    snprintf(idmap, sizeof(idmap), "%s/cgroup_ids_%s.txt", dir, mode);
    snprintf(errpath, sizeof(errpath), "%s/run_%s.err", dir, mode);

    FILE *idf = fopen(idmap, "w");
    if (idf) {
        for (int i = 0; i < n; i++)
            fprintf(idf, "%s %s %ld\n", client_names[i], cgroup_names[i], ids[i]);
        fclose(idf);
    }

    FILE *bf = fopen(script, "w");
    if (!bf) {
        INFO("warning: cannot write bpftrace script %s: %s", script, strerror(errno));
        return -1;
    }
    fprintf(bf, "/* auto-generated by benchmark --bpftrace; debug entry counts only */\n");
    for (int i = 0; i < n; i++) {
        // Every time a memstall event occurs, increment the memstall counter for this cgroup
        fprintf(bf,
                "kprobe:psi_memstall_enter\n"
                "/cgroup == %ld/\n"
                "{\n"
                "  @memstall%d += 1;\n"
                "}\n\n",
                ids[i], i);
    }
    // Every time a task enters iowait, increment the iowait counter for the corresponding cgroup
    fprintf(bf, "tracepoint:sched:sched_switch\n{\n");
    fprintf(bf, "  $t = (struct task_struct *)curtask;\n");
    fprintf(bf, "  if ($t->in_iowait == 1) {\n");
    for (int i = 0; i < n; i++) {
        fprintf(bf,
                "    if (cgroup == %ld) { @iowait%d += 1; }\n",
                ids[i], i);
    }
    fprintf(bf, "  }\n}\n\n");
    // Every second, print the timestamp, client name, and cumulative memstall and iowait counts for each cgroup
    fprintf(bf, "interval:s:1\n{\n");
    for (int i = 0; i < n; i++) {
        /* wall-clock epoch for alignment with memstat phase wall_time */
        fprintf(bf, "  time(\"%%s,\");\n");
        fprintf(bf,
                "  printf(\"%s,%%lld,%%lld\\n\", @memstall%d, @iowait%d);\n",
                client_names[i], i, i);
    }
    fprintf(bf, "}\n");
    fclose(bf);

    FILE *cf = fopen(csv, "w");
    if (!cf) {
        INFO("warning: cannot write %s: %s", csv, strerror(errno));
        return -1;
    }
    fprintf(cf, "ts,client,memstall_entries,iowait_entries\n");
    fclose(cf);

    /* bpftrace script example:
     * auto-generated by benchmark --bpftrace; debug entry counts only
     * kprobe:psi_memstall_enter
     * /cgroup == cgroup_id_1/
     * {
     *   @memstall0 += 1;
     * }
     *
     * kprobe:psi_memstall_enter
     * /cgroup == cgroup_id_2/
     * {
     *   @memstall1 += 1;
     * }
     *
     * tracepoint:sched:sched_switch
     * {
     *   $t = (struct task_struct *)curtask;
     *   if ($t->in_iowait == 1) {
     *     if (cgroup == cgroup_id_1) { @iowait0 += 1; }
     *     if (cgroup == cgroup_id_2) { @iowait1 += 1; }
     *   }
     * }
     *
     * interval:s:1
     * {
     *   time("%s,");
     *   printf("client1_steady,%lld,%lld\n", @memstall0, @iowait0);
     *   time("%s,");
     *   printf("client2_noisy,%lld,%lld\n", @memstall1, @iowait1);
     * }
     */
    char cmd[MAX_CMD];
    snprintf(cmd, sizeof(cmd),
             "exec bpftrace '%s' >> '%s' 2>'%s'", script, csv, errpath);

    pid_t pid = fork();
    if (pid == 0) {
        /* child: execute the bpftrace command */
        // runs bpftrace results/.../psi_entries/probe_<mode>.bt >> run_<mode>.csv 2> run_<mode>.err
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    if (pid < 0) {
        INFO("warning: fork bpftrace: %s", strerror(errno));
        return -1;
    }
    /* brief settle so the first interval sample isn't empty of attachment */
    sleep(1);
    INFO("bpftrace debug tracing -> %s (probe %s)", csv, script);
    return pid;
}

static void stop_bpftrace(pid_t pid) {
    if (pid <= 0) return;
    kill(pid, SIGINT);
    waitpid(pid, NULL, 0);
}

/* ------------------------------------------------------------------ *
 * fio command builder
 * ------------------------------------------------------------------ */

/* Append "arg" to cmd buffer, guarding against overflow. */
static void append(char *cmd, size_t cap, const char *fmt, ...) {
    size_t len = strlen(cmd);
    va_list ap; va_start(ap, fmt);
    vsnprintf(cmd + len, cap - len, fmt, ap);
    va_end(ap);
}

/*
 * Build the fio command line for one client phase.
 * When clat logging is enabled for client1_steady / client2_noisy, writes a
 * .fio job file with write_lat_log (portable; produces prefix_clat.<job>.log
 * for the aggregator).
 */
static void build_fio_cmd(char *cmd, size_t cap, const ClientConfig *c,
                          const PhaseConfig *p, const char *test_file,
                          const char *json_out, const char *mode_str,
                          bool cached) {
    int phase = (int)(p - c->phases);
    bool clat = opt.write_clat_log &&
                (!strcmp(c->name, "client1_steady") ||
                 !strcmp(c->name, "client2_noisy"));

    if (clat) {
        char clat_dir[MAX_CMD], clat_prefix[MAX_CMD], job_path[MAX_CMD];
        ensure_subdir(opt.output_dir, "clat", clat_dir, sizeof(clat_dir));
        snprintf(clat_prefix, sizeof(clat_prefix), "%s/%s_%s_p%d",
                 clat_dir, c->name, mode_str, phase);
        snprintf(job_path, sizeof(job_path), "%s/%s_%s_p%d.fio",
                 clat_dir, c->name, mode_str, phase);

        FILE *jf = fopen(job_path, "w");
        if (!jf) {
            INFO("warning: cannot write fio job %s: %s (clat log disabled)",
                 job_path, strerror(errno));
            clat = false;
        } else {
            fprintf(jf, "[%s_p%d]\n", c->name, phase);
            fprintf(jf, "filename=%s\n", test_file);
            fprintf(jf, "size=%s\n", c->file_size);
            fprintf(jf, "rw=%s\n", p->pattern);
            fprintf(jf, "bs=%s\n", p->block_size);
            fprintf(jf, "ioengine=%s\n", p->ioengine);
            fprintf(jf, "iodepth=%d\n", p->iodepth);
            fprintf(jf, "numjobs=%d\n", p->numjobs);
            if (p->io_size[0]) {
                fprintf(jf, "io_size=%s\n", p->io_size);
                if (p->runtime > 0) fprintf(jf, "runtime=%d\n", p->runtime);
            } else {
                fprintf(jf, "runtime=%d\n", p->runtime);
                fprintf(jf, "time_based=1\n");
            }
            fprintf(jf, "direct=%d\n", cached ? 0 : 1);
            if (cached) fprintf(jf, "invalidate=0\n");
            fprintf(jf, "group_reporting=1\n");
            if (p->rate_bw[0]) fprintf(jf, "rate=%s\n", p->rate_bw);
            if (p->rate_iops > 0) fprintf(jf, "rate_iops=%d\n", p->rate_iops);
            if (p->rwmixread >= 0 && strstr(p->pattern, "rw"))
                fprintf(jf, "rwmixread=%d\n", p->rwmixread);
            if (p->random_distribution[0])
                fprintf(jf, "random_distribution=%s\n", p->random_distribution);
            if (p->fdatasync > 0) fprintf(jf, "fdatasync=%d\n", p->fdatasync);
            if (p->fsync > 0) fprintf(jf, "fsync=%d\n", p->fsync);
            /* 1 s histograms, not per-IO samples: fio buffers every sample in
             * anonymous memory and flushes it to a log file at job end, and
             * both are charged to this client's cgroup. At ~1216 bins/s the
             * histogram is negligible next to the working set, so
             * memory.stat:file stays a measure of the workload's own cache. */
            fprintf(jf, "write_hist_log=%s\n", clat_prefix);
            fprintf(jf, "log_hist_msec=1000\n");
            fprintf(jf, "log_hist_coarseness=0\n");
            fprintf(jf, "log_unix_epoch=1\n");
            fclose(jf);

            cmd[0] = '\0';
            append(cmd, cap, "fio %s", job_path);
            append(cmd, cap, " --output-format=json+ --output=%s", json_out);
            return;
        }
    }

    cmd[0] = '\0';
    append(cmd, cap, "fio");
    append(cmd, cap, " --name=%s_p%d", c->name, phase);
    append(cmd, cap, " --filename=%s", test_file);
    append(cmd, cap, " --size=%s", c->file_size);
    append(cmd, cap, " --rw=%s", p->pattern);
    append(cmd, cap, " --bs=%s", p->block_size);
    append(cmd, cap, " --ioengine=%s", p->ioengine);
    append(cmd, cap, " --iodepth=%d", p->iodepth);
    append(cmd, cap, " --numjobs=%d", p->numjobs);
    if (p->io_size[0]) {
        append(cmd, cap, " --io_size=%s", p->io_size);
        if (p->runtime > 0) append(cmd, cap, " --runtime=%d", p->runtime);
    } else {
        append(cmd, cap, " --runtime=%d --time_based", p->runtime);
    }
    append(cmd, cap, " --direct=%d", cached ? 0 : 1);
    if (cached) append(cmd, cap, " --invalidate=0");
    append(cmd, cap, " --group_reporting");

    if (p->rate_bw[0])    append(cmd, cap, " --rate=%s", p->rate_bw);
    if (p->rate_iops > 0) append(cmd, cap, " --rate_iops=%d", p->rate_iops);
    if (p->rwmixread >= 0 && strstr(p->pattern, "rw"))
        append(cmd, cap, " --rwmixread=%d", p->rwmixread);
    if (p->random_distribution[0])
        append(cmd, cap, " --random_distribution=%s", p->random_distribution);
    if (p->fdatasync > 0) append(cmd, cap, " --fdatasync=%d", p->fdatasync);
    if (p->fsync     > 0) append(cmd, cap, " --fsync=%d",     p->fsync);

    append(cmd, cap, " --output-format=json+ --output=%s", json_out);
}

/* ------------------------------------------------------------------ *
 * Test-file management
 * ------------------------------------------------------------------ */

static void test_file_for(const ClientConfig *c, char *out, size_t n) {
    snprintf(out, n, "test_file_%s_%s", c->name, c->file_size);
}

/* Pre-create a client's test file with fio --create_only if it is missing or
 * the wrong size. Doing this up front keeps the timed phases from paying file
 * layout cost and keeps refault accounting clean. */
static void ensure_test_file(const ClientConfig *c) {
    char file[MAX_STR];
    test_file_for(c, file, sizeof(file));
    if (access(file, F_OK) == 0) { VLOG("test file %s exists", file); return; }
    INFO("creating test file %s (%s)...", file, c->file_size);
    char cmd[MAX_CMD];
    snprintf(cmd, sizeof(cmd),
             "fio --name=create_%s --filename=%s --size=%s --rw=write --bs=1M "
             "--create_only=1 --ioengine=libaio --direct=1 > /dev/null 2>&1",
             c->name, file, c->file_size);
    if (system(cmd) != 0)
        INFO("warning: could not pre-create %s (fio installed?)", file);
}

/* Drop the page cache before a cached-mode phase so refaults are meaningful. */
static void drop_caches(void) {
    if (!is_linux()) return;
    FILE *f = fopen("/proc/sys/vm/drop_caches", "w");
    if (!f) { INFO("warning: drop_caches (need sudo?): %s", strerror(errno)); return; }
    fprintf(f, "3\n");
    fclose(f);
    VLOG("dropped page cache");
}

/* ------------------------------------------------------------------ *
 * Running a single client (all its phases) inside its cgroup
 * ------------------------------------------------------------------ */

/* Wrap the fio command so the spawned shell joins the cgroup before exec. */
static void wrap_with_cgroup(char *cmd, size_t cap, const char *cg_path) {
    if (!cg_path || !is_linux()) return;
    char buf[MAX_CMD];
    /* echo $$ is the shell pid, which gets written to cgroup.procs (moves process into the cgroup) */
    /* then, exec replaces what the shell is running from shell script with the fio command */
    snprintf(buf, sizeof(buf),
             "echo $$ > %s/cgroup.procs 2>/dev/null; exec %s", cg_path, cmd);
    snprintf(cmd, cap, "%s", buf);
}

/* Fork+exec a client's phase in the background; returns pid. */
static pid_t spawn_client_phase(const ClientConfig *c, const PhaseConfig *p,
                                const char *mode_str, bool cached,
                                const char *cg_path) {
    char test_file[MAX_STR], json_out[MAX_CMD], cmd[MAX_CMD];
    test_file_for(c, test_file, sizeof(test_file));

    char json_dir[MAX_CMD];
    snprintf(json_dir, sizeof(json_dir), "%s", opt.output_dir);
    ensure_dir(json_dir);
    snprintf(json_out, sizeof(json_out), "%s/%s_%s_p%d.json",
             opt.output_dir, c->name, mode_str, (int)(p - c->phases));

    /* build but don't execute the fio command (which performs the workload described by the PhaseConfig) */
    build_fio_cmd(cmd, sizeof(cmd), c, p, test_file, json_out, mode_str, cached);
    /* build command to move the process into the cgroup and then execute the fio command */
    wrap_with_cgroup(cmd, sizeof(cmd), cg_path);
    VLOG("exec: %s", cmd);
    /* get the child process pid */
    pid_t pid = fork();
    if (pid == 0) {
        /* child: execute the fio command */
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    /* parent: return the child process pid */
    return pid;
}

/* ------------------------------------------------------------------ *
 * Run orchestration
 * ------------------------------------------------------------------ */

static const char *cgpath_or_null(const CgroupSet *set, const char *client, char *buf, size_t n) {
    if (!opt.use_cgroups || !set) return NULL;
    const CgroupConfig *g = cgroup_for_client(set, client);
    if (!g) return NULL;
    snprintf(buf, n, "%s/%s", CGROUP_ROOT, g->cgroup_name);
    return buf;
}

/* Run a set of clients concurrently for one cache mode, phase by phase. */
static void run_clients(Config *cfg, const CgroupSet *cgset,
                        const char **client_names, int n_clients,
                        bool cached) {
    const char *mode_str = cached ? "cached" : "direct";
    INFO("=== Running %d client(s) in %s mode ===", n_clients, mode_str);

    /* collect cgroup names (+ client labels) for the sampler / bpftrace */
    char cg_names[MAX_CGROUPS][MAX_STR];
    char bt_clients[MAX_CGROUPS][MAX_STR];
    char cg_target_files[MAX_CGROUPS][MAX_STR];
    int n_cg = 0;
    if (opt.use_cgroups && cgset) {
        for (int i = 0; i < n_clients && n_cg < MAX_CGROUPS; i++) {
            const CgroupConfig *g = cgroup_for_client(cgset, client_names[i]);
            if (!g) continue;
            snprintf(cg_names[n_cg], MAX_STR, "%s", g->cgroup_name);
            snprintf(bt_clients[n_cg], MAX_STR, "%s", client_names[i]);
            ClientConfig *tc = find_client(cfg, client_names[i]);
            cg_target_files[n_cg][0] = '\0';
            if (tc) test_file_for(tc, cg_target_files[n_cg], MAX_STR);
            n_cg++;
        }
    }

    /* max phase count across clients */
    int max_phases = 1;
    for (int i = 0; i < n_clients; i++) {
        ClientConfig *c = find_client(cfg, client_names[i]);
        if (c && c->num_phases > max_phases) max_phases = c->num_phases;
    }

    /* pre-create every client's test file (fio --create_only) */
    for (int i = 0; i < n_clients; i++) {
        ClientConfig *c = find_client(cfg, client_names[i]);
        if (c) ensure_test_file(c);
    }

    pid_t sampler = start_sampler(mode_str, bt_clients, cg_names,
                                  cg_target_files, n_cg);
    pid_t iostat  = start_iostat(mode_str);
    pid_t bpftrace_pid = start_bpftrace(mode_str, bt_clients, cg_names, n_cg);

    bool client1_in_run = false, client2_in_run = false;
    for (int i = 0; i < n_clients; i++) {
        if (!strcmp(client_names[i], "client1_steady")) client1_in_run = true;
        if (!strcmp(client_names[i], "client2_noisy")) client2_in_run = true;
    }
    pid_t clat_agg1 = client1_in_run
        ? start_clat_aggregator(mode_str, "client1_steady") : -1;
    pid_t clat_agg2 = client2_in_run
        ? start_clat_aggregator(mode_str, "client2_noisy") : -1;

    const CgroupConfig *senpai_cg = cgset
        ? cgroup_for_client(cgset, opt.senpai_client) : NULL;
    pid_t senpai_pid = -1;

    /* run-start absolute refault per client; cgroups were just recreated so this
     * is ~0. Used to report cumulative "total refaults per phase" (not just the
     * per-phase delta). */
    long base_refault[MAX_CLIENTS];
    for (int i = 0; i < n_clients; i++) {
        const CgroupConfig *g = cgset ? cgroup_for_client(cgset, client_names[i]) : NULL;
        base_refault[i] = g ? read_memstat_field(g->cgroup_name, "workingset_refault_file") : -1;
    }

    for (int ph = 0; ph < max_phases; ph++) {
        INFO("--- phase %d ---", ph);
        /* --drop-once: only clear the cache before phase 0 so B's reads can
         * accumulate across phases 1..N and evict A's pages by the later
         * phases, instead of every phase starting from a cold cache. */
        if (cached && (!opt.drop_once || ph == 0)) drop_caches();

        /* before-snapshot of memory.stat (refaults) to calculate workingset_refault_file_delta */
        /* refaults are a running total of page faults that have occurred per cgroup created */
        /* if we use /sys/fs/cgroup/clients/client1_steady across multiple benchmark runs, the counter keeps climbing */
        long prev_refault[MAX_CLIENTS];
        long prev_memcur[MAX_CLIENTS];
        long prev_pgfault[MAX_CLIENTS];
        long prev_pgmajfault[MAX_CLIENTS];
        long prev_rios[MAX_CLIENTS];
        long prev_file[MAX_CLIENTS];
        long prev_resident[MAX_CLIENTS];
        long prev_nr_cache[MAX_CLIENTS];
        char tgt_file[MAX_CLIENTS][MAX_STR];
        for (int i = 0; i < n_clients; i++) {
            prev_refault[i] = -1;
            prev_memcur[i] = -1;
            prev_pgfault[i] = -1;
            prev_pgmajfault[i] = -1;
            prev_rios[i] = -1;
            prev_file[i] = -1;
            prev_resident[i] = -1;
            prev_nr_cache[i] = -1;
            tgt_file[i][0] = '\0';
            ClientConfig *tc = find_client(cfg, client_names[i]);
            if (tc) test_file_for(tc, tgt_file[i], MAX_STR);
            const CgroupConfig *g = cgset ? cgroup_for_client(cgset, client_names[i]) : NULL;
            if (!g) continue;
            record_memstat(g->cgroup_name, client_names[i], mode_str, ph, "before",
                           -1, &prev_refault[i], -1, &prev_memcur[i], base_refault[i],
                           -1, &prev_pgfault[i], -1, &prev_pgmajfault[i],
                           -1, &prev_rios[i], -1, &prev_file[i],
                           tgt_file[i], -1, &prev_resident[i], -1, &prev_nr_cache[i]);
        }

        /* timed measurement: spawn all clients' phase-ph concurrently */
        pid_t pids[MAX_CLIENTS];
        for (int i = 0; i < n_clients; i++) {
            ClientConfig *c = find_client(cfg, client_names[i]);
            if (!c || ph >= c->num_phases || !c->phases[ph].present) { pids[i] = -1; continue; }
            char cgbuf[MAX_CMD];
            /* get the cgroup path for the client */
            const char *cg = cgpath_or_null(cgset, client_names[i], cgbuf, sizeof(cgbuf));
            /* fork+exec the client's phase in the background */
            /* returns the child process pid */
            pids[i] = spawn_client_phase(c, &c->phases[ph], mode_str, cached, cg);
        }

        /* wait for all clients' phases to complete as they run concurrently */
        for (int i = 0; i < n_clients; i++)
            if (pids[i] > 0) waitpid(pids[i], NULL, 0);
        // (void)npids;

        /* after-snapshot -> computes workingset_refault_file_delta / rios_delta /
         * file_bytes_delta */
        for (int i = 0; i < n_clients; i++) {
            const CgroupConfig *g = cgset ? cgroup_for_client(cgset, client_names[i]) : NULL;
            if (!g) continue;
            record_memstat(g->cgroup_name, client_names[i], mode_str, ph, "after",
                           prev_refault[i], NULL, prev_memcur[i], NULL, base_refault[i],
                           prev_pgfault[i], NULL, prev_pgmajfault[i], NULL,
                           prev_rios[i], NULL, prev_file[i], NULL,
                           tgt_file[i], prev_resident[i], NULL,
                           prev_nr_cache[i], NULL);
        }

        if (ph == 0 && opt.use_senpai_high && senpai_pid < 0)
            senpai_pid = start_senpai_high(
                senpai_cg ? senpai_cg->cgroup_name : NULL, mode_str);
    }

    stop_senpai_high(senpai_pid);
    stop_iostat(iostat);
    stop_clat_aggregator(clat_agg1);
    stop_clat_aggregator(clat_agg2);
    stop_sampler(sampler);
    stop_bpftrace(bpftrace_pid);

    /* append a one-line-per-client run summary */
    char sumpath[MAX_CMD];
    snprintf(sumpath, sizeof(sumpath), "%s/summary.txt", opt.output_dir);
    FILE *sf = fopen(sumpath, "a");
    if (sf) {
        fprintf(sf, "[%s mode] clients:", mode_str);
        for (int i = 0; i < n_clients; i++) fprintf(sf, " %s", client_names[i]);
        fprintf(sf, "  phases=%d  cgroups=%s  psi=%s  bpftrace=%s\n",
                max_phases, opt.use_cgroups ? "on" : "off", opt.use_psi ? "on" : "off",
                bpftrace_pid > 0 ? "on" : "off");
        fclose(sf);
    }
    INFO("=== %s mode complete ===", mode_str);
}

static void run_for_modes(Config *cfg, const CgroupSet *cgset,
                          const char **names, int n) {
    if (opt.mode == MODE_BOTH || opt.mode == MODE_CACHED) run_clients(cfg, cgset, names, n, true);
    if (opt.mode == MODE_BOTH || opt.mode == MODE_DIRECT) run_clients(cfg, cgset, names, n, false);
}

/* ------------------------------------------------------------------ *
 * CLI
 * ------------------------------------------------------------------ */

static void usage(const char *argv0) {
    printf(
"Usage: %s [options] <workload | dual | all>\n"
"\n"
"Modes:\n"
"  <workload>   Run a single client section from the config (baseline/characterization)\n"
"  dual         Run client1_steady (A) + client2_noisy (B) concurrently (primary experiment)\n"
"  all          Run every client section defined in the config\n"
"\n"
"Options:\n"
"  -c, --config PATH        Workload config ini (default: fairness_configs.ini)\n"
"      --cgroup-config PATH  cgroup layout ini (enables cgroup setup)\n"
"      --no-cgroup          Disable cgroup setup (shared page-cache pool)\n"
"      --no-psi             Disable PSI (memory/io.pressure) sampling\n"
"      --no-bpftrace        Disable bpftrace memstall/iowait entry counts\n"
"                           (on by default; skipped if bpftrace/root/BTF missing).\n"
"      --bpftrace           Keep entry-count tracing on (default; accepted for\n"
"                           compatibility). Not a cache-miss metric.\n"
"      --drop-once          Cached mode: drop_caches before phase 0 only; cache\n"
"                           persists across later phases (default: drop every phase)\n"
"      --clat-log           1 s fio clat histograms for client1+client2 (default on)\n"
"      --no-clat-log        Disable clat histogram logging (no 1 Hz p99 series)\n"
"      --no-timeseries      Disable 1 Hz timeseries/ CSVs\n"
"      --senpai-high        After phase 0: senpai_high.py on Client2 memory.high\n"
"                           (upstream Senpai; no Client1 memory.low)\n"
"      --senpai-script PATH Path to senpai_high.py (default: ./senpai_high.py)\n"
"      --senpai-client NAME Section whose memory.high is sized (default: client2_noisy)\n"
"      --senpai-min-size N  memory.high floor (bytes / K/M/G; default 100M)\n"
"      --senpai-max-size N  memory.high ceiling (default: 2G)\n"
"      --senpai-interval N  Probe interval seconds (default: 6)\n"
"      --senpai-pressure N  Memory-stall budget in µs (default: 10000)\n"
"  -m, --mode MODE          cached | direct | both (default: both)\n"
"  -o, --output DIR         Results directory (default: benchmark_results)\n"
"  -v, --verbose            Verbose logging\n"
"  -h, --help               This help\n"
"\n"
"Notes:\n"
"  * Writer-B (Mechanism 2) pairings MUST run with -m cached; direct=1 disables\n"
"    dirty writeback and Mechanism 2 vanishes.\n"
"  * --drop-once is for multi-phase eviction stories: A's hot set is warmed\n"
"    once, then B's reads accumulate phase over phase and evict A from cache.\n"
"  * cgroups / PSI / memory.stat / /proc/vmstat require Linux (cgroup v2).\n"
"  * bpftrace entry counts are on by default; use --no-bpftrace to skip.\n"
"    The benchmark still runs if bpftrace is not installed.\n"
"  * 1 Hz timeseries (default) land in <output>/timeseries/:\n"
"      client1_steady_latency_<mode>.csv / client2_noisy_latency_<mode>.csv — p50–p99.9 + IOPS\n"
"      client1_steady_<mode>.csv / client2_noisy_<mode>.csv — cgroup memory+PSI+io.stat (rios/wios/rbytes/wbytes)\n"
"  * --senpai-high starts only after phase 0 and logs to\n"
"      <output>/senpai/senpai_<client>_<mode>.log\n",
        argv0);
}

int main(int argc, char **argv) {
    const char *workload = NULL;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "-h") || !strcmp(a, "--help")) { usage(argv[0]); return 0; }
        else if (!strcmp(a, "-v") || !strcmp(a, "--verbose")) opt.verbose = true;
        else if (!strcmp(a, "--no-cgroup")) opt.use_cgroups = false;
        else if (!strcmp(a, "--no-psi")) opt.use_psi = false;
        else if (!strcmp(a, "--no-bpftrace")) opt.use_bpftrace = false;
        else if (!strcmp(a, "--bpftrace")) opt.use_bpftrace = true;
        else if (!strcmp(a, "--drop-once")) opt.drop_once = true;
        else if (!strcmp(a, "--clat-log")) opt.write_clat_log = true;
        else if (!strcmp(a, "--no-clat-log")) opt.write_clat_log = false;
        else if (!strcmp(a, "--no-timeseries")) opt.use_timeseries = false;
        else if (!strcmp(a, "--senpai-high")) opt.use_senpai_high = true;
        else if (!strcmp(a, "--senpai-script") && i + 1 < argc)
            opt.senpai_script = argv[++i];
        else if (!strcmp(a, "--senpai-client") && i + 1 < argc)
            opt.senpai_client = argv[++i];
        else if (!strcmp(a, "--senpai-min-size") && i + 1 < argc)
            opt.senpai_min_size = parse_byte_size(argv[++i]);
        else if (!strcmp(a, "--senpai-max-size") && i + 1 < argc)
            opt.senpai_max_size = parse_byte_size(argv[++i]);
        else if (!strcmp(a, "--senpai-interval") && i + 1 < argc)
            opt.senpai_interval = atoi(argv[++i]);
        else if (!strcmp(a, "--senpai-pressure") && i + 1 < argc)
            opt.senpai_pressure = atoi(argv[++i]);
        else if ((!strcmp(a, "-c") || !strcmp(a, "--config")) && i + 1 < argc) opt.config_path = argv[++i];
        else if (!strcmp(a, "--cgroup-config") && i + 1 < argc) opt.cgroup_config_path = argv[++i];
        else if ((!strcmp(a, "-o") || !strcmp(a, "--output")) && i + 1 < argc) opt.output_dir = argv[++i];
        else if ((!strcmp(a, "-m") || !strcmp(a, "--mode")) && i + 1 < argc) {
            const char *m = argv[++i];
            if      (!strcmp(m, "cached")) opt.mode = MODE_CACHED;
            else if (!strcmp(m, "direct")) opt.mode = MODE_DIRECT;
            else if (!strcmp(m, "both"))   opt.mode = MODE_BOTH;
            else { INFO("error: bad mode '%s'", m); return 2; }
        }
        else if (a[0] == '-') { INFO("error: unknown option '%s'", a); usage(argv[0]); return 2; }
        else workload = a;
    }

    if (!workload) { usage(argv[0]); return 2; }

    Config cfg;
    if (!parse_config(opt.config_path, &cfg)) return 1;
    INFO("loaded %d client section(s) from %s", cfg.num_clients, opt.config_path);

    ensure_dir(opt.output_dir);

    CgroupSet cgset; bool have_cgset = false;
    if (opt.use_cgroups && opt.cgroup_config_path) {
        if (!parse_cgroup_config(opt.cgroup_config_path, &cgset)) return 1;
        if (!setup_cgroups(&cgset)) return 1;
        have_cgset = true;
    }
    const CgroupSet *cgptr = have_cgset ? &cgset : NULL;

    if (!strcmp(workload, "dual")) {
        const char *names[] = { "client1_steady", "client2_noisy" };
        for (int i = 0; i < 2; i++)
            if (!find_client(&cfg, names[i])) { INFO("error: dual needs section [%s]", names[i]); return 1; }
        run_for_modes(&cfg, cgptr, names, 2);
    } else if (!strcmp(workload, "all")) {
        const char *names[MAX_CLIENTS];
        for (int i = 0; i < cfg.num_clients; i++) names[i] = cfg.clients[i].name;
        for (int i = 0; i < cfg.num_clients; i++) {
            const char *one[] = { names[i] };
            run_for_modes(&cfg, cgptr, one, 1);
        }
    } else {
        if (!find_client(&cfg, workload)) { INFO("error: no section [%s] in config", workload); return 1; }
        const char *one[] = { workload };
        run_for_modes(&cfg, cgptr, one, 1);
    }

    INFO("done. results in %s/", opt.output_dir);
    return 0;
}

#!/usr/bin/env bash
# check_cgroups.sh — verify cgroup v2 is usable for PageCacheFairnessBenchmark.
#
# Mirrors what benchmark.c does in setup_cgroups():
#   mkdir -p under /sys/fs/cgroup, enable +memory +io on parents,
#   write memory.max / memory.low / io.weight from the cgroup ini.
#
# Usage:
#   ./check_cgroups.sh
#   ./check_cgroups.sh --cgroup-config cgroup_isolated.ini
#   sudo ./check_cgroups.sh --cgroup-config cgroup_shared.ini

set -euo pipefail

CGROUP_ROOT="/sys/fs/cgroup"
CGROUP_CONFIG="cgroup_shared.ini"
BENCHMARK_BIN="./benchmark"
TEST_PREFIX="pagecache_fairness_check"
CLEANUP_PATHS=()

usage() {
    cat <<'EOF'
Usage: check_cgroups.sh [options]

Options:
  --cgroup-config PATH   Cgroup layout ini to validate (default: cgroup_shared.ini)
  -h, --help             Show this help

Run with sudo (or as root) on Linux with cgroup v2. Safe to run on macOS — prints
a platform notice and exits.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cgroup-config) CGROUP_CONFIG="${2:?missing path after --cgroup-config}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m⚠\033[0m  %s\n' "$*"; }
fail() { printf '   \033[31m✗\033[0m %s\n' "$*"; }

register_cleanup() {
    CLEANUP_PATHS+=("$1")
}

cleanup() {
    local path
    for (( i=${#CLEANUP_PATHS[@]}-1; i>=0; i-- )); do
        path="${CLEANUP_PATHS[$i]}"
        if [[ -d "$path" ]]; then
            rmdir "$path" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT

run_privileged() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        return 1
    fi
}

write_file() {
    local path="$1" val="$2"
    printf '%s\n' "$val" | run_privileged tee "$path" >/dev/null
}

read_file() {
    cat "$1" 2>/dev/null || true
}

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "=== PageCache Fairness — Cgroup Diagnostics ==="
    echo
    warn "Not Linux ($(uname -s)). benchmark.c skips cgroup setup here."
    echo "   Build locally; run this script and the benchmark on a Linux host with cgroup v2."
    exit 0
fi

if [[ ! -f "$CGROUP_CONFIG" ]]; then
    echo "error: cgroup config not found: $CGROUP_CONFIG" >&2
    exit 1
fi

echo "=== PageCache Fairness — Cgroup Diagnostics ==="
echo "   config: $CGROUP_CONFIG"
echo

FAILURES=0
WARNINGS=0
SETTING_OK=0
SETTING_FAIL=0

note_fail() { FAILURES=$((FAILURES + 1)); }
note_warn() { WARNINGS=$((WARNINGS + 1)); }

# ------------------------------------------------------------------ #
# 1. cgroup v2 mount
# ------------------------------------------------------------------ #
echo "1. Cgroup v2 mount"
if [[ "$(stat -fc %T "$CGROUP_ROOT" 2>/dev/null || echo missing)" == "cgroup2fs" ]]; then
    ok "cgroup v2 mounted at $CGROUP_ROOT (cgroup2fs)"
else
    fail "expected cgroup2fs at $CGROUP_ROOT"
    echo "   got: $(stat -fc %T "$CGROUP_ROOT" 2>/dev/null || echo 'unavailable')"
    note_fail
fi
echo

# ------------------------------------------------------------------ #
# 2. Root controllers
# ------------------------------------------------------------------ #
echo "2. Available controllers at root"
if [[ -f "$CGROUP_ROOT/cgroup.controllers" ]]; then
    ROOT_CTRL=$(read_file "$CGROUP_ROOT/cgroup.controllers")
    echo "   $ROOT_CTRL"
    for need in memory io; do
        if [[ " $ROOT_CTRL " == *" $need "* ]]; then
            ok "controller '$need' available"
        else
            fail "controller '$need' missing (benchmark needs memory + io)"
            note_fail
        fi
    done
else
    fail "$CGROUP_ROOT/cgroup.controllers not found"
    note_fail
fi
echo

echo "3. Enabled subtree controllers at root"
if [[ -f "$CGROUP_ROOT/cgroup.subtree_control" ]]; then
    ROOT_SUB=$(read_file "$CGROUP_ROOT/cgroup.subtree_control")
    if [[ -z "${ROOT_SUB// }" ]]; then
        warn "root cgroup.subtree_control is empty (benchmark will try to enable +memory +io)"
        note_warn
    else
        echo "   $ROOT_SUB"
    fi
else
    fail "$CGROUP_ROOT/cgroup.subtree_control not found"
    note_fail
fi
echo

# ------------------------------------------------------------------ #
# 4. Permissions
# ------------------------------------------------------------------ #
echo "4. Permissions"
if [[ "$(id -u)" -eq 0 ]]; then
    ok "running as root"
elif groups 2>/dev/null | grep -qw sudo; then
    ok "user in sudo group"
    if sudo -n true 2>/dev/null; then
        ok "passwordless sudo available"
    else
        warn "sudo requires a password — re-run with: sudo $0 --cgroup-config $CGROUP_CONFIG"
        note_warn
    fi
else
    warn "not root and not in sudo — cgroup writes will likely fail"
    note_warn
fi

if [[ -w /proc/sys/vm/drop_caches ]] || run_privileged test -w /proc/sys/vm/drop_caches; then
    ok "drop_caches writable (needed for cached-mode phases)"
else
    warn "cannot write /proc/sys/vm/drop_caches (run with sudo for -m cached)"
    note_warn
fi
echo

# ------------------------------------------------------------------ #
# 5. systemd context
# ------------------------------------------------------------------ #
echo "5. systemd cgroup management"
SYSTEMD_MANAGED=false
if command -v systemctl >/dev/null 2>&1; then
    ok "systemd present"
    if [[ -d "$CGROUP_ROOT/system.slice" ]]; then
        ok "systemd is managing cgroups"
        SYSTEMD_MANAGED=true
    fi
else
    echo "   systemd not detected"
fi
if [[ "$SYSTEMD_MANAGED" == true && -f "$CGROUP_ROOT/user.slice/cgroup.subtree_control" ]]; then
    USER_SLICE_CTRL=$(read_file "$CGROUP_ROOT/user.slice/cgroup.subtree_control")
    if [[ -z "${USER_SLICE_CTRL// }" ]]; then
        warn "user.slice has no delegated controllers (non-root benchmark may fail)"
        note_warn
    else
        echo "   user.slice delegated: $USER_SLICE_CTRL"
    fi
fi
echo

# ------------------------------------------------------------------ #
# 6. Parse cgroup ini (same keys as benchmark.c)
# ------------------------------------------------------------------ #
echo "6. Parsed cgroup layout ($CGROUP_CONFIG)"
declare -a SECTIONS=()
declare -a CGROUP_NAMES=()
declare -a MEMORY_MAX=()
declare -a MEMORY_LOW=()
declare -a IO_WEIGHT=()

current=-1
while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"; line="${line%%;*}"; line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    if [[ "$line" == \[* ]]; then
        section="${line#[}"; section="${section%]}"
        current=$((current + 1))
        SECTIONS[$current]="$section"
        CGROUP_NAMES[$current]="$section"
        MEMORY_MAX[$current]=""
        MEMORY_LOW[$current]=""
        IO_WEIGHT[$current]=""
        continue
    fi
    [[ $current -lt 0 ]] && continue
    key="${line%%=*}"; key="${key%"${key##*[![:space:]]}"}"
    val="${line#*=}"; val="${val#"${val%%[![:space:]]*}"}"
    case "$key" in
        cgroup_name) CGROUP_NAMES[$current]="$val" ;;
        memory.max)  MEMORY_MAX[$current]="$val" ;;
        memory.low)  MEMORY_LOW[$current]="$val" ;;
        io.weight)   IO_WEIGHT[$current]="$val" ;;
    esac
done < "$CGROUP_CONFIG"

if [[ ${#SECTIONS[@]} -eq 0 ]]; then
    fail "no sections found in $CGROUP_CONFIG"
    exit 1
fi

for i in "${!SECTIONS[@]}"; do
    printf '   [%s] path=%s' "${SECTIONS[$i]}" "${CGROUP_NAMES[$i]}"
    [[ -n "${MEMORY_MAX[$i]}" ]]  && printf ' memory.max=%s' "${MEMORY_MAX[$i]}"
    [[ -n "${MEMORY_LOW[$i]}" ]]  && printf ' memory.low=%s' "${MEMORY_LOW[$i]}"
    [[ -n "${IO_WEIGHT[$i]}" ]]   && printf ' io.weight=%s' "${IO_WEIGHT[$i]}"
    printf '\n'
done
echo

# ------------------------------------------------------------------ #
# 7. Recreate benchmark cgroup tree under a test prefix
# ------------------------------------------------------------------ #
echo "7. Benchmark-style cgroup setup (test tree under $TEST_PREFIX/)"

enable_controllers() {
    local cg_path="$1"
    if [[ ! -f "$cg_path/cgroup.subtree_control" ]]; then
        return 1
    fi
    write_file "$cg_path/cgroup.subtree_control" "+memory +io"
}

mkdir_p_cgroup() {
    local rel="$1"
    local acc="$CGROUP_ROOT"
    local -a parts
    local i

    enable_controllers "$CGROUP_ROOT" || true

    IFS='/' read -ra parts <<< "$rel"
    for (( i=0; i<${#parts[@]}; i++ )); do
        acc="$acc/${parts[$i]}"
        if ! run_privileged mkdir -p "$acc" 2>/dev/null; then
            fail "mkdir $acc"
            return 1
        fi
        register_cleanup "$acc"
        if (( i < ${#parts[@]} - 1 )); then
            enable_controllers "$acc" || true
        fi
    done
    return 0
}

test_setting() {
    local cg_path="$1" file="$2" val="$3"
    local full="$cg_path/$file"

    if [[ ! -f "$full" ]]; then
        fail "$file not present under $cg_path (is +$file controller delegated?)"
        SETTING_FAIL=$((SETTING_FAIL + 1))
        note_fail
        return 1
    fi
    if write_file "$full" "$val"; then
        ok "$file = $val  ($(basename "$cg_path"))"
        SETTING_OK=$((SETTING_OK + 1))
        return 0
    fi
    fail "$file = $val failed ($(basename "$cg_path"))"
    SETTING_FAIL=$((SETTING_FAIL + 1))
    note_fail
    return 1
}

# Sort cgroup paths by depth so parents are created first
mapfile -t SORTED_IDX < <(
    for i in "${!CGROUP_NAMES[@]}"; do
        depth=$(tr -cd '/' <<< "${CGROUP_NAMES[$i]}" | wc -c)
        printf '%d %d\n' "$depth" "$i"
    done | sort -n -k1,1 -k2,2 | awk '{print $2}'
)

for idx in "${SORTED_IDX[@]}"; do
    rel="${TEST_PREFIX}/${CGROUP_NAMES[$idx]}"
    echo "   --- ${SECTIONS[$idx]} -> $rel ---"
    if ! mkdir_p_cgroup "$rel"; then
        fail "could not create $CGROUP_ROOT/$rel"
        note_fail
        continue
    fi
    cg_path="$CGROUP_ROOT/$rel"

    [[ -n "${MEMORY_MAX[$idx]}" ]] && test_setting "$cg_path" "memory.max" "${MEMORY_MAX[$idx]}" || true
    [[ -n "${MEMORY_LOW[$idx]}" ]] && test_setting "$cg_path" "memory.low" "${MEMORY_LOW[$idx]}" || true
    [[ -n "${IO_WEIGHT[$idx]}"  ]] && test_setting "$cg_path" "io.weight"  "${IO_WEIGHT[$idx]}"  || true

    if [[ -z "${MEMORY_MAX[$idx]}${MEMORY_LOW[$idx]}${IO_WEIGHT[$idx]}" ]]; then
        warn "no writable limits in ini for [${SECTIONS[$idx]}] (directory only)"
        note_warn
    fi
done
echo

# ------------------------------------------------------------------ #
# 8. Process placement (cgroup.procs) on a leaf tenant
# ------------------------------------------------------------------ #
echo "8. Process placement (cgroup.procs)"
LEAF_REL=""
for idx in "${SORTED_IDX[@]}"; do
    if [[ "${SECTIONS[$idx]}" == client1_steady || "${SECTIONS[$idx]}" == client2_noisy ]]; then
        LEAF_REL="${TEST_PREFIX}/${CGROUP_NAMES[$idx]}"
        break
    fi
done
if [[ -z "$LEAF_REL" ]]; then
    last_idx=${SORTED_IDX[${#SORTED_IDX[@]}-1]}
    LEAF_REL="${TEST_PREFIX}/${CGROUP_NAMES[$last_idx]}"
fi
LEAF_PATH="$CGROUP_ROOT/$LEAF_REL"

if [[ -f "$LEAF_PATH/cgroup.procs" ]]; then
    if run_privileged bash -c "echo \$\$ > '$LEAF_PATH/cgroup.procs'"; then
        ok "shell can join $LEAF_REL (same mechanism as benchmark fio spawn)"
    else
        fail "could not write pid to $LEAF_PATH/cgroup.procs"
        note_fail
    fi
else
    fail "$LEAF_PATH/cgroup.procs missing"
    note_fail
fi
echo

# ------------------------------------------------------------------ #
# 9. Telemetry files the sampler reads
# ------------------------------------------------------------------ #
echo "9. Telemetry readability (memory.stat, PSI)"
if [[ -r "$LEAF_PATH/memory.stat" ]]; then
    refault=$(awk '$1=="workingset_refault_file"{print $2}' "$LEAF_PATH/memory.stat")
    dirty=$(awk '$1=="file_dirty"{print $2}' "$LEAF_PATH/memory.stat")
    ok "memory.stat readable (workingset_refault_file=${refault:-?}, file_dirty=${dirty:-?})"
else
    fail "cannot read $LEAF_PATH/memory.stat"
    note_fail
fi

for res in memory io; do
    psi_file="$LEAF_PATH/${res}.pressure"
    if [[ -r "$psi_file" ]]; then
        ok "${res}.pressure readable"
    else
        warn "${res}.pressure not readable (use --no-psi or fix delegation)"
        note_warn
    fi
done
echo

# ------------------------------------------------------------------ #
# 10. Optional: compiled benchmark present
# ------------------------------------------------------------------ #
echo "10. Benchmark binary"
if [[ -x "$BENCHMARK_BIN" ]]; then
    ok "$BENCHMARK_BIN exists and is executable"
else
    warn "$BENCHMARK_BIN not found — run 'make' before the benchmark"
    note_warn
fi
echo

# ------------------------------------------------------------------ #
# Summary
# ------------------------------------------------------------------ #
echo "=== Summary ==="
echo "   cgroup settings tested: $SETTING_OK ok, $SETTING_FAIL failed"
echo "   hard failures: $FAILURES   warnings: $WARNINGS"
echo

if [[ $FAILURES -eq 0 && $SETTING_FAIL -eq 0 ]]; then
    echo "✅ Cgroup setup looks good for PageCacheFairnessBenchmark."
    echo
    echo "   sudo $BENCHMARK_BIN --cgroup-config $CGROUP_CONFIG -m cached dual"
elif [[ $SETTING_OK -gt 0 ]]; then
    echo "⚠️  Partial cgroup support — some limits may be ignored at runtime."
    echo
    if [[ "$SYSTEMD_MANAGED" == true ]]; then
        echo "   Try running inside a delegated scope:"
        echo "   sudo systemd-run --scope -p Delegate=yes \\"
        echo "     $BENCHMARK_BIN --cgroup-config $CGROUP_CONFIG -m cached dual"
        echo
        echo "   Or run the benchmark as root:"
        echo "   sudo $BENCHMARK_BIN --cgroup-config $CGROUP_CONFIG -m cached dual"
    else
        echo "   sudo $BENCHMARK_BIN --cgroup-config $CGROUP_CONFIG -m cached dual"
    fi
else
    echo "✗ Cgroup setup is not usable for this benchmark."
    echo
    echo "   Requirements:"
    echo "   - Linux with cgroup v2 (stat -fc %T /sys/fs/cgroup = cgroup2fs)"
    echo "   - root or sudo for /sys/fs/cgroup writes and drop_caches"
    echo "   - memory + io controllers delegated on the path you create"
    echo
    echo "   Re-run: sudo $0 --cgroup-config $CGROUP_CONFIG"
fi
echo

exit $(( FAILURES > 0 || SETTING_FAIL > 0 ? 1 : 0 ))

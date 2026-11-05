#!/usr/bin/env bash
# setup_test_files.sh — create dense, size-tagged test files once and reuse
# them across experiments (test_file_1G, test_file_8G, ...).
#
# Usage:
#   ./setup_test_files.sh              # default: 1G + 8G
#   ./setup_test_files.sh 1G 8G 48G    # custom sizes
set -euo pipefail

create_if_missing() {
  local size=$1
  local file="test_file_${size}"

  if [[ -f "$file" ]]; then
    local want have
    case "$size" in
      *T|*t) want=$(( ${size%[Tt]} * 1024 * 1024 * 1024 * 1024 )) ;;
      *G|*g) want=$(( ${size%[Gg]} * 1024 * 1024 * 1024 )) ;;
      *M|*m) want=$(( ${size%[Mm]} * 1024 * 1024 )) ;;
      *K|*k) want=$(( ${size%[Kk]} * 1024 )) ;;
      *)     want=$size ;;
    esac
    have=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null || echo 0)
    if [[ "$have" -eq "$want" ]]; then
      echo "exists: $file ($size)"
      return
    fi
    echo "wrong size: $file ($have bytes, want $want); recreating..."
    rm -f "$file"
  fi

  echo "creating $file ($size) with random data (this may take a while)..."
  # Real writes (not --create_only) so the file is dense and cacheable.
  # Unique randseed per size so test_file_1G ≠ test_file_8G; refill_buffers
  # so each 1M write gets new bytes (otherwise fio repeats one buffer).
  local seed
  seed=$(cksum <<<"$size" | awk '{print $1}')
  fio --name="fill_${size}" --filename="$file" --size="$size" \
      --rw=write --bs=1M --ioengine=libaio --direct=1 \
      --refill_buffers --randrepeat=0 --randseed="$seed" \
      --buffer_compress_percentage=0 >/dev/null
  echo "done: $file"
}

if [[ $# -eq 0 ]]; then
  set -- 1G 8G
fi

for size in "$@"; do
  create_if_missing "$size"
done

echo "test files ready."

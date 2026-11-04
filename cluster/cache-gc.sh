#!/bin/bash
# Prune the per-group package caches under /var/lib/sbx-cache (installed as sbx-cache-gc.timer).
# 1) drop files not read for MAX_AGE_DAYS; 2) if the node total is still above CAP_GB, drop the
# least recently read files until it fits. Empty dirs are removed afterwards. Never touches
# anything outside ROOT.
set -euo pipefail
ROOT=${SBX_CACHE_ROOT:-/var/lib/sbx-cache}
MAX_AGE_DAYS=${SBX_CACHE_MAX_AGE_DAYS:-14}
CAP_GB=${SBX_CACHE_CAP_GB:-30}
[ -d "$ROOT" ] || exit 0
# Refuse anything that is not a dedicated cache root: deleting under / or a system dir would be fatal.
ROOT=$(realpath -e "$ROOT")
case "$ROOT" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib)
    echo "sbx-cache-gc: refusing to prune system directory $ROOT" >&2; exit 2 ;;
esac
[ -e "$ROOT/.sbx-cache-root" ] || [ "${SBX_CACHE_ROOT:-}" = "" ] || {
  echo "sbx-cache-gc: $ROOT has no .sbx-cache-root marker; create it to confirm this is a cache root" >&2; exit 2; }
before=$(du -sb "$ROOT" | cut -f1)
find "$ROOT" -xdev -mindepth 2 -type f -atime +"$MAX_AGE_DAYS" -delete
cap=$((CAP_GB * 1024 * 1024 * 1024))
total=$(du -sb "$ROOT" | cut -f1)
if [ "$total" -gt "$cap" ]; then
  find "$ROOT" -xdev -mindepth 2 -type f -printf '%A@ %s %p\0' | sort -z -n | \
  while IFS= read -r -d '' rec; do
    [ "$total" -le "$cap" ] && break
    size=${rec#* }; size=${size%% *}; path=${rec#* * }
    rm -f -- "$path" && total=$((total - size))
  done
fi
find "$ROOT" -xdev -mindepth 3 -type d -empty -delete 2>/dev/null || true
after=$(du -sb "$ROOT" | cut -f1)
echo "sbx-cache-gc: $ROOT ${before}B -> ${after}B (age>${MAX_AGE_DAYS}d, cap ${CAP_GB}G)"

#!/usr/bin/env bash
# bench_nodict.sh CORPUS LEVEL
#
# Batch-compress corpora/<CORPUS>/heldout with the built zstd, no dictionary,
# and report the raw/compressed byte totals and ratio. Used as the reference
# ("no dict") baseline point for a corpus/level pair.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZSTD="$ROOT/third_party/zstd/programs/zstd"
CORPUS="${1:?usage: bench_nodict.sh CORPUS LEVEL}"
LEVEL="${2:?usage: bench_nodict.sh CORPUS LEVEL}"
HELDOUT="$ROOT/corpora/$CORPUS/heldout"

[ -x "$ZSTD" ] || { echo "zstd binary not found/built at $ZSTD" >&2; exit 1; }
[ -d "$HELDOUT" ] || { echo "no such corpus/heldout dir: $HELDOUT" >&2; exit 1; }

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

"$ZSTD" -q -f "-$LEVEL" -r "$HELDOUT" --output-dir-flat="$OUT"

python3 - "$HELDOUT" "$OUT" <<'PY'
import os, sys
heldout, out = sys.argv[1], sys.argv[2]
raw = sum(os.path.getsize(os.path.join(heldout, f)) for f in os.listdir(heldout))
comp = sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out))
print(f"raw_bytes={raw} comp_bytes={comp} ratio={raw/comp:.6f}")
PY

#!/usr/bin/env bash
# test_smoke.sh -- tiny end-to-end smoke test for dictforge.
#
# Generates a small synthetic corpus (<=200 tiny files), trains a
# dictionary on it with a 60s time budget at level 3 (stage-1 only in
# practice, since level 3 < 16 skips stages 2/3 -- no native helpers
# needed), then round-trips 3 of the corpus files through the STOCK
# system zstd binary (whatever `which zstd` finds) using the trained
# dictionary, to prove the output is a plain, standard-format zstd
# dictionary that any zstd install can use.
#
# Exits 0 on success, 1 on any failure.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOCK_ZSTD="$(command -v zstd || true)"

fail() {
    echo "test_smoke.sh: FAIL: $*" >&2
    exit 1
}

if [ -z "$STOCK_ZSTD" ]; then
    fail "no 'zstd' binary found on PATH; install it (e.g. brew install zstd / apt install zstd) to run this test"
fi
echo "test_smoke.sh: using stock zstd: $STOCK_ZSTD ($($STOCK_ZSTD --version 2>&1))"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/dictforge_smoke.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

CORPUS_DIR="$WORKDIR/corpus"
mkdir -p "$CORPUS_DIR"

echo "test_smoke.sh: generating synthetic corpus (200 tiny files) in $CORPUS_DIR"
for i in $(seq -w 1 200); do
    {
        printf '{"event":"smoke_test","id":%d,"tag":"dictforge-smoke",' "$((10#$i))"
        printf '"payload":"%s-%s-%s"}\n' "$RANDOM" "$RANDOM" "$RANDOM"
    } > "$CORPUS_DIR/rec_$i.json"
done

DICT_OUT="$WORKDIR/smoke.dict"

echo "test_smoke.sh: training (time-budget-s=60, level=3)"
python3 "$HERE/train.py" \
    --train-dir "$CORPUS_DIR" \
    --out "$DICT_OUT" \
    --target-level 3 \
    --time-budget-s 60 \
    --max-budget 65536 \
    || fail "train.py exited non-zero"

[ -s "$DICT_OUT" ] || fail "trained dictionary is missing or empty: $DICT_OUT"
echo "test_smoke.sh: trained dictionary: $DICT_OUT ($(wc -c < "$DICT_OUT" | tr -d ' ') bytes)"

echo "test_smoke.sh: round-tripping 3 corpus files through stock zstd + trained dict"
count=0
for f in "$CORPUS_DIR"/rec_001.json "$CORPUS_DIR"/rec_050.json "$CORPUS_DIR"/rec_100.json; do
    [ -f "$f" ] || fail "expected smoke corpus file missing: $f"
    zst="$WORKDIR/$(basename "$f").zst"
    out="$WORKDIR/$(basename "$f").out"

    "$STOCK_ZSTD" -q -f -3 -D "$DICT_OUT" -o "$zst" "$f" \
        || fail "stock zstd compress failed for $f"
    "$STOCK_ZSTD" -q -f -d -D "$DICT_OUT" -o "$out" "$zst" \
        || fail "stock zstd decompress failed for $f"
    cmp -s "$f" "$out" \
        || fail "round-trip mismatch for $f (decompressed output != original)"

    count=$((count + 1))
    echo "test_smoke.sh:   OK: $(basename "$f") ($(wc -c < "$f" | tr -d ' ') -> $(wc -c < "$zst" | tr -d ' ') bytes, round-trip verified)"
done

[ "$count" -eq 3 ] || fail "expected to verify 3 files, verified $count"

echo "test_smoke.sh: PASS (dictionary trained by dictforge, used entirely by stock zstd $STOCK_ZSTD)"
exit 0

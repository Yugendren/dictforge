#!/usr/bin/env bash
# build_native.sh -- compile dictforge's native helpers (refine_dict,
# offset_hist) against a zstd source checkout's static lib.
#
# Usage:
#   ./build_native.sh                     # uses ../third_party/zstd
#   ZSTD_DIR=/path/to/zstd ./build_native.sh
#
# ZSTD_DIR must be a zstd source checkout with a built lib/libzstd.a
# (i.e. `make lib` has been run in it). Only the static lib and its
# public + zdict headers are needed; nothing else from ZSTD_DIR is used.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZSTD_DIR="${ZSTD_DIR:-$HERE/../third_party/zstd}"
LIBZSTD="$ZSTD_DIR/lib/libzstd.a"

if [ ! -f "$LIBZSTD" ]; then
    echo "build_native.sh: $LIBZSTD not found." >&2
    echo "" >&2
    echo "  ZSTD_DIR is currently: $ZSTD_DIR" >&2
    echo "  Get a zstd source checkout (e.g. git clone --depth 1" >&2
    echo "  https://github.com/facebook/zstd) and build its static lib:" >&2
    echo "" >&2
    echo "      cd /path/to/zstd && make lib" >&2
    echo "" >&2
    echo "  Then either set ZSTD_DIR=/path/to/zstd before running this" >&2
    echo "  script, or place the checkout at $HERE/../third_party/zstd" >&2
    exit 1
fi

mkdir -p "$HERE/native/bin"

echo "build_native.sh: using ZSTD_DIR=$ZSTD_DIR"

cc -O2 -Wall -I "$ZSTD_DIR/lib" \
    -o "$HERE/native/bin/refine_dict" "$HERE/native/refine_dict.c" "$LIBZSTD"
echo "build_native.sh: built native/bin/refine_dict"

cc -O2 -Wall -I "$ZSTD_DIR/lib" \
    -o "$HERE/native/bin/offset_hist" "$HERE/native/offset_hist.c" "$LIBZSTD"
echo "build_native.sh: built native/bin/offset_hist"

echo "build_native.sh: done. Verify with: native/bin/refine_dict validate"

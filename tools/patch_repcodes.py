#!/usr/bin/env python3
"""
patch_repcodes.py -- overwrite a zstd dictionary's default repeat-offset
codes (1, 4, 8) with custom values.

zstd dictionaries store three repeat-offset seed values as consecutive
little-endian uint32 fields, immediately followed by the raw dictionary
content. In a freshly finalized dictionary (ZDICT_finalizeDictionary /
`zstd --train`) these are always the format's defaults: 1, 4, 8 -- i.e.
the 12-byte sequence `01 00 00 00 04 00 00 00 08 00 00 00`. Locating and
overwriting that exact byte sequence lets us seed the decoder's repeat-
offset cache with offsets that are actually common in the training
corpus, which can improve compression for corpora with a small number
of dominant match distances (e.g. fixed-width record formats).

The field must occur exactly once in the dictionary; if it's absent or
appears more than once (e.g. the raw content itself happens to contain
the same 12 bytes) this aborts rather than guessing.

CLI:
    patch_repcodes.py <dict_file> <off1> <off2> <off3> [--out OUT]

Library:
    patch_repcodes(dict_bytes: bytes, offsets: tuple[int, int, int]) -> bytes
"""
import argparse
import struct
import sys

DEFAULT_REPCODES = struct.pack("<III", 1, 4, 8)


def find_repcode_field(dict_bytes: bytes) -> int:
    """Return the byte offset of the unique default-repcodes field, or
    raise ValueError if it's absent or not unique."""
    first = dict_bytes.find(DEFAULT_REPCODES)
    if first < 0:
        raise ValueError("default repcode sequence (1,4,8) not found in dictionary")
    second = dict_bytes.find(DEFAULT_REPCODES, first + 1)
    if second >= 0:
        raise ValueError(
            f"default repcode sequence is not unique (found at offsets {first} and {second})"
        )
    return first


def patch_repcodes(dict_bytes: bytes, offsets) -> bytes:
    """Return a copy of dict_bytes with the default repcode field
    overwritten by `offsets` (three positive ints, LE32-encoded).

    Validates 0 < off <= contentSize for each offset, where contentSize
    is the number of bytes following the repcode field (i.e. the raw
    dictionary content the offsets are meant to index into).
    """
    off1, off2, off3 = offsets
    pos = find_repcode_field(dict_bytes)
    field_end = pos + len(DEFAULT_REPCODES)
    content_size = len(dict_bytes) - field_end
    for name, off in (("off1", off1), ("off2", off2), ("off3", off3)):
        if not (0 < off <= content_size):
            raise ValueError(
                f"{name}={off} out of range: must satisfy 0 < off <= contentSize ({content_size})"
            )
    patched = bytearray(dict_bytes)
    patched[pos:field_end] = struct.pack("<III", off1, off2, off3)
    return bytes(patched)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("dict_file")
    ap.add_argument("off1", type=int)
    ap.add_argument("off2", type=int)
    ap.add_argument("off3", type=int)
    ap.add_argument("--out", default=None,
                     help="output path (default: overwrite dict_file in place)")
    args = ap.parse_args()

    with open(args.dict_file, "rb") as f:
        data = f.read()

    try:
        patched = patch_repcodes(data, (args.off1, args.off2, args.off3))
    except ValueError as e:
        print(f"patch_repcodes: {e}", file=sys.stderr)
        sys.exit(1)

    out_path = args.out or args.dict_file
    with open(out_path, "wb") as f:
        f.write(patched)
    print(f"patched {out_path}: repcodes -> ({args.off1}, {args.off2}, {args.off3})")


if __name__ == "__main__":
    main()

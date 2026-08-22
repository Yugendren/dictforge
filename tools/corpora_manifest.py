#!/usr/bin/env python3
"""Print a manifest (file counts, byte totals) for each corpus/split under corpora/."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPORA_DIR = ROOT / "corpora"


def dir_stats(d: Path):
    n, total = 0, 0
    for f in d.iterdir():
        if f.is_file():
            n += 1
            total += f.stat().st_size
    return n, total


def main():
    names = sorted(
        p.name for p in CORPORA_DIR.iterdir()
        if p.is_dir() and (p / "train").is_dir() and (p / "heldout").is_dir()
    )
    print(f"{'corpus':<14} {'train_n':>8} {'train_bytes':>12} {'heldout_n':>10} {'heldout_bytes':>14}")
    for name in names:
        tn, tb = dir_stats(CORPORA_DIR / name / "train")
        hn, hb = dir_stats(CORPORA_DIR / name / "heldout")
        print(f"{name:<14} {tn:>8} {tb:>12} {hn:>10} {hb:>14}")


if __name__ == "__main__":
    main()

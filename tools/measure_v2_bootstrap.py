#!/usr/bin/env python3
"""
measure_v2_bootstrap.py -- paired bootstrap CI for the T3 block of
results/measure_v2.csv. Run inside the .venv (needs numpy); the caller
(tools/run_measure_v2.py, running under the system python3) shells out to
this script so the numpy dependency stays isolated to the venv.

Reads a JSON object from stdin:
    {"raw": [...], "default": [...], "full": [...]}
where raw[i]/default[i]/full[i] are the raw file size, default-dict
compressed size, and full-dict compressed size (bytes) for heldout file i
(same order, paired). Computes delta% = (ratio_full/ratio_default - 1)*100
where ratio_x = sum(raw[idx]) / sum(comp_x[idx]) over a set of (possibly
resampled) file indices idx -- i.e. an aggregate corpus-level ratio, not a
per-file mean.

Point estimate is delta% on the full (unresampled) sample. Then 10,000
paired bootstrap resamples (seed 1729, sampling file indices with
replacement, same indices applied to both default and full arrays) give
the 2.5% / 97.5% percentiles of the resampling distribution. Resampling is
done in chunks (default 500) to bound peak memory.

Prints a JSON object to stdout:
    {"n": N, "point": P, "ci_lo": LO, "ci_hi": HI}
"""
import json
import sys

import numpy as np

SEED = 1729
N_BOOT = 10000
CHUNK = 500


def delta_pct(raw_sum, comp_default_sum, comp_full_sum):
    ratio_default = raw_sum / comp_default_sum
    ratio_full = raw_sum / comp_full_sum
    return (ratio_full / ratio_default - 1.0) * 100.0


def main():
    payload = json.load(sys.stdin)
    raw = np.asarray(payload["raw"], dtype=np.float64)
    comp_default = np.asarray(payload["default"], dtype=np.float64)
    comp_full = np.asarray(payload["full"], dtype=np.float64)
    n = len(raw)
    assert len(comp_default) == n and len(comp_full) == n, "paired arrays must be equal length"

    point = delta_pct(raw.sum(), comp_default.sum(), comp_full.sum())

    rng = np.random.default_rng(SEED)
    deltas = np.empty(N_BOOT, dtype=np.float64)
    done = 0
    while done < N_BOOT:
        b = min(CHUNK, N_BOOT - done)
        idx = rng.integers(0, n, size=(b, n))  # (b, n) resampled file indices
        raw_r = raw[idx].sum(axis=1)
        cd_r = comp_default[idx].sum(axis=1)
        cf_r = comp_full[idx].sum(axis=1)
        deltas[done:done + b] = delta_pct(raw_r, cd_r, cf_r)
        done += b

    ci_lo, ci_hi = np.percentile(deltas, [2.5, 97.5])
    print(json.dumps({"n": n, "point": float(point), "ci_lo": float(ci_lo), "ci_hi": float(ci_hi)}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
run_campaign_v3.py -- regenerate the MAIN benchmark rows under the
post-SIZE_LADDER-fix trainer (see tools/trainer.py SIZE_LADDER, fixed to
add fine-grained rungs in the 196608..2097152 region after a finer
equal-compute sweep found true optima at 1310720/1572864 bytes that the
old powers-of-two ladder could never reach).

This does NOT reimplement evaluation: it imports tools/run_campaign.py
and calls its run_main() helper directly, so the fit/val split, the
default/tuned/full/rawconcat dict builders, and the heldout batch-
compress evaluation are byte-for-byte the same code that produced
results/benchmark_v2.csv. Only two module globals are monkey-patched
before calling in:

    CSV_PATH -> results/benchmark_v3.csv   (new, not benchmark_v2.csv)
    RUNDIR   -> runs_v3/campaign_v3        (new, not runs/campaign_v2)

results/benchmark_v2.csv and everything under runs/ are frozen evidence
and are never opened for writing by this script.

Resume-safe: like run_campaign.py, on startup (and before every variant)
it re-reads the CSV and skips any (experiment,corpus,level,variant) key
already present, so it can be killed and rerun freely.

Time budgets are UNCHANGED from run_campaign.py (600s @ L3, 1800s @ L19)
-- deliberately not reduced by the "cap yourself at 5 cores" courtesy
constraint, because run_campaign.py's builders hardcode zstd -T8 and
trainer.py hardcodes TRAIN_THREADS=8; touching either would change how
much of the (now-finer) ladder fits inside the same wall-clock budget
and make v3 not comparable to v2 on that axis. Instead this driver (a)
runs corpora/levels strictly serially (never more than one training job
live at a time), and (b) is meant to be launched under `nice -n 10` so
the OS scheduler yields to other agents' work without changing the
experiment itself.

Run (detached):
    mkdir -p runs_v3
    nohup nice -n 10 python3 tools/run_campaign_v3.py \
        > runs_v3/campaign_v3.log 2>&1 &
    disown
"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import run_campaign as rc  # noqa: E402  -- reuse its helpers verbatim

rc.CSV_PATH = ROOT / "results" / "benchmark_v3.csv"
rc.RUNDIR = ROOT / "runs_v3" / "campaign_v3"

MIN_FREE_GB = 8.0


def check_disk(label):
    free_gb = shutil.disk_usage("/").free / 1e9
    rc.log(f"disk check ({label}): {free_gb:.1f} GiB free on /")
    if free_gb < MIN_FREE_GB:
        raise SystemExit(
            f"ABORT: only {free_gb:.1f} GiB free on / (< {MIN_FREE_GB} GiB minimum) "
            f"before {label}; stopping to avoid starving other agents' disk usage."
        )


def git_commit_v3(corpus):
    ok, _rc, out, err = rc.run(["git", "add", str(rc.CSV_PATH)], cwd=str(ROOT))
    if not ok:
        rc.log(f"git add failed: {err}")
        return
    ok, _rc, out, err = rc.run(["git", "diff", "--cached", "--quiet"], cwd=str(ROOT))
    if ok:  # rc==0 means no staged diff
        rc.log(f"git commit skipped for {corpus}: no changes to {rc.CSV_PATH.name}")
        return
    msg = (
        f"Regenerate benchmark_v3 MAIN rows for {corpus} "
        f"(post SIZE_LADDER-fix trainer)\n\n"
        f"Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    )
    ok, _rc, out, err = rc.run(["git", "commit", "-m", msg], cwd=str(ROOT))
    if ok:
        rc.log(f"git commit OK for {corpus}")
    else:
        rc.log(f"git commit FAILED for {corpus}: {err}")


def main():
    rc.ensure_csv_header()
    rc.log("=== dictforge benchmark_v3 MAIN regeneration starting ===")
    rc.log(f"CSV_PATH={rc.CSV_PATH} RUNDIR={rc.RUNDIR}")
    rc.log(f"zstd={rc.ZSTD} exists={rc.ZSTD.exists()}; trainer={rc.TRAINER} exists={rc.TRAINER.exists()}")
    check_disk("startup")

    failures = []
    for corpus in rc.CORPORA:
        check_disk(f"corpus={corpus}")
        rc.log(f"--- corpus: {corpus} ---")
        for level in rc.LEVELS:
            check_disk(f"corpus={corpus} level={level}")
            t0 = time.monotonic()
            try:
                rc.run_main(rc.load_done(), corpus, level)
            except Exception as exc:  # noqa: BLE001 -- one bad cell must not kill the campaign
                msg = f"{type(exc).__name__}: {exc}"[:400]
                rc.log(f"!!! MAIN {corpus} L{level} FAILED, continuing: {msg}")
                failures.append((corpus, level, msg))
                rc.append_row("MAIN", corpus, level, "BLOCK_FAILED", 0, 0.0, 0.0, msg)
            rc.log(f"    {corpus} L{level} done in {time.monotonic() - t0:.1f}s")
        git_commit_v3(corpus)
        rc.log(f"--- corpus DONE: {corpus} ---")

    if failures:
        rc.log(f"=== campaign finished with {len(failures)} failed blocks ===")
        for f in failures:
            rc.log(f"    FAILED: {f}")
    rc.log("=== dictforge benchmark_v3 MAIN regeneration COMPLETE ===")


if __name__ == "__main__":
    main()

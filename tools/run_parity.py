#!/usr/bin/env python3
"""
run_parity.py -- resume-safe runner for the dictforge production-parity /
matched-size campaign.

Motivation: benchmark_v2.csv's headline gains compare large trainer.py
dictionaries against the 110KB zstd default. This campaign additionally
reports EQUAL-SIZE and production-default-size comparisons, at the
configurations real deployments actually use (Cassandra 64KiB@L3, Scylla
RPC path @L1, size-matched head-to-head at our own winning sizes, and the
RocksDB use_zstd_dict_trainer=false fallback).

Writes rows to results/parity_v2.csv:
    experiment,corpus,level,variant,dict_size,ratio,train_wall_s,note

Safe to kill and rerun: on startup it reads the CSV and skips any
(experiment,corpus,level,variant) key already present.

Experiments (see PLAN in the task spec):
    A  Cassandra parity:    64KiB dict, L3.        default64 / ours64
    B  Scylla RPC parity:   L1 eval.                default110_L1 / ours_L1
    C  Matched-size head-to-head: L3 and L19, at our own winning size S
       (S read from results/benchmark_v2.csv MAIN/full).  default_matched
       (ours is NOT rerun here -- it's the existing MAIN/full row).
    D  RocksDB fallback: ZDICT_finalizeDictionary over raw concatenated
       train samples (~111KB), at L3 and L19.       rocksdb_finalize

Reuses helpers from tools/run_campaign.py (eval_ratio, timed_run,
build_default, build_rawconcat, get_full_variant_row, git patterns) so the
measurement methodology is identical to the benchmark_v2 campaign.

Run: python3 tools/run_parity.py 2>&1 | tee -a runs/parity_v2.log
Intended to be launched detached (nohup ... & disown) and polled via its
log file, exactly like run_campaign.py.
"""
import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import run_campaign as rc  # noqa: E402  -- reuse eval_ratio/timed_run/etc.

ZSTD = rc.ZSTD
TRAINER = rc.TRAINER
REFINE_DICT = ROOT / "tools" / "refine_dict"
CSV_PATH = ROOT / "results" / "parity_v2.csv"
RUNDIR = ROOT / "runs" / "parity_v2"

CORPORA = ["github_users", "gharchive", "weblogs", "apijson", "csvrows"]
FIELDS = ["experiment", "corpus", "level", "variant", "dict_size", "ratio", "train_wall_s", "note"]

SEED = rc.SEED  # 1729 -- keep raw-concat sampling identical to benchmark_v2

# Generous subprocess timeouts. Per the task spec: trainer.py at L19 with
# large budgets can take 30-90 min/cell, so any subprocess timeout here
# must be >=4h. build_trainer_local() computes cap = max(4h, budget*6);
# ZSTD_TRAIN_TIMEOUT below covers the plain `zstd --train` (fastCover)
# calls in A/default64 and C/default_matched, which are empirically fast
# (single-digit to low-double-digit seconds even at maxdict=2MB) but are
# still given a 4h ceiling rather than reusing run_campaign's tighter
# 6000s constant.
ZSTD_TRAIN_TIMEOUT = 4 * 3600
REFINE_TIMEOUT = 4 * 3600

MAXDICT_A = 65536          # Cassandra real default: 64KiB
MAXDICT_D = 111 * 1024     # RocksDB fallback content size: ~111KB


def log(msg):
    rc.log(msg)


# ---------------------------------------------------------------------
# CSV bookkeeping (parity_v2.csv -- separate file from benchmark_v2.csv)
# ---------------------------------------------------------------------

def load_done():
    done = set()
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="") as f:
            for row in csv.DictReader(f):
                done.add((row["experiment"], row["corpus"], str(row["level"]), row["variant"]))
    return done


def ensure_csv_header():
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def append_row(experiment, corpus, level, variant, dict_size, ratio, train_wall_s, note):
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writerow({
            "experiment": experiment, "corpus": corpus, "level": level, "variant": variant,
            "dict_size": dict_size, "ratio": f"{ratio:.6f}" if ratio is not None else "",
            "train_wall_s": f"{train_wall_s:.3f}" if train_wall_s is not None else "",
            "note": note,
        })
        f.flush()
        os.fsync(f.fileno())
    log(f"WROTE {experiment}/{corpus}/L{level}/{variant}: size={dict_size} ratio={ratio} "
        f"wall={train_wall_s} note={note!r}")


def row_lookup(done, experiment, corpus, level, variant):
    return (experiment, corpus, str(level), variant) in done


def artifact_path(corpus, tag, name):
    d = RUNDIR / corpus / str(tag)
    d.mkdir(parents=True, exist_ok=True)
    return d / name


# ---------------------------------------------------------------------
# builders local to this campaign
# ---------------------------------------------------------------------

def build_zstd_default(train_dir, out_path, maxdict):
    cmd = [str(ZSTD), "--train", "-r", str(train_dir), f"--maxdict={maxdict}",
           "-T8", "-o", str(out_path), "-f"]
    ok, code, out, err, wall = rc.timed_run(cmd, timeout=ZSTD_TRAIN_TIMEOUT)
    if not ok or not out_path.exists():
        raise RuntimeError(f"zstd --train failed (rc={code}): {(err or out)[-500:]}")
    return wall


def build_trainer_local(train_dir, out_path, level, time_budget_s, max_budget, stages="all"):
    cmd = ["python3", str(TRAINER), "--train-dir", str(train_dir), "--out", str(out_path),
           "--target-level", str(level), "--max-budget", str(max_budget),
           "--stages", stages, "--time-budget-s", str(time_budget_s)]
    cap = max(4 * 3600, time_budget_s * 6)
    ok, code, out, err, wall = rc.timed_run(cmd, timeout=cap)
    if not ok or not out_path.exists():
        raise RuntimeError(f"trainer.py failed (rc={code}): {(err or out)[-800:]}")
    return wall


def build_rocksdb_finalize(train_dir, content_path, out_path, level):
    """RocksDB use_zstd_dict_trainer=false path: raw-concat content
    (~111KB, same seeded sampling as run_campaign's build_rawconcat) run
    through ZDICT_finalizeDictionary via tools/refine_dict finalize."""
    t0 = time.monotonic()
    rc.build_rawconcat(train_dir, content_path, maxdict=MAXDICT_D)
    cmd = [str(REFINE_DICT), "finalize", str(content_path), str(train_dir), str(out_path), str(level)]
    ok, code, out, err = rc.run(cmd, timeout=REFINE_TIMEOUT)
    wall = time.monotonic() - t0
    if not ok or not out_path.exists():
        raise RuntimeError(f"refine_dict finalize failed (rc={code}): {(err or out)[-500:]}")
    return wall


# ---------------------------------------------------------------------
# Experiment A: Cassandra parity (64KiB, L3)
# ---------------------------------------------------------------------

def run_a(done, corpus):
    level = 3
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / "A"

    if not row_lookup(done, "A", corpus, level, "default64"):
        out_path = artifact_path(corpus, "A", "default64.dict")
        wall = build_zstd_default(train_dir, out_path, MAXDICT_A)
        ratio = rc.eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("A", corpus, level, "default64", out_path.stat().st_size, ratio, wall,
                    f"zstd --train -r train --maxdict={MAXDICT_A} -T8; Cassandra real default (64KiB@L3)")

    if not row_lookup(done, "A", corpus, level, "ours64"):
        out_path = artifact_path(corpus, "A", "ours64.dict")
        wall = build_trainer_local(train_dir, out_path, level, time_budget_s=300, max_budget=MAXDICT_A)
        ratio = rc.eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("A", corpus, level, "ours64", out_path.stat().st_size, ratio, wall,
                    "trainer.py --target-level 3 --max-budget 65536 --time-budget-s 300")


# ---------------------------------------------------------------------
# Experiment B: Scylla RPC parity (L1 eval)
# ---------------------------------------------------------------------

def run_b(done, corpus):
    level = 1
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / "B"

    if not row_lookup(done, "B", corpus, level, "default110_L1"):
        campaign_default = ROOT / "runs" / "campaign_v2" / corpus / "3" / "default.dict"
        if campaign_default.exists():
            out_path = campaign_default
            wall = 0.0
            note = (f"reused MAIN 'default' dict from runs/campaign_v2/{corpus}/3/default.dict "
                     "(zstd --train has no level dependency, so the L3-dir artifact is identical to "
                     "what an L1 build would produce); evaluated at L1")
        else:
            out_path = artifact_path(corpus, "B", "default110_L1.dict")
            wall = build_zstd_default(train_dir, out_path, rc.DEFAULT_MAXDICT)
            note = (f"MAIN default dict NOT found at expected path runs/campaign_v2/{corpus}/3/default.dict; "
                     f"retrained identically (zstd --train --maxdict={rc.DEFAULT_MAXDICT} -T8); evaluated at L1")
        ratio = rc.eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("B", corpus, level, "default110_L1", out_path.stat().st_size, ratio, wall, note)

    if not row_lookup(done, "B", corpus, level, "ours_L1"):
        out_path = artifact_path(corpus, "B", "ours_L1.dict")
        wall = build_trainer_local(train_dir, out_path, level, time_budget_s=600, max_budget=2097152)
        ratio = rc.eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("B", corpus, level, "ours_L1", out_path.stat().st_size, ratio, wall,
                    "trainer.py --target-level 1 --max-budget 2097152 --time-budget-s 600")


# ---------------------------------------------------------------------
# Experiment C: matched-size head-to-head (L3, L19; size S = MAIN/full)
# ---------------------------------------------------------------------

def run_c(done, corpus, level):
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / f"C_{level}"

    if row_lookup(done, "C", corpus, level, "default_matched"):
        return
    full_row = rc.get_full_variant_row(corpus, level)
    if full_row is None:
        log(f"C {corpus}/L{level}: MAIN/full row not found in benchmark_v2.csv, skipping")
        return
    s = int(full_row["dict_size"])
    full_ratio = float(full_row["ratio"])

    out_path = artifact_path(corpus, f"C_{level}", "default_matched.dict")
    wall = build_zstd_default(train_dir, out_path, s)
    ratio = rc.eval_ratio(out_path, heldout_dir, level, tmp_dir)
    append_row("C", corpus, level, "default_matched", out_path.stat().st_size, ratio, wall,
                f"zstd --train -r train --maxdict={s} -T8 (S=MAIN/full dict_size for this corpus/level); "
                f"reference: MAIN/full ratio={full_ratio:.6f} at same nominal size S={s} "
                f"(see results/benchmark_v2.csv; not rerun here)")


# ---------------------------------------------------------------------
# Experiment D: RocksDB fallback (ZDICT_finalizeDictionary, ~111KB)
# ---------------------------------------------------------------------

def run_d(done, corpus, level):
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / f"D_{level}"

    if row_lookup(done, "D", corpus, level, "rocksdb_finalize"):
        return
    content_path = artifact_path(corpus, f"D_{level}", "rocksdb_content.bin")
    out_path = artifact_path(corpus, f"D_{level}", "rocksdb_finalize.dict")
    wall = build_rocksdb_finalize(train_dir, content_path, out_path, level)
    ratio = rc.eval_ratio(out_path, heldout_dir, level, tmp_dir)
    append_row("D", corpus, level, "rocksdb_finalize", out_path.stat().st_size, ratio, wall,
                f"ZDICT_finalizeDictionary(content=~{MAXDICT_D}B raw concat of random train files, seed={SEED}, "
                f"samples=full train dir, compressionLevel={level}); RocksDB use_zstd_dict_trainer=false path")


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def git_commit(corpus):
    ok, code, out, err = rc.run(["git", "add", str(CSV_PATH)], cwd=str(ROOT))
    if not ok:
        log(f"git add failed: {err}")
        return
    ok, code, out, err = rc.run(["git", "diff", "--cached", "--quiet"], cwd=str(ROOT))
    if ok:  # rc==0 -> no staged diff
        log(f"git commit skipped for {corpus}: no changes to results/parity_v2.csv")
        return
    msg = (f"Record parity_v2 campaign results for {corpus}\n\n"
           "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>")
    ok, code, out, err = rc.run(["git", "commit", "-m", msg], cwd=str(ROOT))
    if ok:
        log(f"git commit OK for {corpus}")
    else:
        log(f"git commit FAILED for {corpus}: {err}")


def main():
    ensure_csv_header()
    log("=== dictforge parity_v2 campaign starting ===")
    log(f"zstd={ZSTD} exists={ZSTD.exists()}; trainer={TRAINER} exists={TRAINER.exists()}; "
        f"refine_dict={REFINE_DICT} exists={REFINE_DICT.exists()}")
    if not rc.CSV_PATH.exists():
        log(f"WARNING: {rc.CSV_PATH} (benchmark_v2.csv) not found -- experiment C needs MAIN/full rows "
            f"from it and will skip corpora/levels until it exists")

    failures = []
    for corpus in CORPORA:
        log(f"--- corpus: {corpus} ---")
        blocks = [
            ("A", lambda d: run_a(d, corpus), None),
            ("B", lambda d: run_b(d, corpus), None),
            ("C", lambda d, lvl=3: run_c(d, corpus, lvl), 3),
            ("C", lambda d, lvl=19: run_c(d, corpus, lvl), 19),
            ("D", lambda d, lvl=3: run_d(d, corpus, lvl), 3),
            ("D", lambda d, lvl=19: run_d(d, corpus, lvl), 19),
        ]
        for name, fn, level in blocks:
            try:
                fn(load_done())
            except Exception as exc:                      # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"[:400]
                lvl_display = level if level is not None else (3 if name == "A" else 1)
                log(f"!!! {name} {corpus} L{lvl_display} FAILED, continuing: {msg}")
                failures.append((name, corpus, lvl_display, msg))
                append_row(name, corpus, lvl_display, "BLOCK_FAILED", 0, 0.0, 0.0, msg)
        git_commit(corpus)
        log(f"--- corpus DONE: {corpus} ---")

    if failures:
        log(f"=== campaign finished with {len(failures)} failed blocks ===")
        for f in failures:
            log(f"    FAILED: {f}")
    log("=== dictforge parity_v2 campaign COMPLETE ===")


if __name__ == "__main__":
    main()

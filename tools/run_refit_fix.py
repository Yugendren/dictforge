#!/usr/bin/env python3
"""
run_refit_fix.py -- campaign for KNOWN_ISSUES.md #1 (refit-on-full
contamination). Runs all three proposed fixes (--refit-mode optA/optB/optC
on tools/trainer.py) across all 5 corpora x 2 levels x {tuned, full}
variants and records heldout ratios to results/refit_fix.csv.

Diagnosis (recorded separately, not in this CSV) confirmed the
contamination hypothesis exactly: instrumenting a github_users L3 run
showed the refit candidate's val_ratio jump 10.167009 -> 11.078146 (val is
part of the refit's own training data), while on TRUE heldout the refit
dict scores WORSE than the honest stage-1-only dict (9.944199 vs
10.202290, -2.53%) -- reproducing results/benchmark_v3.csv's tuned/full
gap for that cell exactly.

Variant naming: <option>_tuned (--stages 1) / <option>_full (--stages all)
for option in {optA, optB, optC}. See tools/trainer.py --refit-mode --help
for what each option does; summary:
  optA -- refit dropped entirely; tuned == full by construction.
  optB -- stage-1 candidates trained on the full train-dir from the start,
          val used for selection only; refit is then a no-op (skipped).
  optC -- refit kept, but train-dir is carved 70/15/15 fit/val/gate and
          refit's accept/reject gate uses the untouched gate split.
The original, unfixed behavior (--refit-mode contaminated, the default)
is NOT rerun here -- its numbers are already in results/benchmark_v3.csv
(variant 'full') and are read-only reference points for the report.

Machine safety: every trainer.py invocation is prefixed with `nice -n 10`
and passed `--threads 5` (this machine has 10 logical CPUs; frozen
benchmark_v2/v3 rows used -T8 and are NOT bit-for-byte reproduced here --
noted as a caveat in the final report). Disk is checked (abort if free
space on / drops below 8 GiB) before every corpus/level block.

Resume-safe: on startup (and before every variant) re-reads
results/refit_fix.csv and skips any (experiment,corpus,level,variant) key
already present, so it can be killed and rerun freely. Writes ONLY to
results/refit_fix.csv and runs_refit_fix/ -- never touches
results/{benchmark_v2,benchmark_v3,parity_v2,measure_v2,corrections_v2}.csv
or runs/.

Run (detached):
    mkdir -p runs_refit_fix
    nohup nice -n 10 python3 tools/run_refit_fix.py \
        > runs_refit_fix/campaign.log 2>&1 &
    disown
"""
import csv
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZSTD = ROOT / "third_party" / "zstd" / "programs" / "zstd"
TRAINER = ROOT / "tools" / "trainer.py"
CSV_PATH = ROOT / "results" / "refit_fix.csv"
RUNDIR = ROOT / "runs_refit_fix" / "campaign"

CORPORA = ["github_users", "gharchive", "weblogs", "apijson", "csvrows"]
LEVELS = [3, 19]
TIME_BUDGET_S = {3: 600, 19: 1800}
OPTIONS = ["optA", "optB", "optC"]
THREADS = 5  # machine safety: max 5 cores for our new work
NICE_PREFIX = ["nice", "-n", "10"]
FIELDS = ["experiment", "corpus", "level", "variant", "dict_size", "ratio", "train_wall_s", "note"]

TRAIN_TIMEOUT_HARD_CAP = 6000
BATCH_TIMEOUT = 3600
MIN_FREE_GB = 8.0


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------
# CSV bookkeeping
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


# ---------------------------------------------------------------------
# subprocess / measurement helpers
# ---------------------------------------------------------------------

def run(cmd, timeout=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        return False, None, "", f"TIMEOUT after {timeout}s: {e}"


def timed_run(cmd, timeout=None):
    t0 = time.monotonic()
    ok, rc, out, err = run(cmd, timeout=timeout)
    return ok, rc, out, err, time.monotonic() - t0


def list_files(d):
    return sorted(p for p in os.listdir(d) if os.path.isfile(os.path.join(d, p)))


def dir_raw_bytes(d, names=None):
    if names is None:
        names = list_files(d)
    return sum(os.path.getsize(os.path.join(d, n)) for n in names)


def batch_compress_sizes(dict_path, src_dir, level, tmp_dir):
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    cmd = [str(ZSTD), "-q", "-f", f"-{level}", "-r", str(src_dir), "--output-dir-flat", str(tmp_dir)]
    if dict_path is not None:
        cmd = cmd[:1] + ["-D", str(dict_path)] + cmd[1:]
    ok, rc, out, err = run(cmd, timeout=BATCH_TIMEOUT)
    if not ok:
        raise RuntimeError(f"batch compress failed (rc={rc}): {(err or out)[-500:]}")
    sizes = {}
    for name in os.listdir(tmp_dir):
        if name.endswith(".zst"):
            sizes[name[:-4]] = os.path.getsize(os.path.join(tmp_dir, name))
    return sizes


def eval_ratio(dict_path, src_dir, level, tmp_dir):
    names = list_files(src_dir)
    raw = dir_raw_bytes(src_dir, names)
    sizes = batch_compress_sizes(dict_path, src_dir, level, tmp_dir)
    comp = sum(sizes.values())
    if len(sizes) != len(names):
        log(f"WARNING: eval_ratio size mismatch: {len(names)} inputs, {len(sizes)} outputs (src={src_dir})")
    return raw / comp if comp else 0.0


def dict_path_for(corpus, level, variant):
    d = RUNDIR / corpus / str(level)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{variant}.dict"


def build_trainer(train_dir, out_path, level, stages, time_budget_s, refit_mode):
    cmd = NICE_PREFIX + ["python3", str(TRAINER), "--train-dir", str(train_dir), "--out", str(out_path),
                          "--target-level", str(level), "--stages", stages,
                          "--time-budget-s", str(time_budget_s),
                          "--threads", str(THREADS), "--refit-mode", refit_mode]
    cap = max(4 * 3600, time_budget_s * 6)
    ok, rc, out, err, wall = timed_run(cmd, timeout=cap)
    if not ok or not out_path.exists():
        raise RuntimeError(f"trainer.py ({stages}, {refit_mode}) failed (rc={rc}): {(err or out)[-800:]}")
    return wall


# ---------------------------------------------------------------------
# resource guard
# ---------------------------------------------------------------------

def check_disk(label):
    free_gb = shutil.disk_usage("/").free / 1e9
    log(f"disk check ({label}): {free_gb:.1f} GiB free on /")
    if free_gb < MIN_FREE_GB:
        raise SystemExit(
            f"ABORT: only {free_gb:.1f} GiB free on / (< {MIN_FREE_GB} GiB minimum) "
            f"before {label}; stopping."
        )


# ---------------------------------------------------------------------
# per corpus/level/option block
# ---------------------------------------------------------------------

def run_block(done, corpus, level, option):
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / str(level) / option
    budget = TIME_BUDGET_S[level]

    tuned_variant = f"{option}_tuned"
    if not row_lookup(done, "REFIT_FIX", corpus, level, tuned_variant):
        out_path = dict_path_for(corpus, level, tuned_variant)
        wall = build_trainer(train_dir, out_path, level, "1", budget, option)
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("REFIT_FIX", corpus, level, tuned_variant, out_path.stat().st_size, ratio, wall,
                    f"trainer.py --stages 1 --refit-mode {option} --threads {THREADS} "
                    f"--time-budget-s {budget}")

    full_variant = f"{option}_full"
    if not row_lookup(done, "REFIT_FIX", corpus, level, full_variant):
        out_path = dict_path_for(corpus, level, full_variant)
        wall = build_trainer(train_dir, out_path, level, "all", budget, option)
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("REFIT_FIX", corpus, level, full_variant, out_path.stat().st_size, ratio, wall,
                    f"trainer.py --stages all --refit-mode {option} --threads {THREADS} "
                    f"--time-budget-s {budget}")


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def git_commit(corpus):
    ok, rc, out, err = run(["git", "add", str(CSV_PATH)])
    if not ok:
        log(f"git add failed: {err}")
        return
    ok, rc, out, err = run(["git", "diff", "--cached", "--quiet"])
    if ok:  # rc==0 means no staged diff
        log(f"git commit skipped for {corpus}: no changes to {CSV_PATH.name}")
        return
    msg = (
        f"Record refit-fix campaign results for {corpus}\n\n"
        f"Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    )
    ok, rc, out, err = run(["git", "commit", "-m", msg])
    if ok:
        log(f"git commit OK for {corpus}")
    else:
        log(f"git commit FAILED for {corpus}: {err}")


def main():
    ensure_csv_header()
    log("=== dictforge refit_fix campaign starting ===")
    log(f"CSV_PATH={CSV_PATH} RUNDIR={RUNDIR} THREADS={THREADS}")
    log(f"zstd={ZSTD} exists={ZSTD.exists()}; trainer={TRAINER} exists={TRAINER.exists()}")
    check_disk("startup")

    failures = []
    for corpus in CORPORA:
        check_disk(f"corpus={corpus}")
        log(f"--- corpus: {corpus} ---")
        for level in LEVELS:
            check_disk(f"corpus={corpus} level={level}")
            for option in OPTIONS:
                t0 = time.monotonic()
                try:
                    run_block(load_done(), corpus, level, option)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{type(exc).__name__}: {exc}"[:400]
                    log(f"!!! REFIT_FIX {corpus} L{level} {option} FAILED, continuing: {msg}")
                    failures.append((corpus, level, option, msg))
                    append_row("REFIT_FIX", corpus, level, f"{option}_BLOCK_FAILED", 0, 0.0, 0.0, msg)
                log(f"    {corpus} L{level} {option} done in {time.monotonic() - t0:.1f}s")
        git_commit(corpus)
        log(f"--- corpus DONE: {corpus} ---")

    if failures:
        log(f"=== campaign finished with {len(failures)} failed blocks ===")
        for f in failures:
            log(f"    FAILED: {f}")
    log("=== dictforge refit_fix campaign COMPLETE ===")


if __name__ == "__main__":
    main()

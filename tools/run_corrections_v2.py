#!/usr/bin/env python3
"""
run_corrections_v2.py -- corrections to two measurement errors an
independent audit found in the benchmark_v2 evidence base. Both errors
share one root cause: the affected experiments swept `zstd --train-cover`
(COVER), which is NOT the deployed default. The deployed default is
`zstd --train` (fastCOVER). This script redoes both on the default path.

ERROR 1 -- E1 (equal-compute baseline). tools/run_campaign.py's run_e1()
trained candidates with `--train-cover`, inflating our margin over the
"best effort at equal compute" baseline. This script re-sweeps E1 with
`--train` (fastCOVER) instead, reusing the same equal-compute-budget /
fit-val-split methodology (mirrors tools/trainer.py's 85/15,
sorted-then-seeded-shuffle split, seed=1729): train on the fit split,
accumulate wall time against the MAIN 'full' row's train_wall_s for that
corpus+level (read-only from results/benchmark_v2.csv), select the
best-on-val candidate, then report its heldout ratio. Ladder is denser
than the original (adds rungs between 1048576 and 2097152; the audit
found the optimum can sit off a power-of-two rung, e.g. ~1.3-1.5MiB on
github_users). Variants: default_sweep_cand_<size>, default_sweep_winner.

ERROR 2 -- Sec 3.1's motivating "cliff" table reused those same
--train-cover E1 candidate rows. This re-derives it on the default path
directly: for each corpus at level 3, train `--train` on the FULL train
dir at every ladder size (-T8, no fit/val split -- this table is about
requested-vs-emitted dict size and heldout ratio, not candidate
selection) and evaluate on heldout. Requested size is embedded in the
variant name; emitted size is dict_size; ratio is the heldout ratio.
Each corpus's ladder is swept TWICE (run1/run2) to check determinism.
Variants: default_cliff_<size>_run<1|2>.

Resume-safe: skips any (experiment,corpus,level,variant) key already
present in results/corrections_v2.csv. Writes ONLY to that file --
never touches results/benchmark_v2.csv, results/parity_v2.csv,
results/measure_v2.csv, or anything under runs/. All scratch dict/eval
artifacts live under a throwaway temp dir (default: fresh
tempfile.mkdtemp(); override with --scratch-dir).

Every zstd training invocation is prefixed with `nice -n 10` since this
was run alongside a live results/measure_v2.csv campaign
(tools/run_measure_v2.py); we deliberately did NOT run corpora/levels
concurrently with each other, so overlap with that campaign is limited
to whatever CPU is briefly shared during a single `nice`d zstd call.

Run: python3 tools/run_corrections_v2.py 2>&1 | tee -a /tmp/corrections_v2.log
"""
import argparse
import csv
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZSTD = ROOT / "third_party" / "zstd" / "programs" / "zstd"
BENCH_CSV = ROOT / "results" / "benchmark_v2.csv"   # read-only
CORR_CSV = ROOT / "results" / "corrections_v2.csv"  # write target

CORPORA = ["github_users", "gharchive", "weblogs", "apijson", "csvrows"]
LEVELS = [3, 19]
SEED = 1729
LADDER = [16384, 65536, 112640, 262144, 524288, 786432, 1048576,
          1310720, 1572864, 1835008, 2097152]
FIELDS = ["experiment", "corpus", "level", "variant", "dict_size", "ratio", "train_wall_s", "note"]

TRAIN_TIMEOUT_HARD_CAP = 6000
BATCH_TIMEOUT = 3600
NICE_PREFIX = ["nice", "-n", "10"]
MIN_FREE_GB = 8.0


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------
# CSV bookkeeping (corrections_v2.csv only)
# ---------------------------------------------------------------------

def load_done():
    done = set()
    if CORR_CSV.exists():
        with open(CORR_CSV, newline="") as f:
            for row in csv.DictReader(f):
                done.add((row["experiment"], row["corpus"], str(row["level"]), row["variant"]))
    return done


def ensure_csv_header():
    CORR_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not CORR_CSV.exists():
        with open(CORR_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def append_row(experiment, corpus, level, variant, dict_size, ratio, train_wall_s, note):
    with open(CORR_CSV, "a", newline="") as f:
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


def get_full_variant_row(corpus, level):
    """Read the MAIN 'full' row for corpus/level from benchmark_v2.csv (read-only)."""
    with open(BENCH_CSV, newline="") as f:
        for row in csv.DictReader(f):
            if (row["experiment"], row["corpus"], str(row["level"]), row["variant"]) == \
               ("MAIN", corpus, str(level), "full"):
                return row
    return None


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


def compute_split(train_dir):
    """Identical algorithm to tools/trainer.py's internal fit/val split."""
    names = list_files(train_dir)
    rng = random.Random(SEED)
    rng.shuffle(names)
    n_fit = round(len(names) * 0.85)
    return names[:n_fit], names[n_fit:]


def materialize_split(train_dir, split_dir):
    fit_dir = split_dir / "fit"
    val_dir = split_dir / "val"
    if fit_dir.exists() and val_dir.exists():
        return fit_dir, val_dir
    fit_names, val_names = compute_split(train_dir)
    fit_dir.mkdir(parents=True, exist_ok=True)
    val_dir.mkdir(parents=True, exist_ok=True)
    for n in fit_names:
        link = fit_dir / n
        if not link.exists():
            os.symlink(train_dir / n, link)
    for n in val_names:
        link = val_dir / n
        if not link.exists():
            os.symlink(train_dir / n, link)
    return fit_dir, val_dir


def train_default(train_src_dir, size, out_path):
    """zstd --train (fastCOVER, the deployed default), -T8, as specified
    by the audit's correction spec."""
    cmd = NICE_PREFIX + [str(ZSTD), "--train", "-r", str(train_src_dir),
                          f"--maxdict={size}", "-T8", "-o", str(out_path), "-f"]
    return timed_run(cmd, timeout=TRAIN_TIMEOUT_HARD_CAP)


# ---------------------------------------------------------------------
# resource guards
# ---------------------------------------------------------------------

def measure_running():
    ok, rc, out, err = run(["pgrep", "-f", "measure"])
    pids = [p for p in out.split() if p.strip()]
    return bool(pids), pids


def check_disk():
    st = shutil.disk_usage("/")
    free_gb = st.free / (1024 ** 3)
    if free_gb < MIN_FREE_GB:
        log(f"ABORT: only {free_gb:.1f}GiB free on / (< {MIN_FREE_GB}GiB threshold)")
        sys.exit(1)
    return free_gb


def guard(tag):
    running, pids = measure_running()
    free_gb = check_disk()
    log(f"guard[{tag}]: measure_running={running} pids={pids} disk_free={free_gb:.1f}GiB")


# ---------------------------------------------------------------------
# ERROR 1 fix: E1 equal-compute baseline, default trainer
# ---------------------------------------------------------------------

def run_e1_fix(done, corpus, level, scratch):
    winner_variant = "default_sweep_winner"
    if row_lookup(done, "E1_FIX", corpus, level, winner_variant):
        return

    full_row = get_full_variant_row(corpus, level)
    if full_row is None:
        log(f"E1_FIX {corpus}/L{level}: MAIN 'full' row not found in benchmark_v2.csv, skipping")
        return
    budget = float(full_row["train_wall_s"])

    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    split_dir = scratch / corpus / "_split"
    fit_dir, val_dir = materialize_split(train_dir, split_dir)
    tmp_dir = scratch / "_tmp_eval" / corpus / str(level) / "e1fix"

    accumulated = 0.0
    best = None  # (val_ratio, path, size, wall)
    for size in LADDER:
        variant = f"default_sweep_cand_{size}"
        cache_path = scratch / corpus / str(level) / f"e1fix_{variant}.dict"
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        if row_lookup(done, "E1_FIX", corpus, level, variant):
            with open(CORR_CSV, newline="") as f:
                for row in csv.DictReader(f):
                    if (row["experiment"], row["corpus"], str(row["level"]), row["variant"]) == \
                       ("E1_FIX", corpus, str(level), variant):
                        accumulated += float(row["train_wall_s"])
                        r = float(row["ratio"])
                        if best is None or r > best[0]:
                            best = (r, cache_path, int(row["dict_size"]), float(row["train_wall_s"]))
            if accumulated > budget:
                break
            continue

        guard(f"E1_FIX {corpus}/L{level}/{variant}")
        ok, rc, out, err, wall = train_default(fit_dir, size, cache_path)
        if not ok or not cache_path.exists():
            append_row("E1_FIX", corpus, level, variant, 0, 0.0, wall,
                       f"TRAIN_FAILED rc={rc} err={(err or out)[-200:]!r}")
            accumulated += wall
            if accumulated > budget:
                break
            continue
        emitted_size = cache_path.stat().st_size
        val_ratio = eval_ratio(cache_path, val_dir, level, tmp_dir)
        accumulated += wall
        append_row("E1_FIX", corpus, level, variant, emitted_size, val_ratio, wall,
                    f"candidate; trainer=default(fastCOVER, --train); equal-compute budget={budget:.1f}s; "
                    f"accumulated={accumulated:.1f}s; evaluated on train-internal val split (seed={SEED})")
        if best is None or val_ratio > best[0]:
            best = (val_ratio, cache_path, emitted_size, wall)
        if accumulated > budget:
            break

    if best is None:
        log(f"E1_FIX {corpus}/L{level}: no candidates ran (unexpected), skipping winner row")
        return
    val_ratio, winner_path, winner_size, winner_wall = best
    heldout_ratio = eval_ratio(winner_path, heldout_dir, level, tmp_dir)
    append_row("E1_FIX", corpus, level, winner_variant, winner_size, heldout_ratio, winner_wall,
               f"winner=default_{winner_size}b (by val_ratio={val_ratio:.6f}); "
               f"trainer=default(fastCOVER, --train); equal-compute budget={budget:.1f}s "
               f"(=MAIN full train_wall_s); heldout ratio (no refit-on-full)")


# ---------------------------------------------------------------------
# ERROR 2 fix: Sec 3.1 cliff table, default trainer path, determinism x2
# ---------------------------------------------------------------------

def run_cliff_fix(done, corpus, run_idx, scratch):
    level = 3
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = scratch / "_tmp_eval" / corpus / "cliff" / f"run{run_idx}"

    for size in LADDER:
        variant = f"default_cliff_{size}_run{run_idx}"
        if row_lookup(done, "CLIFF_FIX", corpus, level, variant):
            continue
        out_path = scratch / corpus / "cliff" / f"run{run_idx}_{size}.dict"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        guard(f"CLIFF_FIX {corpus}/{variant}")
        ok, rc, out, err, wall = train_default(train_dir, size, out_path)
        if not ok or not out_path.exists():
            append_row("CLIFF_FIX", corpus, level, variant, 0, 0.0, wall,
                       f"TRAIN_FAILED requested={size} rc={rc} err={(err or out)[-200:]!r}")
            continue
        emitted_size = out_path.stat().st_size
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("CLIFF_FIX", corpus, level, variant, emitted_size, ratio, wall,
                    f"requested={size}; emitted={emitted_size}; trainer=default(fastCOVER, --train); "
                    f"heldout ratio; determinism_run={run_idx}/2; full train dir (no fit/val split)")


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch-dir", default=None,
                     help="scratch dir for temp dicts/eval outputs (default: fresh tempfile.mkdtemp())")
    ap.add_argument("--only", choices=["e1", "cliff", "both"], default="both")
    ap.add_argument("--corpora", nargs="+", default=CORPORA)
    args = ap.parse_args()

    ensure_csv_header()
    scratch = Path(args.scratch_dir) if args.scratch_dir else Path(tempfile.mkdtemp(prefix="dictforge_corr_"))
    scratch.mkdir(parents=True, exist_ok=True)
    log(f"scratch dir: {scratch}")
    log(f"zstd={ZSTD} exists={ZSTD.exists()}")

    if args.only in ("e1", "both"):
        for corpus in args.corpora:
            for level in LEVELS:
                try:
                    run_e1_fix(load_done(), corpus, level, scratch)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{type(exc).__name__}: {exc}"[:400]
                    log(f"!!! E1_FIX {corpus} L{level} FAILED: {msg}")
                    append_row("E1_FIX", corpus, level, "BLOCK_FAILED", 0, 0.0, 0.0, msg)

    if args.only in ("cliff", "both"):
        for corpus in args.corpora:
            for run_idx in (1, 2):
                try:
                    run_cliff_fix(load_done(), corpus, run_idx, scratch)
                except Exception as exc:  # noqa: BLE001
                    msg = f"{type(exc).__name__}: {exc}"[:400]
                    log(f"!!! CLIFF_FIX {corpus} run{run_idx} FAILED: {msg}")
                    append_row("CLIFF_FIX", corpus, 3, f"BLOCK_FAILED_run{run_idx}", 0, 0.0, 0.0, msg)

    log("=== corrections_v2 run COMPLETE ===")


if __name__ == "__main__":
    main()

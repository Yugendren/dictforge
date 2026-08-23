#!/usr/bin/env python3
"""
run_campaign.py -- resume-safe runner for the dictforge benchmark_v2 campaign.

Writes rows to results/benchmark_v2.csv:
    experiment,corpus,level,variant,dict_size,ratio,train_wall_s,note

Safe to kill and rerun: on startup it reads the CSV and skips any
(experiment,corpus,level,variant) key already present. Intended to be
launched detached (nohup ... & disown) and polled via its log file.

Experiments: MAIN, E1 (equal-compute baseline), E2 (klauspost/compress
builddict comparison), E4 (peak-memory measurements). E3 (brotli-matched)
is deliberately NOT implemented here -- the brotli dictionary generator
has not been rebuilt; brotli rows are intentionally absent from this CSV.

Run: python3 tools/run_campaign.py 2>&1 | tee -a runs/campaign_v2.log
"""
import csv
import os
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZSTD = ROOT / "third_party" / "zstd" / "programs" / "zstd"
TRAINER = ROOT / "tools" / "trainer.py"
BUILDDICT = ROOT / "third_party" / "klauspost_builddict" / "builddict"
CSV_PATH = ROOT / "results" / "benchmark_v2.csv"
RUNDIR = ROOT / "runs" / "campaign_v2"

CORPORA = ["github_users", "gharchive", "weblogs", "apijson", "csvrows"]
LEVELS = [3, 19]
TIME_BUDGET_S = {3: 600, 19: 1800}
DEFAULT_MAXDICT = 112640
E1_LADDER = [16384, 65536, 112640, 262144, 524288, 1048576, 2097152]
E2_ZLEVEL = {3: 1, 19: 4}  # klauspost only exposes speed 0-4, not zstd 1-22; approximate mapping, noted in CSV
E4_MEM_FULL_SCOPE = {("github_users", 3), ("gharchive", 3)}
SEED = 1729
FIELDS = ["experiment", "corpus", "level", "variant", "dict_size", "ratio", "train_wall_s", "note"]

BATCH_TIMEOUT = 3600
TRAIN_TIMEOUT_HARD_CAP = 6000  # belt-and-braces vs a runaway subprocess; well above any expected budget


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


def get_full_variant_row(corpus, level):
    """Read the MAIN 'full' row for corpus/level back from the CSV (must already exist)."""
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if (row["experiment"], row["corpus"], str(row["level"]), row["variant"]) == \
               ("MAIN", corpus, str(level), "full"):
                return row
    return None


# ---------------------------------------------------------------------
# subprocess / measurement helpers
# ---------------------------------------------------------------------

def run(cmd, timeout=None, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
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


def batch_compress_sizes(dict_path, src_dir, level, tmp_dir, time_wrap=False):
    """Batch-compress every file in src_dir at level (optionally -D dict_path).
    Returns (sizes_dict, max_rss_bytes_or_None)."""
    if os.path.exists(tmp_dir):
        import shutil
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    cmd = [str(ZSTD), "-q", "-f", f"-{level}", "-r", str(src_dir), "--output-dir-flat", str(tmp_dir)]
    if dict_path is not None:
        cmd = cmd[:1] + ["-D", str(dict_path)] + cmd[1:]
    max_rss = None
    if time_wrap:
        cmd = ["/usr/bin/time", "-l"] + cmd
    ok, rc, out, err = run(cmd, timeout=BATCH_TIMEOUT)
    if time_wrap:
        max_rss = parse_max_rss(err)
    if not ok:
        raise RuntimeError(f"batch compress failed (rc={rc}): {(err or out)[-500:]}")
    sizes = {}
    for name in os.listdir(tmp_dir):
        if name.endswith(".zst"):
            sizes[name[:-4]] = os.path.getsize(os.path.join(tmp_dir, name))
    return sizes, max_rss


def eval_ratio(dict_path, src_dir, level, tmp_dir):
    names = list_files(src_dir)
    raw = dir_raw_bytes(src_dir, names)
    sizes, _ = batch_compress_sizes(dict_path, src_dir, level, tmp_dir)
    comp = sum(sizes.values())
    if len(sizes) != len(names):
        log(f"WARNING: eval_ratio size mismatch: {len(names)} input files, {len(sizes)} .zst outputs "
            f"(src={src_dir})")
    return raw / comp if comp else 0.0


def parse_max_rss(time_l_stderr):
    """Parse '<n> maximum resident set size' from macOS /usr/bin/time -l output (bytes)."""
    for line in time_l_stderr.splitlines():
        line = line.strip()
        if line.endswith("maximum resident set size"):
            try:
                return int(line.split()[0])
            except (ValueError, IndexError):
                pass
    return None


# ---------------------------------------------------------------------
# fit/val split (identical algorithm to trainer.py's internal split)
# ---------------------------------------------------------------------

def compute_split(train_dir):
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


# ---------------------------------------------------------------------
# variant builders (MAIN)
# ---------------------------------------------------------------------

def dict_path_for(corpus, level, variant, suffix=""):
    d = RUNDIR / corpus / str(level)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{variant}{suffix}.dict"

def build_default(train_dir, out_path):
    cmd = [str(ZSTD), "--train", "-r", str(train_dir), f"--maxdict={DEFAULT_MAXDICT}",
           "-T8", "-o", str(out_path), "-f"]
    ok, rc, out, err, wall = timed_run(cmd, timeout=TRAIN_TIMEOUT_HARD_CAP)
    if not ok or not out_path.exists():
        raise RuntimeError(f"default train failed (rc={rc}): {(err or out)[-500:]}")
    return wall

def build_trainer(train_dir, out_path, level, stages, time_budget_s):
    cmd = ["python3", str(TRAINER), "--train-dir", str(train_dir), "--out", str(out_path),
           "--target-level", str(level), "--stages", stages, "--time-budget-s", str(time_budget_s)]
    # --time-budget-s gates stage ENTRY, not stage duration: one stage-2
    # refinement round on a 48MB corpus at L19 can itself take tens of minutes.
    # (Measured pre-wipe: gharchive L19 --stages all ran ~4980s at budget 1800.)
    cap = max(4 * 3600, time_budget_s * 6)
    ok, rc, out, err, wall = timed_run(cmd, timeout=cap)
    if not ok or not out_path.exists():
        raise RuntimeError(f"trainer.py ({stages}) failed (rc={rc}): {(err or out)[-800:]}")
    return wall

def build_rawconcat(train_dir, out_path, maxdict=DEFAULT_MAXDICT):
    t0 = time.monotonic()
    names = list_files(train_dir)
    rng = random.Random(SEED)
    rng.shuffle(names)
    buf = bytearray()
    for n in names:
        if len(buf) >= maxdict:
            break
        with open(train_dir / n, "rb") as f:
            buf.extend(f.read())
    data = bytes(buf[:maxdict])
    with open(out_path, "wb") as f:
        f.write(data)
    return time.monotonic() - t0


# ---------------------------------------------------------------------
# MAIN experiment
# ---------------------------------------------------------------------

def run_main(done, corpus, level):
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / str(level)
    budget = TIME_BUDGET_S[level]

    # nodict
    if not row_lookup(done, "MAIN", corpus, level, "nodict"):
        ratio = eval_ratio(None, heldout_dir, level, tmp_dir)
        append_row("MAIN", corpus, level, "nodict", 0, ratio, 0.0, "")

    # default
    if not row_lookup(done, "MAIN", corpus, level, "default"):
        out_path = dict_path_for(corpus, level, "default")
        wall = build_default(train_dir, out_path)
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("MAIN", corpus, level, "default", out_path.stat().st_size, ratio, wall,
                    "zstd --train --maxdict=112640 -T8")

    # tuned (stage 1 only)
    if not row_lookup(done, "MAIN", corpus, level, "tuned"):
        out_path = dict_path_for(corpus, level, "tuned")
        wall = build_trainer(train_dir, out_path, level, "1", budget)
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("MAIN", corpus, level, "tuned", out_path.stat().st_size, ratio, wall,
                    f"trainer.py --stages 1 --time-budget-s {budget}")

    # full (all stages)
    if not row_lookup(done, "MAIN", corpus, level, "full"):
        out_path = dict_path_for(corpus, level, "full")
        wall = build_trainer(train_dir, out_path, level, "all", budget)
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("MAIN", corpus, level, "full", out_path.stat().st_size, ratio, wall,
                    f"trainer.py --stages all --time-budget-s {budget}")

    # rawconcat
    if not row_lookup(done, "MAIN", corpus, level, "rawconcat"):
        out_path = dict_path_for(corpus, level, "rawconcat")
        wall = build_rawconcat(train_dir, out_path)
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("MAIN", corpus, level, "rawconcat", out_path.stat().st_size, ratio, wall,
                    f"random files concat to {DEFAULT_MAXDICT}B, raw (unfinalized) dict, seed={SEED}")


# ---------------------------------------------------------------------
# E1: equal-compute baseline
# ---------------------------------------------------------------------

def run_e1(done, corpus, level):
    winner_variant = "winner"
    if row_lookup(done, "E1", corpus, level, winner_variant):
        return  # whole block already complete (winner written last)

    full_row = get_full_variant_row(corpus, level)
    if full_row is None:
        log(f"E1 {corpus}/L{level}: MAIN 'full' row not found yet, skipping E1 for now")
        return
    budget = float(full_row["train_wall_s"])

    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    split_dir = RUNDIR / corpus / "_split"
    fit_dir, val_dir = materialize_split(train_dir, split_dir)
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / str(level) / "e1"

    accumulated = 0.0
    best = None  # (val_ratio, out_path, size, wall)
    for size in E1_LADDER:
        variant = f"cover_{size}"
        cache_path = dict_path_for(corpus, level, f"e1_{variant}")
        if row_lookup(done, "E1", corpus, level, variant):
            # already recorded in a prior (killed) run -- read it back to keep best-tracking correct
            with open(CSV_PATH, newline="") as f:
                for row in csv.DictReader(f):
                    if (row["experiment"], row["corpus"], str(row["level"]), row["variant"]) == \
                       ("E1", corpus, str(level), variant):
                        accumulated += float(row["train_wall_s"])
                        r = float(row["ratio"])
                        if best is None or r > best[0]:
                            best = (r, cache_path, int(row["dict_size"]), float(row["train_wall_s"]))
            if accumulated > budget:
                break
            continue

        cmd = [str(ZSTD), "--train-cover", "-r", str(fit_dir), f"--maxdict={size}",
               "-T8", "-o", str(cache_path), "-f"]
        ok, rc, out, err, wall = timed_run(cmd, timeout=TRAIN_TIMEOUT_HARD_CAP)
        if not ok or not cache_path.exists():
            append_row("E1", corpus, level, variant, 0, 0.0, wall,
                       f"TRAIN_FAILED rc={rc} err={(err or out)[-200:]!r}")
            accumulated += wall
            if accumulated > budget:
                break
            continue
        emitted_size = cache_path.stat().st_size
        val_ratio = eval_ratio(cache_path, val_dir, level, tmp_dir)
        accumulated += wall
        append_row("E1", corpus, level, variant, emitted_size, val_ratio, wall,
                    f"candidate; equal-compute budget={budget:.1f}s; "
                    f"accumulated={accumulated:.1f}s; evaluated on train-internal val split")
        if best is None or val_ratio > best[0]:
            best = (val_ratio, cache_path, emitted_size, wall)
        if accumulated > budget:
            break

    if best is None:
        log(f"E1 {corpus}/L{level}: no candidates ran (unexpected), skipping winner row")
        return
    val_ratio, winner_path, winner_size, winner_wall = best
    heldout_ratio = eval_ratio(winner_path, heldout_dir, level, tmp_dir)
    append_row("E1", corpus, level, winner_variant, winner_size, heldout_ratio, winner_wall,
               f"winner=cover_{winner_size}b (by val_ratio={val_ratio:.6f}); "
               f"equal-compute budget={budget:.1f}s (=MAIN full train_wall_s); "
               f"heldout ratio (no refit-on-full)")


# ---------------------------------------------------------------------
# E2: klauspost/compress builddict comparison
# ---------------------------------------------------------------------

def run_e2(done, corpus, level):
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / str(level) / "e2"

    full_row = get_full_variant_row(corpus, level)
    if full_row is None:
        log(f"E2 {corpus}/L{level}: MAIN 'full' row not found yet, skipping E2 for now")
        return
    full_size = int(full_row["dict_size"])
    sizes = sorted(set([DEFAULT_MAXDICT, full_size]))
    zlevel = E2_ZLEVEL[level]

    for size in sizes:
        source = "110K_default_match" if size == DEFAULT_MAXDICT else "full_variant_winning_size"
        variant = f"klauspost_{size}"
        if row_lookup(done, "E2", corpus, level, variant):
            continue
        out_path = dict_path_for(corpus, level, f"e2_{variant}")
        cmd = [str(BUILDDICT), "-len", str(size), "-zlevel", str(zlevel), "-q",
               "-o", str(out_path), str(train_dir)]
        ok, rc, out, err, wall = timed_run(cmd, timeout=TRAIN_TIMEOUT_HARD_CAP)
        if not ok or not out_path.exists():
            append_row("E2", corpus, level, variant, 0, 0.0, wall,
                       f"BUILD_FAILED rc={rc} err={(err or out)[-200:]!r} size_source={source}")
            continue
        emitted_size = out_path.stat().st_size
        ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
        append_row("E2", corpus, level, variant, emitted_size, ratio, wall,
                    f"klauspost/compress dict/cmd/builddict -zlevel={zlevel} "
                    f"(approx map: L3->1(fastest) L19->4(best); klauspost only exposes speeds 0-4, "
                    f"not zstd's 1-22 CLI scale); size_source={source}; evaluated with our zstd on heldout")


# ---------------------------------------------------------------------
# E4: memory
# ---------------------------------------------------------------------

def run_e4(done, corpus, level):
    train_dir = ROOT / "corpora" / corpus / "train"
    heldout_dir = ROOT / "corpora" / corpus / "heldout"
    tmp_dir = RUNDIR / "_tmp_eval" / corpus / str(level) / "e4"
    RSS_CAVEAT = ("CAVEAT: /usr/bin/time -l on macOS reports only the traced process's own peak RSS; "
                  "it does NOT aggregate memory of forked subprocesses. For mem_trainer_full this "
                  "captures the python3 driver only, excluding the zstd/refine_dict/offset_hist "
                  "child processes it spawns (likely the dominant consumer). mem_default_train and "
                  "mem_batch_* trace a single zstd process directly and are NOT subject to this gap.")

    # mem_default_train: all corpora x both levels
    variant = "mem_default_train"
    if not row_lookup(done, "E4", corpus, level, variant):
        out_path = dict_path_for(corpus, level, "e4_default")
        cmd = ["/usr/bin/time", "-l", str(ZSTD), "--train", "-r", str(train_dir),
               f"--maxdict={DEFAULT_MAXDICT}", "-T8", "-o", str(out_path), "-f"]
        ok, rc, out, err, wall = timed_run(cmd, timeout=TRAIN_TIMEOUT_HARD_CAP)
        max_rss = parse_max_rss(err)
        if not ok or not out_path.exists():
            append_row("E4", corpus, level, variant, 0, 0.0, wall, f"FAILED rc={rc}")
        else:
            ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
            append_row("E4", corpus, level, variant, out_path.stat().st_size, ratio, wall,
                        f"max_rss_bytes={max_rss}")

    if (corpus, level) not in E4_MEM_FULL_SCOPE:
        return

    # mem_trainer_full
    variant = "mem_trainer_full"
    if not row_lookup(done, "E4", corpus, level, variant):
        out_path = dict_path_for(corpus, level, "e4_full")
        budget = TIME_BUDGET_S[level]
        cmd = ["/usr/bin/time", "-l", "python3", str(TRAINER), "--train-dir", str(train_dir),
               "--out", str(out_path), "--target-level", str(level), "--stages", "all",
               "--time-budget-s", str(budget)]
        ok, rc, out, err, wall = timed_run(cmd, timeout=budget + 900)
        max_rss = parse_max_rss(err)
        if not ok or not out_path.exists():
            append_row("E4", corpus, level, variant, 0, 0.0, wall, f"FAILED rc={rc}; {RSS_CAVEAT}")
        else:
            ratio = eval_ratio(out_path, heldout_dir, level, tmp_dir)
            append_row("E4", corpus, level, variant, out_path.stat().st_size, ratio, wall,
                        f"max_rss_bytes={max_rss}; {RSS_CAVEAT}")

    # mem_batch_default / mem_batch_full (reuse MAIN-trained dicts if present, else build fresh)
    for src_variant, mem_variant in [("default", "mem_batch_default"), ("full", "mem_batch_full")]:
        if row_lookup(done, "E4", corpus, level, mem_variant):
            continue
        dpath = dict_path_for(corpus, level, src_variant)
        if not dpath.exists():
            log(f"E4 {corpus}/L{level}/{mem_variant}: MAIN {src_variant} dict not cached, building it now")
            if src_variant == "default":
                build_default(train_dir, dpath)
            else:
                build_trainer(train_dir, dpath, level, "all", TIME_BUDGET_S[level])
        names = list_files(heldout_dir)
        raw = dir_raw_bytes(heldout_dir, names)
        t0 = time.monotonic()
        sizes, max_rss = batch_compress_sizes(dpath, heldout_dir, level, tmp_dir, time_wrap=True)
        wall = time.monotonic() - t0
        comp = sum(sizes.values())
        ratio = raw / comp if comp else 0.0
        append_row("E4", corpus, level, mem_variant, dpath.stat().st_size, ratio, wall,
                    f"single batch-compress of heldout with {src_variant} dict; max_rss_bytes={max_rss}")


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def git_commit(corpus):
    ok, rc, out, err = run(["git", "add", str(CSV_PATH)], cwd=str(ROOT))
    if not ok:
        log(f"git add failed: {err}")
        return
    ok, rc, out, err = run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(ROOT))
    if ok:  # no staged changes (rc==0 means no diff)
        log(f"git commit skipped for {corpus}: no changes to results/benchmark_v2.csv")
        return
    msg = f"Record benchmark_v2 campaign results for {corpus}"
    ok, rc, out, err = run(["git", "commit", "-m", msg], cwd=str(ROOT))
    if ok:
        log(f"git commit OK for {corpus}")
    else:
        log(f"git commit FAILED for {corpus}: {err}")


def main():
    ensure_csv_header()
    log("=== dictforge benchmark_v2 campaign starting ===")
    log(f"zstd={ZSTD} exists={ZSTD.exists()}; trainer={TRAINER} exists={TRAINER.exists()}; "
        f"builddict={BUILDDICT} exists={BUILDDICT.exists()}")
    # A single failing cell must not abort a multi-hour campaign: record the
    # failure as a row (so it is visible, and so resume does not silently skip
    # it) and continue with the next experiment block.
    failures = []
    for corpus in CORPORA:
        log(f"--- corpus: {corpus} ---")
        for name, fn in (("MAIN", run_main), ("E1", run_e1), ("E2", run_e2), ("E4", run_e4)):
            for level in LEVELS:
                try:
                    fn(load_done(), corpus, level)
                except Exception as exc:                      # noqa: BLE001
                    msg = f"{type(exc).__name__}: {exc}"[:400]
                    log(f"!!! {name} {corpus} L{level} FAILED, continuing: {msg}")
                    failures.append((name, corpus, level, msg))
                    append_row(name, corpus, level, "BLOCK_FAILED", 0, 0.0, 0.0, msg)
        git_commit(corpus)
        log(f"--- corpus DONE: {corpus} ---")
    if failures:
        log(f"=== campaign finished with {len(failures)} failed blocks ===")
        for f in failures:
            log(f"    FAILED: {f}")
    log("=== dictforge benchmark_v2 campaign COMPLETE ===")


if __name__ == "__main__":
    main()

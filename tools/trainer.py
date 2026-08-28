#!/usr/bin/env python3
"""
trainer.py -- dictforge's zstd dictionary trainer (was trainer_v1.py).

Pipeline:
  0. Split train-dir 85/15 into fit/val (sorted names, seeded shuffle,
     symlinked into a scratch workdir -- no copying).
  1. Stage 1 (size-ladder sweep): try a fixed ladder of --maxdict sizes,
     fastcover first (all sizes) then cover (all sizes, skipped once
     under 15% of the time budget remains), plus an unconditional
     "floor" probe at a small fastcover size. Track the best-on-val
     candidate as we go.
  2. Stage 2 (target_level >= 16 only): iteratively evict poorly-
     referenced 64-byte buckets (measured via tools/refine_dict
     coverage) and refill the freed space with excerpts from the
     worst-compressing fit files, accepting a round only if it shrinks
     the fit-set's batch-compressed size.
  3. Stage 3 (target_level >= 16 only): reseed the dictionary's repeat-
     offset codes from tools/offset_hist's top offsets, keeping the
     patch only if it improves the held-out val ratio.
  4. Refit-on-full: if the final dictionary is still the raw stage 1
     winner (stages 2/3 never improved on it, or never ran), retrain
     that same (trainer, size) pair on the *full* train-dir (fit+val)
     and keep it if it's at least as good on val as the stage 1 result.

Writes the final dictionary to --out and a JSON sidecar (<out>.meta.json)
recording every decision made along the way.

CLI:
    trainer.py --train-dir DIR --out DICT --target-level N
               [--max-budget BYTES] [--time-budget-s SECONDS]
               [--seed SEED] [--stages all|1] [--zstd-bin PATH]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from patch_repcodes import patch_repcodes, find_repcode_field  # noqa: E402

DEFAULT_ZSTD_BIN = HERE.parent / "third_party" / "zstd" / "programs" / "zstd"
REFINE_DICT_BIN = HERE / "refine_dict"
OFFSET_HIST_BIN = HERE / "offset_hist"

# Fine-grained in the large-size region (~1.25x spacing from 196608 up)
# because a finer equal-compute sweep found the true optima sitting
# *between* our old powers-of-two rungs -- at 1310720 and 1572864 bytes,
# sizes the old ladder [.., 1048576, 2097152] could never land on. That
# blind spot cost us 2.53% on github_users at L3 (10.202 achievable vs
# 9.944 with the coarse ladder) plus smaller losses elsewhere. Keep the
# small sizes as-is; only the >=196608 region needed the finer spacing.
SIZE_LADDER = [2048, 4096, 8192, 16384, 32768, 65536, 112640, 196608,
               262144, 393216, 524288, 655360, 786432, 1048576, 1310720,
               1572864, 1835008, 2097152]
FLOOR_SIZE_CAP = 112640
STAGE2_BUCKET = 64
STAGE2_MAX_ROUNDS = 4
STAGE2_REFILL_SEGMENT = 1024
STAGE3_TOP_N = 3
TRAIN_THREADS = 8


def log(msg):
    print(f"[trainer] {msg}", flush=True)


# ---------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------

def list_files(d):
    return sorted(p for p in os.listdir(d) if os.path.isfile(os.path.join(d, p)))


def dir_raw_bytes(d):
    return sum(os.path.getsize(os.path.join(d, p)) for p in list_files(d))


def run(cmd, timeout=None):
    """Run a subprocess, return (ok, returncode_or_None, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return False, None, "", "timeout"


def train_dict(zstd_bin, trainer, train_src_dir, size, out_path, timeout=None):
    """trainer in {'fastcover', 'cover'}. Returns (ok, stderr_tail)."""
    if trainer == "fastcover":
        train_flag = "--train"
    elif trainer == "cover":
        train_flag = "--train-cover"
    else:
        raise ValueError(trainer)
    cmd = [str(zstd_bin), train_flag, "-f", "-r", str(train_src_dir),
           f"--maxdict={size}", f"-T{TRAIN_THREADS}", "-o", str(out_path)]
    ok, rc, out, err = run(cmd, timeout=timeout)
    if not ok or not os.path.exists(out_path):
        return False, (err or out)[-500:]
    return True, ""


def batch_compress(zstd_bin, dict_path, src_dir, level, tmp_dir):
    """Batch-compress every file in src_dir with dict_path at level.
    Returns dict: {original_filename: compressed_bytes}."""
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    cmd = [str(zstd_bin), "-q", "-f", f"-{level}", "-D", str(dict_path),
           "-r", str(src_dir), "--output-dir-flat", str(tmp_dir)]
    ok, rc, out, err = run(cmd)
    if not ok:
        raise RuntimeError(f"batch compress failed: {err[-500:]}")
    sizes = {}
    for name in os.listdir(tmp_dir):
        if name.endswith(".zst"):
            orig = name[:-4]
            sizes[orig] = os.path.getsize(os.path.join(tmp_dir, name))
    return sizes


def batch_ratio(zstd_bin, dict_path, src_dir, level, tmp_dir, raw_bytes):
    sizes = batch_compress(zstd_bin, dict_path, src_dir, level, tmp_dir)
    comp = sum(sizes.values())
    if comp == 0:
        return 0.0
    return raw_bytes / comp


def header_size(dict_bytes):
    """Header length = position of the default-repcode field + 12 bytes;
    the raw content immediately follows in every zstd dict format."""
    return find_repcode_field(dict_bytes) + 12


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="dictforge zstd dictionary trainer")
    ap.add_argument("--train-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target-level", type=int, required=True)
    ap.add_argument("--max-budget", type=int, default=2097152)
    ap.add_argument("--time-budget-s", type=float, default=600)
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument("--stages", choices=["all", "1"], default="all")
    ap.add_argument("--zstd-bin", default=str(DEFAULT_ZSTD_BIN))
    args = ap.parse_args()

    if not REFINE_DICT_BIN.exists() or not OFFSET_HIST_BIN.exists():
        log("WARNING: refine_dict/offset_hist binaries not found next to trainer.py; "
            "stages 2/3 will fail if they're needed. Build them first.")

    t_start = time.monotonic()
    deadline = t_start + args.time_budget_s

    def remaining():
        return deadline - time.monotonic()

    def exhausted():
        return remaining() <= 0

    train_dir = Path(args.train_dir).resolve()
    out_path = Path(args.out).resolve()
    meta_path = Path(str(out_path) + ".meta.json")
    zstd_bin = args.zstd_bin

    workdir = Path(subprocess.run(
        ["mktemp", "-d", "/tmp/dictforge_trainer.XXXXXX"],
        capture_output=True, text=True, check=True).stdout.strip())
    log(f"workdir: {workdir}")

    meta = {
        "args": vars(args) | {"train_dir": str(train_dir), "out": str(out_path)},
        "start_time": time.time(),
    }

    try:
        _run_trainer(args, zstd_bin, train_dir, out_path, meta_path, workdir,
                      meta, remaining, exhausted, t_start)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_trainer(args, zstd_bin, train_dir, out_path, meta_path, workdir,
                  meta, remaining, exhausted, t_start):
    # -------------------------------------------------------------
    # 0. fit/val split
    # -------------------------------------------------------------
    names = list_files(train_dir)
    rng = random.Random(args.seed)
    rng.shuffle(names)
    n_fit = round(len(names) * 0.85)
    fit_names, val_names = names[:n_fit], names[n_fit:]

    fit_dir = workdir / "fit"
    val_dir = workdir / "val"
    fit_dir.mkdir()
    val_dir.mkdir()
    for n in fit_names:
        os.symlink(train_dir / n, fit_dir / n)
    for n in val_names:
        os.symlink(train_dir / n, val_dir / n)

    fit_bytes = dir_raw_bytes(fit_dir)
    val_bytes = dir_raw_bytes(val_dir)
    log(f"split: {len(names)} files -> {len(fit_names)} fit ({fit_bytes} bytes) / "
        f"{len(val_names)} val ({val_bytes} bytes)")

    meta["split"] = {
        "total_files": len(names), "fit_files": len(fit_names),
        "val_files": len(val_names), "fit_bytes": fit_bytes, "val_bytes": val_bytes,
    }

    # -------------------------------------------------------------
    # 1. stage 1: size-ladder sweep
    # -------------------------------------------------------------
    cap = min(args.max_budget, fit_bytes // 4) if fit_bytes > 0 else args.max_budget
    ladder_sizes = [s for s in SIZE_LADDER if s <= cap]
    floor_size = min(args.max_budget, FLOOR_SIZE_CAP)

    candidates = [("fastcover", floor_size, True)]  # (trainer, size, unconditional)
    for s in ladder_sizes:
        candidates.append(("fastcover", s, False))
    for s in ladder_sizes:
        candidates.append(("cover", s, False))

    stage1_leader_path = workdir / "stage1_leader.bin"
    candidate_path = workdir / "stage1_candidate.dict"
    ratio_tmp = workdir / "ratio_tmp"

    best_ratio = -1.0
    best_meta = None
    stage1_records = []
    time_exhausted_flag = False

    for trainer, size, unconditional in candidates:
        rec = {"trainer": trainer, "requested_size": size, "unconditional": unconditional}

        if not unconditional:
            if exhausted():
                rec["skipped"] = "time_exhausted"
                stage1_records.append(rec)
                time_exhausted_flag = True
                continue
            if trainer == "cover" and remaining() < 0.15 * args.time_budget_s:
                rec["skipped"] = "cover_time_budget_below_15pct"
                stage1_records.append(rec)
                continue

        t0 = time.monotonic()
        timeout = None if unconditional else max(5, remaining())
        ok, err_tail = train_dict(zstd_bin, trainer, fit_dir, size, candidate_path,
                                   timeout=timeout)
        rec["train_time_s"] = time.monotonic() - t0

        if not ok:
            rec["failed"] = True
            rec["error_tail"] = err_tail
            stage1_records.append(rec)
            log(f"stage1 [{trainer} {size}]: FAILED ({err_tail[:120]!r})")
            continue

        emitted_size = os.path.getsize(candidate_path)
        degenerate = emitted_size < 0.9 * size
        ratio = batch_ratio(zstd_bin, candidate_path, val_dir, args.target_level,
                             ratio_tmp, meta["split"]["val_bytes"])

        rec.update({"emitted_size": emitted_size, "degenerate": degenerate,
                    "val_ratio": ratio})
        stage1_records.append(rec)
        log(f"stage1 [{trainer} {size}]: emitted={emitted_size} "
            f"degenerate={degenerate} val_ratio={ratio:.6f}")

        if ratio > best_ratio:
            best_ratio = ratio
            best_meta = {"trainer": trainer, "size": size, "emitted_size": emitted_size}
            shutil.copyfile(candidate_path, stage1_leader_path)  # snapshot immediately

    if not stage1_leader_path.exists():
        log("FATAL: no stage1 candidate succeeded; cannot produce a dictionary")
        meta["stage1"] = {"candidates": stage1_records, "time_exhausted": time_exhausted_flag}
        meta_path.write_text(json.dumps(meta, indent=2))
        sys.exit(1)

    with open(stage1_leader_path, "rb") as f:
        current_bytes = f.read()
    current_val_ratio = best_ratio
    current_source = "stage1"

    meta["stage1"] = {
        "candidates": stage1_records,
        "winner": best_meta,
        "winner_val_ratio": current_val_ratio,
        "time_exhausted": time_exhausted_flag,
    }
    log(f"stage1 winner: {best_meta} val_ratio={current_val_ratio:.6f}")

    if args.stages == "1":
        _finish(args, out_path, meta_path, meta, current_bytes, current_val_ratio,
                current_source, t_start)
        return

    # -------------------------------------------------------------
    # 2. stage 2: coverage-driven eviction/refill (target_level >= 16)
    # -------------------------------------------------------------
    if args.target_level >= 16:
        current_bytes, current_source, current_val_ratio = _stage2(
            args, zstd_bin, fit_dir, val_dir, workdir, meta,
            current_bytes, current_val_ratio, remaining, exhausted)
    else:
        meta["stage2"] = {"skipped": "target_level < 16"}

    # -------------------------------------------------------------
    # 3. stage 3: repcode reseed from offset_hist (target_level >= 16)
    # -------------------------------------------------------------
    if args.target_level >= 16:
        current_bytes, current_source, current_val_ratio = _stage3(
            args, zstd_bin, fit_dir, val_dir, workdir, meta,
            current_bytes, current_source, current_val_ratio, remaining, exhausted)
    else:
        meta["stage3"] = {"skipped": "target_level < 16"}

    # -------------------------------------------------------------
    # 4. refit-on-full
    # -------------------------------------------------------------
    if current_source == "stage1":
        current_bytes, current_source, current_val_ratio = _refit_on_full(
            args, zstd_bin, train_dir, val_dir, workdir, meta, best_meta,
            current_bytes, current_val_ratio)
    else:
        meta["refit_on_full"] = {"skipped": f"final source is '{current_source}', not stage1"}

    _finish(args, out_path, meta_path, meta, current_bytes, current_val_ratio,
            current_source, t_start)


def _finish(args, out_path, meta_path, meta, final_bytes, final_val_ratio,
            final_source, t_start):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(final_bytes)
    meta["final"] = {
        "source": final_source,
        "val_ratio": final_val_ratio,
        "dict_size": len(final_bytes),
    }
    meta["wall_time_s"] = time.monotonic() - t_start
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    log(f"DONE: wrote {out_path} ({len(final_bytes)} bytes), source={final_source}, "
        f"val_ratio={final_val_ratio:.6f}, meta={meta_path}")


# ---------------------------------------------------------------------
# stage 2
# ---------------------------------------------------------------------

def _run_coverage(dict_path, content_offset, samples_dir, level, bucket):
    ok, rc, out, err = run([str(REFINE_DICT_BIN), "coverage", str(dict_path),
                             str(content_offset), str(samples_dir), str(level), str(bucket)])
    if not ok:
        raise RuntimeError(f"refine_dict coverage failed: {err[-500:]}")
    content_size = None
    counts = {}
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "DICT_CONTENT_SIZE":
            content_size = int(parts[1])
        elif parts[0] == "BUCKET":
            counts[int(parts[1])] = int(parts[2])
    nb = max(counts) + 1 if counts else 0
    count_list = [counts.get(i, 0) for i in range(nb)]
    return content_size, count_list


def _stage2(args, zstd_bin, fit_dir, val_dir, workdir, meta, current_bytes,
            current_val_ratio, remaining, exhausted):
    rounds = []
    ratio_tmp = workdir / "ratio_tmp"
    round_input_path = workdir / "stage2_input.dict"
    round_content_path = workdir / "stage2_content.bin"
    round_out_path = workdir / "stage2_round.dict"

    with open(round_input_path, "wb") as f:
        f.write(current_bytes)
    prev_total = sum(batch_compress(zstd_bin, round_input_path, fit_dir,
                                     args.target_level, ratio_tmp).values())
    log(f"stage2: baseline fit-set compressed size = {prev_total}")

    for round_idx in range(1, STAGE2_MAX_ROUNDS + 1):
        rec = {"round": round_idx}
        if exhausted():
            rec["skipped"] = "time_exhausted"
            rounds.append(rec)
            break

        hsize = header_size(current_bytes)
        content = current_bytes[hsize:]
        content_size = len(content)

        with open(round_input_path, "wb") as f:
            f.write(current_bytes)
        cov_content_size, counts = _run_coverage(round_input_path, hsize, fit_dir,
                                                   args.target_level, STAGE2_BUCKET)
        if cov_content_size != content_size or not counts:
            rec["skipped"] = "coverage_unavailable"
            rounds.append(rec)
            break

        nonzero = sorted(c for c in counts if c > 0)
        if nonzero:
            idx = min(len(nonzero) - 1, max(0, int(len(nonzero) * 0.1)))
            decile_threshold = nonzero[idx]
        else:
            decile_threshold = 0
        evictable = {i for i, c in enumerate(counts)
                     if c == 0 or (c > 0 and c <= decile_threshold)}

        # contiguous runs of length >= 2 only
        evict_idx = set()
        i, n = 0, len(counts)
        while i < n:
            if i in evictable:
                j = i
                while j < n and j in evictable:
                    j += 1
                if j - i >= 2:
                    evict_idx.update(range(i, j))
                i = j
            else:
                i += 1

        if not evict_idx:
            rec["accepted"] = False
            rec["reason"] = "no_contiguous_evictable_runs"
            rounds.append(rec)
            break

        kept_ranges = []
        i = 0
        while i < n:
            if i in evict_idx:
                i += 1
                continue
            j = i
            while j < n and j not in evict_idx:
                j += 1
            kept_ranges.append((i * STAGE2_BUCKET, min(j * STAGE2_BUCKET, content_size)))
            i = j

        compacted = b"".join(content[a:b] for a, b in kept_ranges)
        freed_bytes = content_size - len(compacted)
        rec["evicted_buckets"] = len(evict_idx)
        rec["freed_bytes"] = freed_bytes

        # rank fit files by current dict's compression ratio (worst first)
        sizes = batch_compress(zstd_bin, round_input_path, fit_dir, args.target_level,
                                ratio_tmp)
        ratios = []
        for name in fit_dir.iterdir():
            fname = name.name
            comp = sizes.get(fname)
            if comp:
                raw = os.path.getsize(name)
                ratios.append((raw / comp, name))
        ratios.sort(key=lambda t: t[0])  # worst (lowest ratio) first

        refill = bytearray()
        for _, path in ratios:
            if len(refill) >= freed_bytes:
                break
            with open(path, "rb") as f:
                chunk = f.read(STAGE2_REFILL_SEGMENT)
            refill.extend(chunk)
        refill = bytes(refill[:freed_bytes])

        new_content = refill + compacted
        with open(round_content_path, "wb") as f:
            f.write(new_content)

        ok, rc, out, err = run([str(REFINE_DICT_BIN), "finalize", str(round_content_path),
                                 str(fit_dir), str(round_out_path), str(args.target_level)])
        if not ok:
            rec["accepted"] = False
            rec["reason"] = f"finalize_failed: {err[-300:]}"
            rounds.append(rec)
            break

        new_total = sum(batch_compress(zstd_bin, round_out_path, fit_dir,
                                        args.target_level, ratio_tmp).values())
        rec["fit_compressed_before"] = prev_total
        rec["fit_compressed_after"] = new_total

        if new_total < prev_total:
            with open(round_out_path, "rb") as f:
                current_bytes = f.read()
            prev_total = new_total
            rec["accepted"] = True
            rounds.append(rec)
            log(f"stage2 round {round_idx}: accepted, "
                f"fit compressed {rec['fit_compressed_before']} -> {new_total}")
        else:
            rec["accepted"] = False
            rec["reason"] = "no_improvement"
            rounds.append(rec)
            log(f"stage2 round {round_idx}: rejected (no improvement)")
            break

    accepted_any = any(r.get("accepted") for r in rounds)
    source = "stage2" if accepted_any else "stage1"

    if accepted_any:
        tmp_final = workdir / "stage2_final.dict"
        with open(tmp_final, "wb") as f:
            f.write(current_bytes)
        current_val_ratio = batch_ratio(zstd_bin, tmp_final, val_dir, args.target_level,
                                         ratio_tmp, meta["split"]["val_bytes"])

    meta["stage2"] = {"rounds": rounds, "accepted_any": accepted_any,
                       "final_val_ratio_if_accepted": current_val_ratio if accepted_any else None}
    return current_bytes, source, current_val_ratio


# ---------------------------------------------------------------------
# stage 3
# ---------------------------------------------------------------------

def _stage3(args, zstd_bin, fit_dir, val_dir, workdir, meta, current_bytes,
            current_source, current_val_ratio, remaining, exhausted):
    ratio_tmp = workdir / "ratio_tmp"
    input_path = workdir / "stage3_input.dict"
    patched_path = workdir / "stage3_patched.dict"

    if exhausted():
        meta["stage3"] = {"skipped": "time_exhausted"}
        return current_bytes, current_source, current_val_ratio

    with open(input_path, "wb") as f:
        f.write(current_bytes)

    ok, rc, out, err = run([str(OFFSET_HIST_BIN), str(input_path), str(fit_dir)])
    if not ok:
        meta["stage3"] = {"skipped": f"offset_hist failed: {err[-300:]}"}
        return current_bytes, current_source, current_val_ratio

    content_size = len(current_bytes) - header_size(current_bytes)
    top_offsets = []
    in_weighted = False
    for line in out.splitlines():
        if line.strip() == "TOP10_WEIGHTED":
            in_weighted = True
            continue
        if line.strip() == "TOP10_ALL":
            break
        if in_weighted and line.startswith("OFFSET"):
            off = int(line.split()[1])
            if 0 < off <= content_size:
                top_offsets.append(off)

    if len(top_offsets) < STAGE3_TOP_N:
        meta["stage3"] = {"skipped": "fewer_than_3_valid_offsets",
                           "candidates_found": top_offsets}
        return current_bytes, current_source, current_val_ratio

    off1, off2, off3 = top_offsets[:STAGE3_TOP_N]

    try:
        patched_bytes = patch_repcodes(current_bytes, (off1, off2, off3))
    except ValueError as e:
        meta["stage3"] = {"skipped": f"patch_repcodes rejected offsets: {e}"}
        return current_bytes, current_source, current_val_ratio

    with open(patched_path, "wb") as f:
        f.write(patched_bytes)

    ratio_before = current_val_ratio if current_val_ratio is not None else batch_ratio(
        zstd_bin, input_path, val_dir, args.target_level, ratio_tmp, meta["split"]["val_bytes"])
    ratio_after = batch_ratio(zstd_bin, patched_path, val_dir, args.target_level,
                               ratio_tmp, meta["split"]["val_bytes"])

    rec = {"offsets": [off1, off2, off3], "val_ratio_before": ratio_before,
           "val_ratio_after": ratio_after}

    if ratio_after > ratio_before:
        rec["accepted"] = True
        meta["stage3"] = rec
        log(f"stage3: accepted, val_ratio {ratio_before:.6f} -> {ratio_after:.6f}")
        return patched_bytes, "stage3", ratio_after
    else:
        rec["accepted"] = False
        meta["stage3"] = rec
        log(f"stage3: rejected (val_ratio {ratio_after:.6f} <= {ratio_before:.6f})")
        return current_bytes, current_source, current_val_ratio


# ---------------------------------------------------------------------
# refit-on-full
# ---------------------------------------------------------------------

def _refit_on_full(args, zstd_bin, train_dir, val_dir, workdir, meta, stage1_winner,
                    current_bytes, current_val_ratio):
    ratio_tmp = workdir / "ratio_tmp"
    refit_path = workdir / "refit_full.dict"
    refit_timeout = max(30, min(180, args.time_budget_s))

    trainer, size = stage1_winner["trainer"], stage1_winner["size"]
    t0 = time.monotonic()
    ok, err_tail = train_dict(zstd_bin, trainer, train_dir, size, refit_path,
                               timeout=refit_timeout)
    elapsed = time.monotonic() - t0

    if not ok:
        meta["refit_on_full"] = {"trainer": trainer, "size": size, "failed": True,
                                  "error_tail": err_tail, "train_time_s": elapsed}
        log(f"refit_on_full: FAILED ({err_tail[:120]!r}), keeping stage1 winner")
        return current_bytes, "stage1", current_val_ratio

    refit_ratio = batch_ratio(zstd_bin, refit_path, val_dir, args.target_level,
                               ratio_tmp, meta["split"]["val_bytes"])
    rec = {"trainer": trainer, "size": size, "train_time_s": elapsed,
           "val_ratio": refit_ratio, "prior_val_ratio": current_val_ratio}

    if refit_ratio >= current_val_ratio:
        rec["accepted"] = True
        meta["refit_on_full"] = rec
        with open(refit_path, "rb") as f:
            refit_bytes = f.read()
        log(f"refit_on_full: accepted, val_ratio {current_val_ratio:.6f} -> {refit_ratio:.6f}")
        return refit_bytes, "refit_full", refit_ratio
    else:
        rec["accepted"] = False
        meta["refit_on_full"] = rec
        log(f"refit_on_full: rejected (val_ratio {refit_ratio:.6f} < {current_val_ratio:.6f}), "
            f"keeping stage1 winner")
        return current_bytes, "stage1", current_val_ratio


if __name__ == "__main__":
    main()

# EXPERIMENTS.md — data dictionary for `results/*.csv`

All five files use aggregate ratios, not per-file averages: wherever a row
reports `ratio`, it is `sum(raw_bytes_of_every_file) / sum(compressed_bytes_of_every_file)`
over a whole directory (heldout or validation split), computed by
`eval_ratio`/`batch_ratio` helpers in the relevant script — never a mean of
per-file ratios. Keep this in mind when comparing against any external
per-file-averaged number.

All five files are resume-safe append logs: every runner script re-reads
its target CSV on startup (and, for the long ones, before every cell) and
skips any `(experiment, corpus, level, variant[, metric])` key already
present, so they can be killed and rerun freely without duplicating rows.
**Do not hand-edit these CSVs** — if a row is wrong, fix the generating
script and either delete the specific stale row(s) or regenerate into a
fresh file, so the resume-skip logic can't paper over a real fix.

Corpora referenced everywhere below: `github_users`, `gharchive`,
`weblogs`, `apijson`, `csvrows` (see `README.md` repo layout / `corpora/fetch_corpora.py`).
Levels: 3 (fast, RocksDB/ScyllaDB-SSTable/Cassandra default) and 19
(archival); level 1 appears only in `parity_v2.csv` experiment B.

---

## `results/benchmark_v2.csv`

**Question.** How does our trainer's own best dictionary compare to the
deployed default, a no-training control, and a raw-concatenation control,
plus two comparison-baseline experiments (equal-compute sweep of the
incumbent, an alternative open-source trainer) and a memory experiment?
**This is the file the paper (`paper/latex/main.tex`, `paper/sections/*.md`)
currently cites for every headline number.**

**Schema:** `experiment,corpus,level,variant,dict_size,ratio,train_wall_s,note`
- `dict_size`: emitted dictionary size in bytes (0 for `nodict`).
- `ratio`: aggregate heldout compression ratio (see note above); `0.000000`
  or blank marks a failed cell (see `BLOCK_FAILED`/`TRAIN_FAILED` notes).
- `train_wall_s`: wall-clock seconds to produce the dictionary (0.000 for
  `nodict`).

**`experiment` values:**
| experiment | meaning |
|---|---|
| `MAIN` | headline comparison: no-dict / deployed-default / our tool / raw-content control |
| `E1` | equal-compute baseline: give the incumbent our wall-clock training budget and let it sweep sizes, selecting by measured validation ratio |
| `E2` | `klauspost/compress`'s `builddict`, the only other tool found that emits standard-format zstd dictionaries and exposes a level flag |
| `E4` | peak-memory measurements (subset of corpora/levels) |

E3 (brotli-matched) is **deliberately absent** — the brotli dictionary
generator baseline was never rebuilt for this experiment; see `measure_v2.csv`
T4 for the brotli/lz4 cross-codec-transfer experiment instead, which is a
different question (transferring *our* dictionaries to other codecs, not
comparing brotli's own trainer).

**`MAIN` variants:**
| variant | meaning |
|---|---|
| `nodict` | batch-compress heldout with no dictionary at all |
| `default` | `zstd --train -r train --maxdict=112640 -T8` (the deployed default: fastCOVER, 110 KiB) |
| `tuned` | `tools/trainer.py --stages 1` — stage 1 (size/family search) only, no refinement/repcode/refit |
| `full` | `tools/trainer.py --stages all` — everything, including refit-on-full (see `KNOWN_ISSUES.md` #1: this variant's L3 numbers are not fully trustworthy where refit was accepted) |
| `rawconcat` | random training files concatenated (seed 1729) to 112640 bytes, used *unfinalized* (no entropy tables/header) — a no-training-heuristic-at-all control |

Time budgets: 600s at L3, 1800s at L19 (`TIME_BUDGET_S` in `tools/run_campaign.py`).

**`E1` variants:** `cover_<size>` for `size` in `[16384, 65536, 112640,
262144, 524288, 1048576, 2097152]` (candidates, trained with
`zstd --train-cover` on the trainer's own 85/15 fit split, evaluated on the
val split, accumulated wall time compared against the corresponding
`MAIN`/`full` row's `train_wall_s` as the equal-compute budget); `winner`
(the best-on-val candidate's dictionary, re-evaluated on heldout).
**Known error, corrected in `corrections_v2.csv`:** this used
`--train-cover` (COVER) instead of the deployed default `--train`
(fastCOVER) — see that file's section below and `KNOWN_ISSUES.md` #3.

**`E2` variants:** `klauspost_<size>` where `size` is either
`DEFAULT_MAXDICT` (112640, "110K_default_match") or the corresponding
`MAIN`/`full` dict_size ("full_variant_winning_size") — see the note
field's `size_source=` tag to tell which. `-zlevel` mapping is an
**approximation**: klauspost only exposes speed tiers 0–4, not zstd's 1–22
CLI scale; `E2_ZLEVEL = {3: 1, 19: 4}` in `tools/run_campaign.py`, flagged
in every E2 row's note.

**`E4` variants:** `mem_default_train`, `mem_trainer_full` (peak RSS of the
*training* process; `mem_trainer_full` only recorded for
`(github_users, 3)` and `(gharchive, 3)` — `E4_MEM_FULL_SCOPE` — and
carries an explicit `RSS_CAVEAT` note: macOS `/usr/bin/time -l` traces only
the wrapped process, not its children, so `mem_trainer_full`'s number
excludes the zstd/`refine_dict`/`offset_hist` subprocesses `trainer.py`
spawns — likely the dominant consumer, so treat this number as an
underestimate), `mem_batch_default`, `mem_batch_full` (peak RSS of a single
batch-compress of heldout with the `MAIN` default/full dict — these two are
NOT subject to the child-process gap, since they wrap a single zstd
process directly).

**Regenerate:**
```bash
python3 tools/run_campaign.py 2>&1 | tee -a runs/campaign_v2.log
```
Resume-safe; intended to run detached (`nohup ... & disown`); commits to
git after each corpus completes. A single failing block is recorded as a
`BLOCK_FAILED` row and does not abort the rest of the campaign.

---

## `results/benchmark_v3.csv`

**Question.** Same as `benchmark_v2.csv`'s `MAIN` block, rerun after fixing
a blind spot in `tools/trainer.py`'s `SIZE_LADDER` (see the comment above
that constant): the old power-of-two ladder `[..., 1048576, 2097152]`
could never land on the true optimum, which a finer equal-compute sweep
found sitting at 1310720/1572864 bytes for some corpora — a gap that cost
2.53% on `github_users` L3 alone. New ladder adds rungs every ~1.25× from
196608 up.

**Schema:** identical to `benchmark_v2.csv`.

**Scope:** `MAIN` experiment only, same 5 variants
(`nodict`/`default`/`tuned`/`full`/`rawconcat`), same 5 corpora, same 2
levels. **E1/E2/E4 were not rerun** — this file has no rows for those
experiments; use `benchmark_v2.csv` (or its `corrections_v2.csv`
correction) if you need them.

**Not yet used by the paper.** `tools/make_tables.py` and
`paper/latex/Makefile` both still default to `benchmark_v2.csv`. See
`KNOWN_ISSUES.md` #5 and `HANDOFF.md` for the reconciliation task.

**Regenerate:**
```bash
mkdir -p runs_v3
nohup nice -n 10 python3 tools/run_campaign_v3.py > runs_v3/campaign_v3.log 2>&1 &
disown
```
Implemented as a thin wrapper: monkey-patches `tools/run_campaign.py`'s
`CSV_PATH` → `results/benchmark_v3.csv` and `RUNDIR` → `runs_v3/campaign_v3`,
then calls its `run_main()` directly — so the fit/val split, dict builders,
and heldout evaluation are byte-for-byte the same code that produced
`benchmark_v2.csv`. Time budgets are deliberately unchanged (600s/1800s)
to keep the two files comparable on that axis; runs strictly serially,
meant to be launched under `nice -n 10`. Aborts if free disk on `/` drops
below 8GB.

---

## `results/corrections_v2.csv`

**Question.** Redo two specific measurement errors an independent audit
found in `benchmark_v2.csv`'s evidence base, both sharing one root cause:
the affected rows swept `zstd --train-cover` (COVER), which is **not** the
deployed default — the deployed default is `zstd --train` (fastCOVER).

**Schema:** identical to `benchmark_v2.csv`. **Never writes to
`benchmark_v2.csv`, `parity_v2.csv`, `measure_v2.csv`, or anything under
`runs/`** — read-only against `benchmark_v2.csv` (for the equal-compute
budget and the `MAIN`/`full` dict size), write-only to this file.

**`experiment` values:**
| experiment | meaning |
|---|---|
| `E1_FIX` | correction to `benchmark_v2.csv`'s E1 (equal-compute baseline): resweep with `--train` (fastCOVER) instead of `--train-cover` |
| `CLIFF_FIX` | correction to paper §3.1's motivating "cliff" table, which had reused the same `--train-cover` E1 rows: re-derive directly on the default path |

**`E1_FIX` variants:** `default_sweep_cand_<size>` for `size` in a denser
ladder than the original E1 (`[16384, 65536, 112640, 262144, 524288,
786432, 1048576, 1310720, 1572864, 1835008, 2097152]` — adds rungs between
1048576 and 2097152 after the audit found the optimum can sit off a
power-of-two rung, e.g. ~1.3–1.5 MiB on `github_users`); `default_sweep_winner`
(best-on-val candidate's dictionary size/note; the row's `ratio` is its
**heldout** ratio, no refit-on-full applied). Same fit/val-split algorithm
as `tools/trainer.py` (85/15, sorted-then-seeded-shuffle, seed 1729),
equal-compute budget = the corresponding `benchmark_v2.csv`
`MAIN`/`full`/`train_wall_s` value.

**`CLIFF_FIX` variants:** `default_cliff_<size>_run<1|2>`, level fixed at
3. Trains `zstd --train` directly on the **full** train dir (no fit/val
split — this table is about requested-vs-emitted dict size and heldout
ratio, not candidate selection) at every ladder size, evaluated on
heldout. Each corpus's ladder is swept **twice** (`run1`/`run2`) to check
determinism — compare the two runs' `dict_size`/`ratio` for a given size
to see whether `zstd --train` is deterministic on this machine/build.

**Caveats recorded in this file's own docstring/notes:**
- Run under `nice -n 10` because it executed alongside a live
  `measure_v2.csv` campaign; not run concurrently corpus-by-corpus with
  itself, so CPU overlap is limited to brief sharing during a single
  `nice`d zstd call.
- Every block checks free disk (`MIN_FREE_GB = 8.0`) and aborts
  (`sys.exit(1)`) if it drops below that, and logs whether a `measure`
  process is currently running (informational only, not a gate).

**Cross-reference (important):** `E1_FIX,github_users,3,default_sweep_winner`
(`10.202290`) exactly matches `benchmark_v3.csv`'s
`MAIN,github_users,3,tuned` row — see `KNOWN_ISSUES.md` #1, the
REFIT-ON-FULL contamination finding, which this file's data was used to
confirm.

**Regenerate:**
```bash
python3 tools/run_corrections_v2.py 2>&1 | tee -a /tmp/corrections_v2.log
# options: --only {e1,cliff,both} --corpora C1 C2 ... --scratch-dir DIR
```

---

## `results/parity_v2.csv`

**Question.** `benchmark_v2.csv`'s headline comparisons use our trainer's
own (usually much larger) chosen size against the 110 KiB default. This
file instead compares at **production-motivated sizes/levels** and at
**matched (equal) sizes**, to separate "how much of the gain is size" from
"how much is method."

**Schema:** identical to `benchmark_v2.csv`.

**`experiment` values:**
| experiment | meaning | level | variants |
|---|---|---|---|
| `A` | Cassandra parity: the code's actual shipped default (64 KiB, L3) | 3 | `default64` (`zstd --train --maxdict=65536`), `ours64` (`trainer.py --target-level 3 --max-budget 65536 --time-budget-s 300`) |
| `B` | ScyllaDB-RPC-path-motivated evaluation setting (110 KiB, L1 — that path is off by default, see `paper/sections/background.md` §2.3) | 1 | `default110_L1` (reuses the `MAIN`/`default` dict from `runs/campaign_v2/<corpus>/3/default.dict` if present — valid because `zstd --train` has no level dependency — else retrains), `ours_L1` (`trainer.py --target-level 1 --max-budget 2097152 --time-budget-s 600`) |
| `C` | matched-size head-to-head at our own winning size S (read from `benchmark_v2.csv` `MAIN`/`full` `dict_size`) | 3, 19 | `default_matched` only — the "ours" side is *not* rerun, it's the existing `MAIN`/`full` row (`benchmark_v2.csv`), referenced in the note field, not duplicated as a row here |
| `D` | RocksDB's opt-in `use_zstd_dict_trainer=false` fallback: `ZDICT_finalizeDictionary` over raw concatenated train samples (~111 KB, seed 1729), no content-selection heuristic at all | 3, 19 | `rocksdb_finalize` |

**Known caveat — three starved `C` cells.** Asked for our winning budget,
the stock trainer itself sometimes emits substantially less than
requested (the budget-degeneracy defect under study, not a clean control —
see `KNOWN_ISSUES.md`/`docs/FINDINGS.md`). Confirmed in this file:
`gharchive` L19 (`dict_size=892712` against a `maxdict=2097152` request),
`apijson` L3 (`dict_size=952604` against `maxdict=1048576`), `apijson` L19
(`dict_size=952604` against `maxdict=1048583`). These three `C` rows are
size-*starved*, not size-*matched*; the paper excludes them from the
survival-percentage summary in §6.2/§`sec:evaluation-parity` and reports
them separately.

**Regenerate:**
```bash
python3 tools/run_parity.py 2>&1 | tee -a runs/parity_v2.log
```
Reuses `tools/run_campaign.py`'s helpers (`eval_ratio`, `timed_run`,
`build_default`, `build_rawconcat`, `get_full_variant_row`) so measurement
methodology matches `benchmark_v2.csv` exactly. Generous subprocess
timeouts (up to 4h) since L19 refinement can take 30–90 minutes/cell.

---

## `results/measure_v2.csv`

**Question.** Supplementary measurements beyond ratio: throughput, memory,
statistical confidence (bootstrap CIs), and cross-codec transfer (do our
zstd-trained dictionaries help brotli/lz4 too?).

**Schema:** `experiment,corpus,level,variant,metric,value,unit,note`
(note the extra `metric`/`unit` columns vs. the other four files — this is
a metric-per-row format, not one-ratio-per-row).

**`experiment` blocks:**

**`T1` — throughput.** Variants `default`/`full` (the `MAIN` dicts).
Metrics: `dict_size` (bytes), `compress_throughput_MBps`,
`decompress_throughput_MBps` (MB/s = 1e6-byte "decimal" MB; median of 3
reps, `nice -n 10`, serial, single-threaded batch compress/decompress of
heldout; decompress reps run against the `.zst` tree left by the last
compress rep, with a one-shot byte-count integrity check against raw
size). `csvrows` L19 has a tighter batch timeout (600s vs the default
3600s) per spec; a timeout/failure records a `SKIPPED` row with the reason
in `note` rather than aborting the block.

**`T2` — peak RSS.** Variants `default`/`full`: `peak_rss_single_file_compress`
(bytes; single-file compression of one heldout file, `/usr/bin/time -l`,
per corpus/level). `train_default`: `peak_rss` (bytes; retraining
`zstd --train` at `-T6`, capped down from the campaign's usual `-T8` for
machine-safety — reported once per corpus, level-independent, since
`zstd --train` has no level dependency). `trainer_py`:
`peak_rss_omitted_note` — a one-time explanatory row (not a measurement):
`trainer.py`'s own peak RSS is **not measured**, because macOS
`/usr/bin/time -l` only reports the traced process's own RSS, excluding
the zstd/`refine_dict`/`offset_hist` child processes `trainer.py` spawns
(almost certainly the actual dominant memory consumer) — reporting the
near-zero Python-interpreter number would be actively misleading, so it's
omitted rather than reported.

**`T3` — paired bootstrap CIs.** Variant `full_vs_default` only. Metrics
`delta_pct_point`, `delta_pct_ci_lo_2.5`, `delta_pct_ci_hi_97.5` (percent).
10,000 paired bootstrap resamples (seed 1729) over heldout files, computed
by `tools/measure_v2_bootstrap.py` (run under `.venv/bin/python3` for
numpy) via a JSON stdin/stdout handoff from `tools/run_measure_v2.py`
(which stays on system Python). `delta_pct = (ratio_full/ratio_default - 1) * 100`,
where each resample's `ratio_x` is the **aggregate** corpus-level ratio
over the resampled file indices (not a per-file mean delta) — same
indices applied to both `default` and `full` arrays (paired). **Caveat
inherited from the paper's discussion section:** heldout files within one
corpus are shuffled draws from a single scrape, not independent draws
from a population, so this CI understates true variance; read it as
"variance under this specific held-out sample," not as a population CI.

**`T4_brotli`/`T4_lz4` — cross-codec transfer.** Do our raw dictionary
*content* (zstd header/entropy tables stripped) help other codecs used as
external/raw dictionaries? Heldout files are concatenated into a single
blob per corpus first (`build_blob`), since neither CLI has a
recursive/batch mode. Variants: `nodict`, `default_derived`, `full_derived`
(content from the `MAIN` `default`/`full` dicts) for both; `T4_lz4` adds
`full_derived_trunc65536` — the `full` dict's content truncated to its
**last 65536 bytes**, approximating what LZ4 can actually reach given its
64 KiB match window. Metrics: `T4_brotli` → `ratio_q5`, `ratio_q11`
(brotli 1.2.0 quality 5/11); `T4_lz4` → `ratio_default`, `ratio_lz4_9`
(lz4 1.10.0 default vs `-9`, capped at `-T4`).

Dictionary content is extracted from a finalized zstd dict via
`tools/dict_header_size` (`ZDICT_getDictHeaderSize()`), **not** by
byte-searching for the default repcode signature `(1,4,8)` — because
`trainer.py`'s stage 3 overwrites that exact field in place with
corpus-specific offsets on any accepted `target_level >= 16` `full` dict,
so the signature is absent from those files even though the header size
itself is unaffected (see `tools/dict_header_size.c`'s header comment).
**Caveat:** the first six `T4_brotli` rows for `github_users` L3 (visible
in `git log` as the commit immediately before "Fix T4 dict-content
extraction: parse header via ZDICT_getDictHeaderSize", `0d5bf07`) were
computed via the old byte-search method (`find_repcode_field`) — check
each row's `note` field for `"via find_repcode_field"` vs
`"via dict_header_size (ZDICT_getDictHeaderSize)"` to tell which method
produced it. Functionally equivalent for L3 dicts (no stage-3 patch there
to corrupt the signature), but flagged for completeness. The switch was
forced by a genuine `T4,ALL,,BLOCK_FAILED,exception,...,"ValueError:
default repcode sequence (1,4,8) not found in dictionary"` row (visible in
that commit's diff) hit on a stage-3-patched L19 dict — that row is a
historical artifact from before the fix and does not represent a currently
unresolved failure.

**Machine-safety guards throughout `measure_v2.csv`** (all baked into
`tools/run_measure_v2.py`, daily-driver Mac): every codec invocation
prefixed `nice -n 10`; lz4 capped at `-T4`; zstd batch runs single-threaded
(no explicit `-T`); scratch dirs purged immediately after each measurement
group; each block checks `df -h /` and aborts+logs if free space < 8 GB.

**Regenerate:**
```bash
python3 tools/run_measure_v2.py 2>&1 | tee -a runs/measure_v2.log
```
Blocks run in order `T1 → T2 → T3 → T4`; one block's exception is caught,
recorded as a `BLOCK_FAILED` row, and does not stop the rest.

# dictforge

A validation-driven zstd dictionary trainer, and the diagnosis of why the
incumbent trainer (`zstd --train` / `--train-cover`, COVER/fastCOVER)
underperforms. `zstd --train` never searches dictionary size, targets a
single hard-coded compression level regardless of what the caller will
actually use, and — past a corpus-dependent budget threshold — its
selection objective actively *prefers* a build that silently failed to
fill the requested size, so asking for a bigger dictionary can hand back
a smaller, worse one with no warning at any verbosity level (§3 of the
paper; `results/benchmark_v2.csv`, `MAIN`/`default` rows). `dictforge`
(`tools/trainer.py`, packaged standalone as `package/dictforge`) fixes
this by treating dictionary construction as a search over candidates
adjudicated by real, measured compression on a held-out validation split:
it sweeps size and trainer family, is aware of the target compression
level, refines content by which bytes real compressions actually
reference, and seeds repeat-offset codes — every stage validation-gated,
with an anytime never-worse-than-the-incumbent floor. Output is a 100%
standard-format zstd dictionary; any stock zstd binary, library, or
language binding can use it with no dictforge runtime dependency.

Target venue: DCC 2027. **Deadline: October 2, 2026.** See `HANDOFF.md`
for what's left.

## Status (as of commit `79a70b0`, 2026-08-28)

- All five benchmark corpora built and split 80/20 (`corpora/`, gitignored
  data + committed generator `corpora/fetch_corpora.py`).
- `results/benchmark_v2.csv` is the **evidence base the paper currently
  cites** (main.tex, `paper/sections/*.md`): MAIN results (5 corpora × 2
  levels × 5 variants), E1 (equal-compute baseline, `--train-cover`),
  E2 (klauspost/compress baseline), E4 (memory). It was produced against
  an older, coarser `SIZE_LADDER` in `tools/trainer.py`.
- `results/benchmark_v3.csv` re-runs **only the MAIN block** against a
  fixed, fine-grained `SIZE_LADDER` (see `tools/trainer.py` comment above
  the constant). **Not yet integrated into the paper** — `tools/make_tables.py`
  and `paper/latex/Makefile` still default to `benchmark_v2.csv`. See
  `HANDOFF.md` for the reconciliation task.
- `results/corrections_v2.csv` redoes two measurement errors an audit
  found in `benchmark_v2.csv`'s E1 and the §3.1 cliff table (both had
  swept `--train-cover` instead of the deployed-default `--train`
  fastCOVER path). Also independently confirms the **REFIT-ON-FULL
  contamination bug** — see `KNOWN_ISSUES.md`, the single biggest open
  correctness issue in the repo right now.
- `results/parity_v2.csv` and `results/measure_v2.csv` add
  matched-size/production-config parity and throughput/memory/bootstrap-CI/
  cross-codec measurements respectively.
- Paper draft (`paper/latex/main.tex`, ported from `paper/sections/*.md`)
  is a complete, fact-checked 10-page DCC scaffold with an appendix of
  supplementary tables/figures to be cut before submission. Author
  affiliation is a placeholder (`paper/latex/main.tex:80`).
- Four upstream `facebook/zstd` fixes are drafted but **not submitted**
  (`upstream/README.md`); one patch file is ready
  (`upstream/0001-dictBuilder-fix-suppressed-diagnostics.patch`).

## Headline results

All figures below are `MAIN`/`full` vs `MAIN`/`default` in
`results/benchmark_v2.csv` (the CSV the paper currently cites; independently
recomputed here, not copied from prose):

| corpus | L3 gain (full vs default) | L19 gain |
|---|---:|---:|
| github_users | +3.1% | +11.5% |
| gharchive | +9.4% | +28.2% |
| weblogs | +14.3% | +32.1% |
| apijson | +6.1% | +16.6% |
| csvrows | +6.7% | +28.2% |

Everything else that matters about how to read these numbers — how much
is dictionary *size* vs *method* (matched-size comparison), how they hold
up against an equal-wall-clock sweep of the incumbent, and the open
correctness caveat on the `full` variant itself — is in
`docs/FINDINGS.md`. Do not quote the table above without reading that
file first.

## Repo layout

| Path | Contents |
|---|---|
| `README.md` | This file. |
| `HANDOFF.md` | Return-after-a-month doc: state, done/left, next actions, decisions pending. |
| `KNOWN_ISSUES.md` | Open bugs/questions with evidence and proposed fixes. |
| `docs/EXPERIMENTS.md` | Data dictionary for every `results/*.csv`. |
| `docs/FINDINGS.md` | The scientific record: claim → evidence → verification status. |
| `corpora/` | `fetch_corpora.py` (committed) builds 5 corpora into gitignored `corpora/<name>/{train,heldout}/`; `corpora/_raw/` holds cached downloads. |
| `notes/` | Research notes not part of the DCC submission (dictionary-drift post-submission angle). |
| `package/dictforge/` | Standalone pip-installable package: `train.py` (packaged copy of `tools/trainer.py`), `native/` (C helper sources), `build_native.sh`, `test_smoke.sh`, `pyproject.toml`. |
| `paper/` | `figures/` (gitignored, generated), `latex/` (`main.tex`, `Makefile`, `tables/` gitignored-generated, `references.bib`), `sections/*.md` (prose drafts main.tex was ported from — still useful, more discursive), `paper_draft.md` (stale v0.2 outline, **superseded**, kept for history). |
| `results/` | The evidence base: `benchmark_v2.csv`, `benchmark_v3.csv`, `corrections_v2.csv`, `parity_v2.csv`, `measure_v2.csv`, `validation_checkpoint.md`. **Do not modify** — see `docs/EXPERIMENTS.md` for schemas and regeneration commands. |
| `runs/`, `runs_v3/` | Gitignored raw artifacts (trained `.dict` files, `.meta.json` sidecars, campaign logs) backing the CSVs above. Frozen evidence — do not modify or delete. |
| `third_party/` | Gitignored: `zstd` (facebook/zstd shallow clone, built) and `klauspost_builddict` (Go tool, built). See `tools/README_zstd_build.md`, `tools/README_klauspost_builddict.md`. |
| `tools/` | Everything that runs experiments: `trainer.py` (the real trainer; `package/dictforge/train.py` is a packaged copy — keep them in sync), campaign runners (`run_campaign*.py`, `run_corrections_v2.py`, `run_parity.py`, `run_measure_v2.py`), native C helpers (`refine_dict.c`, `offset_hist.c`, `dict_header_size.c`, prebuilt binaries), `make_tables.py`/`make_figures.py` (CSV → paper artifacts), `patch_repcodes.py`, `corpora_manifest.py`, `bench_nodict.sh`. |
| `upstream/` | Staged (not submitted) `facebook/zstd` patches + status tracker. |
| `.venv/` | Python venv with numpy, needed only by `tools/measure_v2_bootstrap.py` (T3 bootstrap CIs). |

## Quickstart

Every command below was verified by reading the script it invokes, not
by running it (no CPU-heavy work was performed to write this doc).

**1. Build zstd** (needed by almost everything; gitignored, not vendored):
```bash
git clone --depth 1 https://github.com/facebook/zstd third_party/zstd
cd third_party/zstd && make -j8 zstd && make -j8 lib && cd -
```
See `tools/README_zstd_build.md`. Already built in this checkout at
`third_party/zstd/programs/zstd`.

**2. Build the corpora** (gitignored; ~370MB total; idempotent, skips
already-fetched steps):
```bash
python3 corpora/fetch_corpora.py                    # all 5
python3 corpora/fetch_corpora.py github_users        # one
```
Already built here — check with:
```bash
python3 tools/corpora_manifest.py
```

**3. Train a dictionary** with the in-repo trainer:
```bash
python3 tools/trainer.py --train-dir corpora/github_users/train \
    --out /tmp/my.dict --target-level 19 --time-budget-s 600
```
`--target-level >= 16` requires `tools/refine_dict` and `tools/offset_hist`
next to `trainer.py` (already built here; source in the same directory).

**4. Or install the standalone package:**
```bash
cd package/dictforge
pip install .
# native helpers only needed for --target-level >= 16:
ZSTD_DIR=../../third_party/zstd ./build_native.sh
dictforge --train-dir /path/to/records --out my.dict --target-level 19 --time-budget-s 600
```

**5. Smoke test** (trains on a synthetic corpus, round-trips through the
system's stock `zstd`, ~1 minute):
```bash
cd package/dictforge && ./test_smoke.sh
```

**6. No-dictionary baseline for a corpus/level:**
```bash
./tools/bench_nodict.sh github_users 3
```

**7. Regenerate paper tables/figures from `results/*.csv`:**
```bash
python3 tools/make_tables.py     # -> paper/latex/tables/*.tex (defaults to benchmark_v2.csv + parity_v2.csv)
python3 tools/make_figures.py    # -> paper/figures/*.pdf, *.png (defaults to benchmark_v2.csv)
```

**8. Build the paper PDF** (needs `pdflatex`/`bibtex` on `PATH`):
```bash
cd paper/latex && make           # regenerates tables, then pdflatex+bibtex+pdflatex×2
```

**Do not casually rerun the campaign scripts** below — each is a
multi-hour-to-multi-day, resume-safe, CPU-heavy job that appends to a
frozen `results/*.csv` and commits as it goes. See `docs/EXPERIMENTS.md`
for what each one produces and exactly how to regenerate a given CSV if
you actually need to:
`tools/run_campaign.py`, `tools/run_campaign_v3.py`,
`tools/run_corrections_v2.py`, `tools/run_parity.py`,
`tools/run_measure_v2.py`.

## Documentation map

- Returning after time away? Start with `HANDOFF.md`.
- Need to know what a CSV column/row means? `docs/EXPERIMENTS.md`.
- Need to know what we actually found, and how sure we are? `docs/FINDINGS.md`.
- Chasing a bug or an unresolved question? `KNOWN_ISSUES.md`.
- Writing or reviewing the paper? `paper/latex/main.tex` is the source of
  truth; `paper/sections/*.md` are the more discursive drafts it was
  ported from (each still carries its own inline TODO list, some already
  resolved in `main.tex`).

## License

`package/dictforge/LICENSE` (MIT) covers the standalone package. No
top-level LICENSE file exists for the rest of the repo (experiment
scripts, paper sources).

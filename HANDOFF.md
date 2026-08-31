# HANDOFF.md — returning to this project after time away

Read this first. It assumes zero memory of prior sessions. For "what does
this project even do," read `README.md`'s first paragraph, then come back
here.

**Target: DCC 2027. Deadline: October 2, 2026.** As of the last commit
(`79a70b0`, 2026-08-28), that's roughly five weeks out.

## Project state as of the last commit

- Working tree is clean; last 5 commits regenerated `results/benchmark_v3.csv`
  (`MAIN` rows only, all 5 corpora) after fixing a `SIZE_LADDER` blind spot
  in `tools/trainer.py`.
- The paper (`paper/latex/main.tex`, a complete 10-page DCC-scaffolded
  draft ported from `paper/sections/*.md`) still cites **`benchmark_v2.csv`**
  throughout — `benchmark_v3.csv` exists but has not been reconciled into
  the paper yet, and only covers the `MAIN` experiment (no E1/E2/E4).
- An audit late in the project found two real problems in the evidence
  base: (1) the equal-compute baseline (E1) and the §3.1 motivating table
  both accidentally swept `--train-cover` instead of the deployed default
  `--train` — corrected in `results/corrections_v2.csv`, not yet ported
  into `main.tex` prose; (2) a genuine trainer bug (REFIT-ON-FULL
  contamination, `KNOWN_ISSUES.md` #1) that biases the `full` variant's
  acceptance gate and demonstrably makes the headline number *worse* on
  the project's flagship corpus (`github_users` L3: -2.53%) in at least
  one case. Neither is fixed in code yet.
- Six new docs were added in this pass to make the project self-orienting:
  this file, `KNOWN_ISSUES.md`, `docs/EXPERIMENTS.md`, `docs/FINDINGS.md`,
  and a rewritten `README.md`. Nothing under `results/`, `runs/`,
  `runs_v3/`, `paper/latex/main.tex`, or `paper/sections/*.md` was
  modified to produce them — they were read-only inputs.

## What is done

- [x] All 5 corpora built and split (`corpora/`, `corpora/fetch_corpora.py`).
- [x] `zstd` built from source (`third_party/zstd`), `klauspost_builddict`
      built (`third_party/klauspost_builddict`).
- [x] `tools/trainer.py` implemented and working (stages 1-3 + refit),
      packaged standalone at `package/dictforge/` with its own smoke test.
- [x] `results/benchmark_v2.csv`: full MAIN/E1/E2/E4 campaign, all 5
      corpora × 2 levels.
- [x] `results/parity_v2.csv`: Cassandra/ScyllaDB/RocksDB-motivated
      parity + matched-size comparisons, all 5 corpora.
- [x] `results/measure_v2.csv`: throughput (T1), memory (T2), bootstrap
      CIs (T3), cross-codec transfer to brotli/lz4 (T4), all 5 corpora.
- [x] `results/corrections_v2.csv`: E1 and the cliff-table corrected onto
      the deployed-default trainer path.
- [x] `results/benchmark_v3.csv`: MAIN block rerun on a fixed, finer size
      ladder — new, not yet used anywhere else.
- [x] SIZE_LADDER blind-spot bug found and fixed in `tools/trainer.py`
      (commit `3e050b0`).
- [x] `displayLevel` upstream fix drafted, ready to submit
      (`upstream/0001-dictBuilder-fix-suppressed-diagnostics.patch`) but
      **not build-verified**.
- [x] Paper background/diagnosis/design/evaluation/related-work sections
      drafted, fact-checked against live upstream sources (RocksDB,
      ScyllaDB, Cassandra, zstd issue trackers) as of 2026-08-28, and
      ported into a single `main.tex` scaffold with an appendix of
      supplementary tables/figures.
- [x] REFIT-ON-FULL contamination bug diagnosed with exact evidence
      (`KNOWN_ISSUES.md` #1).
- [x] Repo-orientation docs (this pass): `README.md`, `HANDOFF.md`,
      `KNOWN_ISSUES.md`, `docs/EXPERIMENTS.md`, `docs/FINDINGS.md`.

## What is left

- [ ] **Fix or remove the REFIT-ON-FULL contamination bug** in
      `tools/trainer.py` (`KNOWN_ISSUES.md` #1) and regenerate any CSV
      rows that depend on the `full` variant at levels where refit can
      fire and change the outcome (in practice: L3 rows across
      `benchmark_v2.csv`/`benchmark_v3.csv`; L19 rows are dominated by
      stages 2/3 so the effect there is smaller but not proven zero).
- [ ] **Port the corrected E1_FIX/CLIFF_FIX numbers** from
      `results/corrections_v2.csv` into `paper/sections/evaluation.md`
      §6.3 and `paper/latex/main.tex` §`sec:evaluation-equalcompute`
      (`KNOWN_ISSUES.md` #3) — numbers are already computed
      (`docs/FINDINGS.md` Finding 10 has the before/after table), this is
      a prose-porting task, not a new experiment. **Do this only after**
      deciding on the REFIT-ON-FULL fix above, since `E1_FIX`'s numbers
      are compared against `MAIN`/`full`, which the fix will change.
- [ ] **Reconcile `benchmark_v3.csv` into the paper**, or decide not to:
      either rerun E1/E2/E4 on the fixed size ladder and switch
      `tools/make_tables.py`/`paper/latex/Makefile` to `benchmark_v3.csv`,
      or explicitly note in the paper that the fixed ladder only changed
      MAIN-block numbers by a small amount and stick with `benchmark_v2.csv`
      (`KNOWN_ISSUES.md` #5).
- [ ] Re-run throughput, a paired bootstrap over the *main-results*
      heldout deltas specifically (T3 in `measure_v2.csv` currently
      compares `full` vs `default`, which is what's needed — check it's
      being cited correctly), and confirm Brotli/LZ4 cross-codec numbers
      are fully folded into `main.tex` §6.6/§6.7 (currently marked
      `\todo{...}` and bracketed `[TODO: ...]` there even though the
      underlying `measure_v2.csv` data exists — this looks like it's
      mostly a porting task, not a re-measurement task; verify that
      before assuming new experiments are needed).
- [ ] x86 Linux replication of throughput numbers (paper explicitly
      flags this as not done; "Yugen's PC" per the old TODO tracker in
      `paper/paper_draft.md`).
- [ ] Resolve the measurement-determinism question (`KNOWN_ISSUES.md` #2)
      enough to state a noise floor, or explicitly caveat every margin
      under ~0.5% in the paper.
- [ ] Fill in author affiliation, `paper/latex/main.tex:80-84`
      (`KNOWN_ISSUES.md` #4 — decision for the human, see below).
- [ ] Decide on upstream submission of the four drafted `facebook/zstd`
      fixes (`upstream/README.md`) — at minimum, build-verify patch #1
      before submitting anything.
- [ ] Fresh scoop-scan of the literature ≤2 weeks before submission
      (per `main.tex`'s own trailing TODO tracker) — citations were
      fact-checked as of 2026-08-28, not re-checked since.
- [ ] AI-assistance disclosure statement — decide wording and add to the
      submission (per `main.tex`'s TODO tracker and `upstream/README.md`'s
      house rules, both already commit to disclosing, just need final text).
- [ ] Delete the appendix (`\clearpage \appendix ...` in `main.tex`) before
      generating the actual camera-ready/submission PDF — it currently
      holds supplementary material intentionally, per its own header
      comment, and must not ship in the 10-page submission.

## Next actions, in priority order

1. **Decide the REFIT-ON-FULL fix** (`KNOWN_ISSUES.md` #1, three options
   listed there). This blocks almost everything else that touches
   headline numbers. No command to run yet — this is a design decision
   first (see "Decisions awaiting the human" below for what needs human
   input vs. what an agent can just implement once directed).
2. Once decided, implement the fix in `tools/trainer.py` (and keep
   `package/dictforge/train.py` in sync — it's a packaged copy, not a
   symlink; diff them after editing).
3. Regenerate the affected CSV rows. For a quick, cheap check on the
   worst-known case before committing to a full re-run:
   ```bash
   python3 tools/trainer.py --train-dir corpora/github_users/train \
       --out /tmp/check.dict --target-level 3 --time-budget-s 600 --stages all
   ```
   then compare against `results/benchmark_v3.csv`'s
   `MAIN,github_users,3,full` row (currently `9.944199`) — it should now
   be >= the `tuned` row's `10.202290`, per the tool's own never-worse
   guarantee. For the full campaign re-run:
   ```bash
   mkdir -p runs_v3   # or a fresh runs_v4 if you want v3 preserved as a comparison point
   nohup nice -n 10 python3 tools/run_campaign_v3.py > runs_v3/campaign_v3_postfix.log 2>&1 &
   disown
   ```
   (Read `docs/EXPERIMENTS.md`'s `benchmark_v3.csv` section first — this
   script is resume-safe against whatever's already in `benchmark_v3.csv`,
   so you likely want a fresh CSV path if you're comparing pre/post-fix
   rather than mixing them.)
4. Port corrected E1/cliff-table numbers into the paper prose
   (`docs/FINDINGS.md` Finding 10 has the numbers already; this is a
   writing task once step 1-3 settle what "full" means).
5. Resolve the `benchmark_v2` vs `benchmark_v3` question for the paper
   (`KNOWN_ISSUES.md` #5).
6. Work through the remaining `[TODO]`/`\todo{}` markers in
   `paper/latex/main.tex` and `paper/sections/*.md` one at a time — most
   look like porting/verification tasks against data that already exists
   in `results/measure_v2.csv`, not new experiments; check each one
   individually before assuming a rerun is needed.
7. Fresh literature scoop-scan, x86 replication, upstream submission
   decision, affiliation — closer to the deadline, roughly in that order.

## Known open issues / bugs (evidence in `KNOWN_ISSUES.md`)

1. REFIT-ON-FULL contamination (important — see above).
2. Measurement-determinism question — unresolved, conflicting evidence.
3. Paper §6.3 (equal compute) has stale pre-correction numbers.
4. Author affiliation placeholder.
5. `benchmark_v3.csv` not yet integrated into the paper.

## Decisions awaiting the human

- **REFIT-ON-FULL fix** (`KNOWN_ISSUES.md` #1): which of the three
  proposed options (drop refit / train-on-full-from-the-start / gate on a
  second untouched split) to implement. This changes the tool's
  behavior and the paper's headline numbers — not something an agent
  should pick unilaterally.
- **Upstream patch submission** (`upstream/README.md`): nothing is
  submitted to `facebook/zstd` without explicit approval, per that
  directory's own house rules. Patch #1 (`displayLevel` fix) is ready
  pending build verification; patches #2-4 need design discussion
  upstream before they're even drafted as diffs.
- **Tool release**: `package/dictforge/` is a complete, installable
  package (`pyproject.toml`, `LICENSE`, smoke test) but there's no
  evidence in this repo of a decision to publish it (PyPI, GitHub
  release, etc.) — flag to the human before doing so.
- **Paper author/affiliation** (`KNOWN_ISSUES.md` #4): needs the human's
  actual institutional affiliation or an explicit "independent
  researcher" decision.
- **AI-assistance disclosure wording**: `main.tex`'s TODO tracker and
  `upstream/README.md` both commit to disclosing AI assistance but
  neither has final wording — needs the human's voice, not an agent's.

## The deadline

**DCC 2027, submission deadline October 2, 2026.** Format constraints
(subject to re-verification against the real DCC 2027 template once
published — `main.tex`'s header comment already flags this): 10 pages
maximum including references and figures, single-column, 12pt body text.
Before submission: delete the paper's appendix section, resolve every
`\todo{}`/`[TODO: ...]` marker in `main.tex`, fill in the author
affiliation, do a fresh literature scoop-scan, and re-verify the DCC
formatting requirements against whatever the organizers actually publish
(the current `geometry` package setup is a best-effort match to
documented rules, not a verified official template).

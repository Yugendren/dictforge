# Known issues

Open problems only. Resolved issues live in `docs/FINDINGS.md` (with the
audit trail) or in git history. Each entry: evidence, current read, proposed
fix.

---

## 1. REFIT-ON-FULL CONTAMINATION (important — biases the headline `full` numbers)

**What.** `tools/trainer.py`'s `_refit_on_full` (called from `_run_trainer`,
`tools/trainer.py:337-340`) retrains the stage-1 winning `(trainer, size)`
pair on the **full** training directory (fit + val, i.e. 100% of the data the
caller marked as training data), then gates acceptance of that refit on the
**validation split** (`tools/trainer.py:631-632`, `batch_ratio(..., val_dir,
...)`). But that validation split is now a subset of the refit's own training
data — the gate is contaminated and structurally biased in favor of
accepting the refit, regardless of whether the refit actually generalizes
better.

**Evidence.**
- `results/benchmark_v3.csv`, `github_users` L3: `tuned` (`--stages 1`,
  which returns before refit ever runs — see `tools/trainer.py:309-311`) =
  `10.202290`. This is **byte-for-byte the same number** as the
  equal-compute swept-default winner's heldout ratio in
  `results/corrections_v2.csv` (`E1_FIX,github_users,3,default_sweep_winner`
  = `10.202290`) — an independent computation that happens to retrain the
  same `(fastCOVER, 1048576)` configuration and evaluate it honestly on
  heldout.
- `full` (`--stages all`, which does apply refit) = `9.944199` for the same
  corpus/level — **2.53% worse** on true heldout than `tuned`.
- This is not a fluke of one corpus: `full` is worse than `tuned` at L3
  (where stages 2/3 are gated off, so refit is the *only* thing that can
  make them differ) on `github_users` (both v2 and v3) and `csvrows` (both
  v2 and v3); it is better on `gharchive` (v3) and mixed on `weblogs`;
  `apijson` shows refit rejected (identical values) both times. See
  `docs/FINDINGS.md` §"Refit-on-full" for the full table. The bug is a
  biased gate, not a uniformly-harmful step — but it means the acceptance
  decision is not trustworthy, and the worst observed case (github_users,
  -2.53%) is exactly the kind of silent regression the tool's own
  never-worse guarantee (`paper/sections/design.md` §5.5, Proposition 2) is
  supposed to rule out.
- A second, related surprise: `--stages 1` and `--stages all` differ at L3
  at all, even though stages 2/3 are gated off by `target_level < 16`
  (`tools/trainer.py:317-332`). The *only* code path that can produce that
  difference is refit-on-full — worth stating explicitly in the paper if
  this isn't fixed before submission, since a reader who knows stages 2/3
  are L3-inert will otherwise expect `tuned == full` at L3 always (it does
  hold exactly when refit is rejected, e.g. `apijson`).

**Proposed fixes to evaluate (not yet decided — see HANDOFF.md decision
list):**
- (a) Drop the refit-on-full step entirely. Simplest; costs the ~0.1%
  "loses to the plain default when no ladder entry beats the floor" case
  documented in `paper/sections/design.md` §5.1, but removes a biased gate
  from a tool whose entire pitch is an honest, validation-gated selection
  rule.
- (b) Train stage-1 candidates on the *full* train dir from the start, and
  use the val split purely for candidate *selection* (never for training
  any candidate) — this matches the incumbent optimizer's own protocol
  (`COVER_checkTotalCompressedSize` trains and scores in the same pass but
  never re-trains post-selection on data the score was measured on). This
  changes stage 1's semantics, not just refit, so it's the largest change
  of the three options.
- (c) Gate refit on a **second, untouched split** (e.g. carve the 15% val
  set into two halves up front, use one for all stage 1/2/3 decisions and
  reserve the other purely for the refit accept/reject gate). Preserves the
  current architecture with the smallest code change; costs some val-set
  size for every other stage's decisions.

**Status:** open, not yet fixed. Affects the `full` variant used as "our
trainer" throughout the paper's headline numbers (`paper/latex/main.tex`
§6.1 references `benchmark_v2.csv`, which was produced before this bug was
found). The github_users L3 case is the only one where the effect is large
enough to plausibly change a paper claim on its own; elsewhere it is inside
the noise of corpus-to-corpus variation already reported. Still needs a
decision and a fix before the numbers can be called clean.

---

## 2. Measurement-determinism question (unresolved, both pieces of evidence kept)

**What.** Whether batch-mode zstd compression ratio measurement is
reproducible across repeated runs on this machine.

- **Claim of nondeterminism.** An agent working on this project reported
  observing ~0.1–0.3% nondeterminism in batch-mode ratio measurement,
  attributed to zstd's worker threads (`-T8`/`-T6` batch compression jobs
  are multi-threaded; thread-scheduling-dependent tie-breaks in the
  trainer's own `(d,k)` optimizer, or in block-splitting/entropy-table
  selection under threading, were the suspected mechanism). No script or
  log artifact recording this specific run is checked into the repo — it
  is recorded here as a claim, not as reproducible evidence.
- **Failure to reproduce.** A follow-up check on `github_users` and
  `csvrows` at L3 ran batch compression 3 times each and found the batch
  **totals byte-identical** across all 3 repetitions in both cases —
  i.e. the follow-up could not reproduce any nondeterminism at all, on the
  two corpora it checked.

**Why this matters.** Several of the margins reported in `docs/FINDINGS.md`
and the paper (e.g. some matched-size deltas in
`sec:evaluation-parity`/`t4_parity.tex`, some equal-compute deltas) are in
the 0.0–2% range. If a ~0.1–0.3% noise floor is real, those specific
margins are not distinguishable from noise; if it isn't (as the
github_users/csvrows check suggests), they are fine as point estimates
(subject to the separate i.i.d. caveat already in `main.tex`
§`sec:discussion`: heldout files within one corpus are shuffled draws from
a single scrape, not independent draws from a population).

**Status:** unresolved. Record both pieces of evidence rather than
resolving in either direction. Before trusting any margin under ~0.5%,
rerun the specific corpus/level/variant 3× and check for byte-identical
totals the way the follow-up check did.

---

## 3. Paper §6.3 (equal compute) contains stale numbers

`paper/sections/evaluation.md` §6.3 and the corresponding
`paper/latex/main.tex` §`sec:evaluation-equalcompute` prose were written
against the **original** E1 sweep in `results/benchmark_v2.csv`, which
(per `tools/run_corrections_v2.py`'s docstring) incorrectly used
`zstd --train-cover` (COVER) instead of the deployed default
`zstd --train` (fastCOVER) for the equal-compute baseline — inflating the
margin over "best effort at equal compute." The corrected sweep
(`E1_FIX` in `results/corrections_v2.csv`, produced by
`tools/run_corrections_v2.py`) exists and is verified, but the consolidated
update to `main.tex` §6.3's prose numbers has not been made. See
`docs/EXPERIMENTS.md` (corrections_v2.csv section) for the corrected
numbers and `docs/FINDINGS.md` for the before/after comparison.

**Status:** open. `HANDOFF.md` lists this as a priority next action —
it's a direct, mechanical correction (numbers already computed; the task
is porting them into the LaTeX prose and re-checking the "closes the gap
at level 3 / wins on four of five at level 19" framing still holds with
the corrected values).

---

## 4. Author affiliation placeholder

`paper/latex/main.tex:80-84`:
```latex
\author{
  Yugen \\
  \textit{TODO: affiliation} \\
  \texttt{19thkingisreal@gmail.com}
}
```
Needs a real affiliation (or explicit "independent researcher" framing)
before submission. This is a decision for the human, not something an
agent should fill in — see `HANDOFF.md` "Decisions awaiting the human."

---

## 5. `results/benchmark_v3.csv` not yet integrated into the paper

Not a bug, but worth tracking as an open inconsistency: `tools/trainer.py`'s
`SIZE_LADDER` was fixed (commit `3e050b0`, "Fix SIZE_LADDER blind spot") to
add fine rungs in the 196608–2097152 byte region after discovering the old
power-of-two ladder could miss the true optimum by a wide margin (2.53% on
`github_users` L3 alone — see the comment above `SIZE_LADDER` in
`tools/trainer.py`). `results/benchmark_v3.csv` reruns the `MAIN` block
under the fixed ladder, but `tools/make_tables.py`, `tools/make_figures.py`,
and `paper/latex/Makefile` all still default to `results/benchmark_v2.csv`,
and no E1/E2/E4 experiments were rerun for v3. Until this is reconciled,
the paper's numbers and `results/benchmark_v3.csv` describe two different
(similar, but not identical) trainer configurations. See `HANDOFF.md` for
the reconciliation plan.

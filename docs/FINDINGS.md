# FINDINGS.md — the scientific record

Format per finding: **Claim** -> **Evidence** (specific file/row/command) ->
**Verification status** -> **Caveats**. This file exists so someone (human
or agent) a month from now can check every claim without re-running
anything, and so the paper never says more than this file can back up.
Cross-reference: `paper/latex/main.tex` is the camera-facing prose;
`paper/sections/*.md` are the more discursive originals it was ported
from; `docs/EXPERIMENTS.md` is the CSV schema reference this file's
"Evidence" columns point into.

---

## Finding 1: Budget degeneracy -- asking for a bigger dictionary can silently produce a smaller, worse one

**Claim.** Past a corpus-dependent `--maxdict` threshold, `zstd --train`/
`--train-cover` emits a dictionary *smaller* than it did at a lower
request, and compression ratio collapses (24-31% measured), with zero
warning at any `-v` level.

**Evidence.** `results/corrections_v2.csv`, `CLIFF_FIX` rows (the
corrected version of this table -- direct `zstd --train` sweeps on the
full train dir, level 3, run twice per corpus for determinism). E.g.
`github_users`: `default_cliff_1048576_run1/2` both emit the full
1,048,576 bytes requested and reach ratio 9.944199, but the mechanism is
visible in the original (uncorrected but mechanism-identical)
`paper/sections/diagnosis.md` section 3.1 table: 262144->10.06, 524288->9.85,
**1048576->emitted 424524, ratio 7.49** (24% loss), **2097152->emitted
425048, ratio 6.95** (31% loss) -- a request increase that *decreases*
emitted size.

**Verification status.** Mechanism traced to source and independently
re-verified against `facebook/zstd main@82d322c` line numbers (see
Finding 2). The specific magnitude numbers above are from the pre-audit
table; a fully corrected version of this exact table using the fixed
finer `SIZE_LADDER` and the deployed-default trainer exists in
`results/corrections_v2.csv`'s `CLIFF_FIX` rows and should be the source
for any final paper number (`docs/EXPERIMENTS.md` has the regeneration
command).

**Caveats.** We do not claim to reproduce the *magnitude* of a related
upstream report (`facebook/zstd` issue #4127, a Roblox engineer's 512KB->90x
vs 550KB->14x observation) -- their data is private and their payload sizes
sit outside our small-record regime. We claim only that the same failure
*signature* is reachable on public data (zstd's own benchmark corpus) and
that we have a mechanistic explanation for it.

---

## Finding 2: Root cause -- the selection objective can't tell "efficient" from "starved"

**Claim (M1-M3, `paper/sections/diagnosis.md` section 3.2).**
- **M1:** `COVER_computeEpochs` (`cover.c:734-749`) sets epoch count
  proportional to the *requested* budget, not the corpus -- a 2 MiB
  request on a 36 MB corpus at k=50 yields ~42,000 sub-kilobyte epochs.
- **M2:** the build loop terminates after a hard-coded run of
  consecutive zero-score epochs (fastCOVER: 10, `fastcover.c:406`,
  `passes=1`) -- trivially reached once epochs are that small, leaving the
  output buffer substantially unfilled.
- **M3 (root cause):** `COVER_checkTotalCompressedSize`
  (`cover.c:868-918`) seeds each candidate's score with its **emitted**
  size (`cover.c:899`) plus check-set compressed size;
  `COVER_best_finish` keeps the minimum (`cover.c:982+`, esp. `:998`).
  This objective cannot distinguish "small because efficient" from "small
  because the build starved" -- so past a threshold, the optimizer
  *deterministically prefers the most degenerate candidate available*.
- **Confirmation:** on `gharchive` at a 2 MiB request, the winning
  candidate (k=50) had the **worst** compression term of all five
  candidates tried -- it compressed the check set 397 KB worse than the
  runner-up -- and won purely on a 1.62 MB size advantage created by its
  own failed fill.
- **Decomposition:** re-running the `github_users` collapse's winning
  configuration at its pre-threshold size attributes roughly two-thirds
  of the lost ratio to this selection error and one-third to genuine
  dilution of an oversized dictionary on a 6 MB corpus.

**Evidence.** Source-line citations above, verified against
`facebook/zstd main@82d322c` (v1.6.0), the checkout at
`third_party/zstd`. Instrumented-rebuild confirmation is recorded in
`paper/sections/diagnosis.md` section 3.2 (prose) -- no separate machine-readable
artifact beyond that prose exists for the gharchive decomposition; if you
need to re-verify it, re-run the trainer with logging and inspect the
per-candidate scores it prints (`COVER`/`FASTCOVER` `-v -v -v`, subject to
Finding 3 below being fixed first, or read the source directly).

**Verification status.** Source-line claims independently re-checked
2026-08-28 against the current `third_party/zstd` checkout (see
`paper/sections/diagnosis.md` header note and TODO-tracker checkmarks).
High confidence.

**Caveats.** None beyond those noted in Finding 1.

**M4 (minor, compounding defect).** `ZDICT_finalizeDictionary`'s
shrink-to-fit (to make room for the entropy header) keeps the **first**
`dictContentSize` bytes when trimming (`zdict.c:902-935`,
`memmove`-verified), but the builder deliberately fills the output buffer
**back-to-front** so the highest-scoring segments sit at the tail,
nearest the compressed data and cheapest to reference
(`cover.c:795-799`, "We fill the dictionary from the back..."). The
shrink therefore discards exactly the content the builder worked hardest
to place. Verified on `82d322c`; magnitude bound (<=~248 bytes per the
pre-rebuild figure) has **not** been re-measured directly against this
checkout -- `paper/sections/diagnosis.md`'s own TODO tracker flags this as
outstanding.

---

## Finding 3: The diagnostics that would have shown all of this are dead code

**Claim.** `COVER_ctx_init`/`FASTCOVER_ctx_init` assign
`ctx->displayLevel` from the caller's notification level and then
`memset()` the entire context to zero a few lines later -- so every
diagnostic that reads `ctx->displayLevel` back (including the epoch-count
and final-fill-size lines that would have exposed Findings 1-2) is
silently suppressed at **every** `-v` verbosity level.

**Evidence.** `cover.c:639` (assign) vs `:658` (memset);
`fastcover.c:320` (assign) vs `:343` (memset). Fix drafted and ready:
`upstream/0001-dictBuilder-fix-suppressed-diagnostics.patch` (moves the
assignment after the memset). Not yet submitted upstream or even
build-verified in this checkout (`upstream/README.md` status table marks
it "Pending -- deferred to avoid CPU contention with the running benchmark
campaign").

**Verification status.** High confidence, inspection-confirmed,
"trivially correct" per the patch's own status note. **Not yet
build-verified** (patched vs. unpatched binary comparison has not been
run in this checkout).

**Caveats.** This is why the pathology in Findings 1-2 is invisible to a
normal user: the trainer *looks* like it's reporting normally (init-time
diagnostics still print, since those use the function parameter directly,
not the context field), so nothing about the CLI's own output suggests
anything is wrong.

---

## Finding 4: Size blindness -- the trainer never searches dictionary size, and the one path that would let it (`shrinkDict`) is dead

**Claim.** The stock trainer grid-searches `(d, k)` but takes `--maxdict`
as a fixed input; `shrinkDict` -- the mechanism that would let dictionary
*size itself* respond to what actually helps -- is hard-coded off. Optimal
size varies by two orders of magnitude across corpora, and the default
110 KiB budget leaves up to 22% of achievable ratio on the table.

**Evidence.** `cover.c:1214`/`fastcover.c:638` declare `const unsigned
shrinkDict = 0` at both "optimize" entry points; `cover.c:1294`/
`fastcover.c:723` propagate that literal `0` into every candidate's
parameters, overriding whatever the caller actually requested via
`--train-cover=...,shrink`; the shrink path itself (`cover.c:1090`)
returns early whenever `shrinkDict == 0`, so it never executes. Size-curve
evidence: `results/benchmark_v2.csv` `MAIN` rows across corpora/levels --
best `--maxdict` choice ranges from 16384 (`csvrows` A/`ours64`,
`results/parity_v2.csv`) to 2097152 (multiple L19 cases); the 22% figure
is `gharchive` L19 (`110KB-equivalent` vs. best-found `MAIN`/`full` ratio,
per `paper/sections/diagnosis.md` section 3.4).

**Verification status.** Dead-code claim re-verified against `82d322c`
2026-08-28 (`paper/sections/diagnosis.md` TODO-tracker checkmark). High
confidence.

**Caveats.** The ratio-vs-budget curve is not just mis-centred but
locally non-monotone (e.g. `github_users` L3: a 4 KiB dictionary performs
within 0.6% of the 110 KiB default while several intermediate sizes fall
below both) -- a manual sweep is not a safe substitute, which motivates
`dictforge`'s validation-gated search rather than a simple binary/grid
search over size.

---

## Finding 5: Level blindness -- three independent confirmations

**Claim.** The trainer optimizes for one hard-coded level (3, via
`ZSTD_CLEVEL_DEFAULT` inside `ZDICT_trainFromBuffer`/the CLI's optimize
path) regardless of the level the caller will actually use, and three
independent construction decisions invert sign between fast and high
levels:

1. **Size response** inverts: L1-3 peak early and decline; L19 rises
   ~monotonically well past 1 MiB. (Same evidence as Finding 4.)
2. **Repeat-offset seeding** (the `#if 0`'d path at `zdict.c:828-838`,
   commented "the impact of statistics is not properly evaluated" -- we
   evaluate it): **+0.3 to +0.4% at L19, -0.2 to -0.6% at L1.**
3. **Content refinement** (stage 2 of our trainer, see Finding 8): **+1.7 to
   +6.6% at L19, ~0% at L3.**

Mechanism: fast strategies (`ZSTD_fast`/`dfast`) hash dictionary content
into one or two small tables where later positions can overwrite earlier
index entries, and use greedy parsers that can't cost-compare a seeded
repeat offset against an explicit one; L19's `btultra2` strategy sorts
the *entire* dictionary into a binary tree (`ZSTD_updateTree`,
`zstd_compress.c:5043-5056`, "we want the dictionary table fully sorted")
and its optimal parser cost-models every option.

**Evidence.** Source-line citations verified against `82d322c`
(`paper/sections/background.md` section 2.1, mechanism paragraph on
`ZSTD_loadDictionaryContent` strategy dispatch, `zstd_compress.c:5003-5060`).
Repcode-seeding and refinement numbers: `tools/trainer.py` stage 2/3
accept/reject logic (validation-gated, so only *positive* deltas are ever
kept in a shipped `full` dict -- the negative-at-L1 number for repcodes
comes from the fact that stages 2/3 are gated off entirely below L16, a
design decision *motivated by* this finding, not from a shipped
dictionary that regressed).

**Verification status.** (1) and mechanism: high confidence, re-verified
2026-08-28. (2) and (3): magnitudes as measured during stage
development; not independently re-derived as a standalone ablation table
in the current `results/*.csv` files beyond what `MAIN`'s `tuned` vs
`full` delta implies (see Finding 8, "stage 2/3 contribution").

**Caveats -- cross-codec generalization (LZ4).** The paper frames a fourth
instance of level-blindness-like behavior when the *consumer is a
different codec entirely*: LZ4's match window is fixed at 64 KiB, so only
the last 64 KiB of any dictionary is reachable regardless of level. See
Finding 6 below for the direct measurement.

---

## Finding 6: LZ4 window truncation -- dictionary content beyond the last 64 KiB is provably inert for LZ4

**Claim.** For an LZ4-consumed dictionary, content further than 65536
bytes from the end of the dictionary is never referenced, because LZ4's
match window is fixed at 64 KiB regardless of dictionary size. For our
~110 KiB deployed-default-sized dictionary, that means roughly **41-42%**
of the dictionary's content is inert under LZ4 even though it is fully
reachable (and useful) under zstd.

**Evidence -- directly measured.** `results/measure_v2.csv`, `T4_lz4`
experiment: for every one of the 10 (corpus x level) `full_derived`
dictionaries (each ~1 MiB, `MAIN`/`full` dict_size from `benchmark_v2.csv`),
compressing the corpus's full heldout blob with the *entire* dictionary
content produces a compression ratio (`ratio_default` and `ratio_lz4_9`)
**identical to 6 decimal places** to compressing the same blob with only
the dictionary's **last 65536 bytes** (`full_derived_trunc65536`
variant). All 20 cells (5 corpora x 2 levels x 2 lz4 settings) match
exactly -- e.g. `github_users` L3 `ratio_default`: `full_derived` =
`8.547220`, `full_derived_trunc65536` = `8.547220`.

**Evidence -- arithmetic extension to the ScyllaDB-shipped 110 KiB size.**
The `MAIN`/`default` dictionary (the size ScyllaDB actually trains and
ships, per `paper/sections/background.md` section 2.3) is 112,640 bytes
total (`results/benchmark_v2.csv`, `MAIN`/`default`/`dict_size`; content
is slightly smaller once the header is subtracted). LZ4's window is fixed
at 65,536 bytes independent of dictionary size -- the mechanism
demonstrated directly above. `1 - 65536/112640 ~= 41.8%` of that
dictionary's bytes would be unreachable if it were handed to LZ4 as an
external dictionary.

**Verification status.** The direct measurement (ratio equality across 20
cells) is solid: it comes from `results/measure_v2.csv` rows produced by
`tools/run_measure_v2.py`'s T4 block, and identical *ratios* over a fixed
raw-byte blob imply identical *compressed-byte totals*. **However:** this
repository does not contain a byte-level diff/hash of the two `.lz4`
output files confirming literal byte-for-byte identity beyond size --
`run_codec_capture_size` (`tools/run_measure_v2.py`) records only the
output file's size, and the temp `.lz4` files are deleted immediately
after each measurement (`if out_path.exists(): out_path.unlink()`). Treat
"byte-identical output" as *strongly implied by identical compressed
size across every cell tested*, not as independently hash-verified in an
artifact you can inspect. The 41.8% figure for the specific 110 KiB
`default`-sized dictionary is an **arithmetic extension** of the directly
measured mechanism (fixed window size, independent of dictionary length)
-- there is no `default_derived_trunc65536` row in `measure_v2.csv`
directly measuring the 110 KiB case the way there is for the ~1 MiB
`full` case.

**Caveats.** This finding is about **transfer of our zstd-format
dictionaries' raw content to LZ4** (a "what if you handed this content to
a different codec" experiment), not a claim about any deployed system's
actual behavior -- no production system in `paper/sections/background.md`
section 2.3 is known to feed a zstd-trained dictionary to LZ4.

---

## Finding 7: Headline benchmark results

**Claim (paper abstract / section 6.1).** Our trainer's `full` variant
improves aggregate heldout compression ratio over the deployed default
(`zstd --train`, 110 KiB) on every corpus at both levels: **+3.1% to
+14.3% at level 3**, **+11.5% to +32.1% at level 19**.

**Evidence.** `results/benchmark_v2.csv`, `MAIN` experiment, `full` vs
`default` variant, computed independently while writing this file:

| corpus | L3 | L19 |
|---|---:|---:|
| github_users | +3.1% | +11.5% |
| gharchive | +9.4% | +28.2% |
| weblogs | +14.3% | +32.1% |
| apijson | +6.1% | +16.6% |
| csvrows | +6.7% | +28.2% |

**Verification status.** Recomputed directly from the CSV (not copied
from prose) and matches the abstract's stated range exactly. High
confidence **as a description of what `benchmark_v2.csv` contains** --
but see the next finding and `KNOWN_ISSUES.md` #1 before treating the
`full` numbers as the final word.

**Caveats -- this is the single most important caveat in the whole
project.** The `full` variant includes the refit-on-full step, which has
a confirmed contamination bug (`KNOWN_ISSUES.md` #1): on `github_users`
L3, `full` (9.944199) is measurably **worse** than `tuned` (10.202290,
stage-1-only, unaffected by refit) -- a 2.53% self-inflicted loss on the
project's own headline corpus. The aggregate table above is not
recomputed with `tuned` substituted for `full`; doing so and comparing
would change some of the per-corpus percentages (mostly at L3, since
stages 2/3 -- the main driver of the L19 gains -- are unaffected by this
bug). `results/benchmark_v3.csv` reruns `MAIN` under a fixed size ladder
(see `docs/EXPERIMENTS.md`) but does not fix the refit bug either -- its
`tuned`/`full` gap shows the same pattern (compare
`benchmark_v3.csv`'s `github_users` L3 `tuned=10.202290` vs
`full=9.944199`, identical to v2, since the refit logic itself did not
change between v2 and v3).

---

## Finding 8: Stage 2/3 contribution (refinement + repcode seeding)

**Claim.** Stages 2-3 (gated to `target_level >= 16`) contribute +1.0 to
+9.0 percentage points at L19, positive on all five corpora; at L3 they
are gated off and `tuned`/`full` should be identical -- any observed
difference at L3 is attributable to the refit-on-full step, not to
stages 2/3.

**Evidence.** `results/benchmark_v2.csv`, `MAIN`, `tuned` vs `full` at
L19 for all five corpora (all `full > tuned`); at L3, `apijson`'s
`tuned`/`full` are bit-for-bit equal (`4.246075` both -- refit rejected,
confirming the L3-gated-off prediction holds when refit doesn't muddy
it), while `github_users`, `gharchive`, `csvrows`, `weblogs` differ by
small amounts purely due to refit (see Finding 7's caveat and
`KNOWN_ISSUES.md` #1).

**Verification status.** High confidence for the L19 direction and
magnitude (directly tabulated). The L3 "should be identical, differs only
because of refit" claim is a logical deduction from the code
(`tools/trainer.py:317-332` shows stages 2/3 are unconditionally skipped
below level 16) plus the `apijson` control case, not a separately
ablated experiment.

**Caveats.** `paper/latex/main.tex` section `sec:evaluation-ablation`
already states this exact finding and frames the L3 tuned/full gap as
"evidence that byte-identical reproducibility was not independently
confirmed post-rebuild" -- that framing predates the more specific
REFIT-ON-FULL diagnosis in `KNOWN_ISSUES.md` #1 and should probably be
updated to name the mechanism directly rather than leaving it as an open
reproducibility question.

---

## Finding 9: Matched-size comparison -- most of the level-3 gain is size, not method; a real margin survives at level 19

**Claim.** Rebuilding the stock trainer at *our* winning size (rather
than the 110 KiB default) closes almost all of the level-3 gap (0-11% of
the headline gain survives as "method"), but a real margin remains at
level 19.

**Evidence.** `results/parity_v2.csv`, experiment `C` (`default_matched`)
vs the referenced `MAIN`/`full` ratio in each row's note (recomputed
independently):

| corpus | level | matched-size delta (full vs default_matched) |
|---|---|---:|
| github_users | 3 | 0.00% (exact match) |
| gharchive | 3 | +0.19% |
| weblogs | 3 | +1.54% |
| csvrows | 3 | 0.00% (exact match) |
| apijson | 3 | **starved cell, excluded** (see below) |
| github_users | 19 | +6.21% |
| gharchive | 19 | **starved cell, excluded** |
| weblogs | 19 | +6.85% |
| csvrows | 19 | +2.77% |
| apijson | 19 | **starved cell, excluded** |

**Verification status.** Recomputed directly from `results/parity_v2.csv`
and matches `paper/sections/evaluation.md` section 6.2 / `main.tex`
section `sec:evaluation-parity`'s stated numbers (0-11% survival at L3;
+6.2/+6.9/+2.8% at L19) essentially exactly.

**Caveats -- the three excluded cells are not a clean control.** Asked for
our winning budget, the stock trainer itself emitted substantially less
than requested: `gharchive` L19 (892,712 bytes emitted against a
2,097,152-byte request), `apijson` L3 (952,604 against 1,048,576),
`apijson` L19 (952,604 against 1,048,583) -- all in
`results/parity_v2.csv`, experiment `C`. This is Finding 1 (budget
degeneracy) reappearing inside the control experiment itself: the size
confound is not fully separable from the defect under study, since part
of "we used a bigger dictionary" is "we could use a bigger dictionary
(and the incumbent, asked to, could not)."

---

## Finding 10: Corrected equal-compute comparison

**Claim.** Giving the incumbent trainer our wall-clock training budget to
sweep sizes (selecting by measured validation ratio -- the diligent
engineer's manual procedure) closes the level-3 gap and loses narrowly to
us at level 19 on one corpus. **This finding needed correcting**: the
original E1 experiment in `benchmark_v2.csv` swept `zstd --train-cover`
(COVER) instead of the deployed default `zstd --train` (fastCOVER),
inflating our margin. `results/corrections_v2.csv`'s `E1_FIX` redoes it
correctly.

**Evidence -- recomputed independently, both versions, `full` vs winner:**

| corpus | level | original (COVER) delta | corrected (fastCOVER, E1_FIX) delta |
|---|---|---:|---:|
| github_users | 3 | -1.50% | **-2.53%** |
| gharchive | 3 | -0.47% | +0.48% |
| weblogs | 3 | +0.30% | +0.77% |
| apijson | 3 | +2.11% | 0.00% |
| csvrows | 3 | +9.46% | +0.06% |
| github_users | 19 | +6.54% | +0.84% |
| gharchive | 19 | +2.36% | +8.62% |
| weblogs | 19 | +8.46% | +7.31% |
| apijson | 19 | -1.14% | -0.53% |
| csvrows | 19 | +1.64% | +2.60% |

(Negative = the equal-compute sweep beats our `full` result.)

**Verification status.** Recomputed directly from
`results/benchmark_v2.csv` (`E1`, `MAIN`/`full`) and
`results/corrections_v2.csv` (`E1_FIX`). The *direction* of the original
paper's claim (level-3: a wash; level-19: we win 4/5, lose narrowly on
`apijson`) still holds under the correction -- `apijson` L19 is still the
one loss (-0.53% vs the stale -1.14%), and 4/5 corpora still win at L19.
**But the level-3 story changes**: the corrected sweep now beats us
outright on `github_users` (-2.53%), not just "closes the gap." That
specific number is exactly the REFIT-ON-FULL contamination magnitude from
`KNOWN_ISSUES.md` #1 -- `E1_FIX`'s winner (10.202290) is numerically
identical to our own `tuned` (stage-1-only, uncontaminated) result for
that cell. **This -2.53% "loss" is therefore fully attributable to Finding
7/8's refit bug, not to the equal-compute sweep genuinely outperforming
an honest version of our tool.**

**Caveats.** `paper/sections/evaluation.md` section 6.3 and `main.tex`
section `sec:evaluation-equalcompute` still cite the **stale, uncorrected**
numbers ("1.5% above to 9.5% below," which are the *original* column
above, not the corrected one). This is `KNOWN_ISSUES.md` #3 -- a
prioritized, mechanical fix for `HANDOFF.md`.

---

## Finding 11: Alternative trainers are not competitive baselines

**Claim.** `klauspost/compress`'s `builddict` (the only other tool found
that emits standard-format zstd dictionaries with a level-targeting flag)
loses to the plain stock default on all twenty corpus/level/size cells
run, in one case emitting ~17 KiB regardless of a 1 MiB request.

**Evidence.** `results/benchmark_v2.csv`, `E2` experiment, all
`klauspost_*` variant rows vs the corresponding `MAIN`/`default` row.

**Verification status.** Directly tabulated, no correction needed
(unlike E1, E2 was not affected by the COVER-vs-fastCOVER error since
klauspost isn't zstd's trainer at all). `-zlevel` mapping (klauspost's
0-4 tiers approximated to zstd's L3/L19 as 1/4) is explicitly flagged as
approximate in every E2 row's note -- a genuine methodological
limitation, not an error, and already disclosed in the paper.

**Caveats.** Brotli's `dictionary_generator` baseline (planned as "E3")
was never rebuilt and has no rows anywhere in this evidence base -- the
paper cites it from prior knowledge, not from a measurement in this
repo. Do not add an E3 claim to the paper without first rebuilding and
running it.

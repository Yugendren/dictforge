# §5 Design: a validation-driven, level-aware trainer (prose draft)

*Companion to paper/sections/diagnosis.md. Implementation is tools/trainer.py
(packaged as package/dictforge). Written to be ported into main.tex §5.*

The diagnosis in §3 suggests a design rather than an algorithm. The stock
trainer's failures are not failures of its covering heuristic, which is sound
and which we retain unchanged; they are failures of *selection* — of deciding
which candidate dictionary to keep, at what size, for which consumer. Our
trainer therefore treats dictionary construction as a search over candidates
adjudicated by measurement, and spends its effort on making that adjudication
honest.

Two facts make this practical. First, evaluating a candidate is cheap: with
the dictionary in hand, compressing a validation set of a few thousand small
records takes on the order of a second, so a search can afford hundreds of
exact evaluations. Second, the thing we want to optimize — compressed size
under the encoder the user will actually run — is directly measurable, so
there is no need for a proxy objective at the selection layer at all. The
design question is not how to avoid evaluation but how to spend an evaluation
budget.

## 5.0 Data discipline

The caller supplies a single training directory. We split it 85/15 into a
*fit* set and a *validation* set, deterministically by seed: the fit set is
what any underlying trainer sees, and the validation set is what every
accept/reject decision in every stage is measured on. The user's own held-out
data is never opened by the tool. This matters because several of our stages
are greedy loops that would otherwise be free to overfit whatever they are
scored against; separating the sets bounds that risk, and we quantify what
remains in §6 (the largest fit-to-held-out deviation we observed across all
runs is 0.14%).

## 5.1 Stage 1 — size and family search

Stage 1 sweeps a geometric ladder of dictionary sizes from 2 KiB to the
caller's cap, with both `--train` (fastCOVER) and `--train-cover` (COVER),
evaluating every candidate on the validation set at the *target* compression
level and keeping the best. Three details do the real work.

**The floor candidate.** Before anything else, unconditionally, we train and
evaluate the incumbent-equivalent: fastCOVER at min(cap, 110 KiB) — precisely
what `zstd --train` would have produced. It seeds the leader, so every
subsequent decision is a comparison against what the user would have had
anyway. This is what makes the tool never-worse by construction rather than by
hope (Proposition 2).

**Cheap-first ordering.** Candidates are ordered by trainer family, not by
size: all fastCOVER sizes (a few seconds each) run before any COVER size
(which can take a minute or more on a large corpus). Under a time budget this
matters enormously — an earlier version ordered by size, so on large corpora
the expensive trainer consumed the budget at small sizes and the ladder was
never explored, producing dictionaries that lost to the plain default by up to
19%. Ordering cheap-first means budget exhaustion truncates the expensive
tail, never the coverage of sizes.

**A degeneracy guard.** Every candidate's *emitted* size is recorded alongside
its requested size, and a candidate emitting materially less than requested is
flagged. We do not discard such candidates — occasionally the corpus genuinely
cannot fill the budget and the short dictionary is the right answer — but the
flag is surfaced, and because selection is by measured compression rather than
by size (§3.2, M3), a starved build cannot win by virtue of being small.

**Refit on the full set.** If the winner comes from stage 1, it was trained on
85% of the caller's data, while the incumbent it must beat would have used
100%. We therefore retrain the winning (family, size) configuration on the
full training directory and keep it if validation does not regress. Without
this step the tool loses by roughly 0.1% to the plain default in the case
where no ladder entry beats the floor — a small margin, but a systematic one,
and the guarantee should be exact rather than approximate.

## 5.2 Stage 2 — refinement by measured reference (target level ≥ 16)

Stage 1 chooses *which* dictionary; stage 2 improves the one it chose, by
asking a question the covering heuristic never asks: when real compressions
run against this dictionary, which of its bytes do they actually reference?

We answer it by extracting the sequence stream from compressions of the fit
set (`ZSTD_generateSequences`), mapping every match whose offset reaches back
into dictionary space to the dictionary region it consumed, and accumulating a
per-region reference count. Regions that no compression ever touches, and
regions in the weakest decile of those that do, are evicted; the freed budget
is refilled with candidate segments drawn from the fit files that currently
compress worst. Proven content is compacted toward the tail, preserving the
placement the builder intended (§3.2, M4). Each round is accepted only if it
improves measured compressed size on the fit set, and the loop stops at the
first rejection.

The offset-to-region mapping is the one piece of this that is easy to get
subtly wrong, so it self-tests: the tool constructs a synthetic dictionary,
plants a known region inside a synthetic sample, and asserts that coverage
attributes the match to exactly that region. The refinement stage refuses to
run if the self-test fails.

Two empirical notes shape how we present this stage. First, literally-dead
content is rare — typically 0–4% of a trained dictionary is never referenced —
so the gains come from replacing *weakly* referenced content, not from
reclaiming obvious waste. Second, the stage is gated to high compression
levels because at fast levels it measurably does nothing (§3.5); we would
rather ship a stage that declines to run than one that burns minutes for no
effect.

## 5.3 Stage 3 — repeat-offset seeding (target level ≥ 16)

zstd dictionaries carry three seeded recent-offset values that the encoder
starts each frame with. The stock trainer computes the most common offsets
observed during training and then discards them, writing `{1,4,8}` (§3.5). We
measure the top three offsets over the fit set and patch them in, keeping the
result only if validation improves. The effect is small (+0.3–0.4% at level
19) and negative at fast levels, which is why the stage is both gated and
validation-checked rather than applied unconditionally: this is, to our
knowledge, the first published measurement of the feature the comment in
`zdict.c` left unevaluated.

## 5.4 Why the level gates exist

Stages 2 and 3 engage only when the caller declares a target level of 16 or
above. This is not tuning conservatism; it follows from §3.5. At fast levels
the encoder indexes only part of a large dictionary into small hash tables and
parses greedily, so neither curated content nor seeded offsets can be
exploited — and seeded offsets can actively mislead the greedy parser. At
level 19 the optimal parser indexes the whole dictionary and cost-compares its
options, so both stages pay. Rather than average these opposite behaviours
into a mediocre compromise, the tool takes the target level as an input and
does different work for different answers.

## 5.5 Guarantees

Let $S$ be the size ladder, $B$ the caller's budget cap, and
$C(B) = \{(t,s) : t \in \{\text{fastCOVER}, \text{COVER}\},\ s \in S,\ s \le B\}
\cup \{\text{floor}(B)\}$ the candidate set, where
$\text{floor}(B) = (\text{fastCOVER}, \min(B, 110\,\text{KiB}))$. The tool
returns $\arg\max_{c \in C(B)} r_{\text{val}}(c)$, where $r_{\text{val}}$ is
measured compression ratio on the validation split.

**Proposition 1 (budget monotonicity).** For $B_1 \le B_2$,
$C(B_1) \subseteq C(B_2)$, hence
$\max_{C(B_1)} r_{\text{val}} \le \max_{C(B_2)} r_{\text{val}}$: increasing the
budget cannot decrease the selected validation ratio.

**Proposition 2 (anytime floor).** For any time budget sufficient to evaluate
$\text{floor}(B)$ — seconds, and it is evaluated first — the selected
validation ratio is at least that of the incumbent trainer at the same cap.

Both are immediate from the construction, and deliberately so: their content
is not mathematical depth but the observation that the deployed trainer
satisfies neither. Proposition 1 fails empirically for stock zstd by 24–31%
(§3.1) because its candidate quality is not monotone in the budget and its
selection objective prefers the failures; Proposition 2 fails because there is
no floor at all. The guarantees also hold under truncation, since cheap-first
ordering completes the fastCOVER sub-ladder within seconds.

Two honest limits. The guarantees are stated on the validation measure, not on
the user's held-out data; §6 quantifies the gap. And Proposition 1 constrains
the *selected* candidate, not the underlying trainers — a starved build is
still a starved build, we merely decline to select it.

---
**TODO for integration**
- [ ] Cross-reference §6 numbers once parity campaign lands (matched-size table).
- [ ] Confirm "0.14% largest deviation" against benchmark_v2 + parity_v2 rather than the pre-wipe figure.
- [ ] Decide whether Prop 1/2 stay in §5 or move to a boxed "Guarantees" float.
- [ ] The 19% anytime-safety regression figure is from the pre-wipe environment; either re-measure with a deliberately misordered build or restate qualitatively.

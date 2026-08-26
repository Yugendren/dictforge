# §3 Diagnosis (prose draft — the paper's spine)

*Written for direct porting into main.tex. Every number here is from
results/benchmark_v2.csv unless marked; line references are to
facebook/zstd main @ 82d322c (v1.6.0). Nothing in this section is inherited
from the pre-wipe environment: all measurements were reproduced on the
rebuilt toolchain and corpora.*

## 3.1 A trainer that is punished for asking for more

We begin with the behaviour that motivated this work, because it is both the
most consequential defect we found and the one that most cleanly exposes the
underlying design flaw.

Consider a user who has trained a dictionary with `zstd --train-cover` and
wants a better one. The obvious move is to raise `--maxdict`. On our
`github_users` corpus — zstd's own published benchmark sample set — doing so
produces the following (Table~\ref{tab:cliff}, Figure~\ref{fig:cliff}):

| requested `--maxdict` | emitted dictionary | ratio (L3) |
|---:|---:|---:|
| 262 144 | 262 144 | 10.06 |
| 524 288 | 524 288 | 9.85 |
| **1 048 576** | **424 524** | **7.49** |
| **2 097 152** | **425 048** | **6.95** |

Asking for a 1 MiB dictionary yields a 415 KiB one and costs 24% of the
compression ratio; asking for 2 MiB costs 31%. The output is not merely
capped — it is *smaller than what the same trainer produced at half the
request* — and no warning is emitted at any verbosity level. A practitioner
sweeping `--maxdict` to tune their deployment will silently select a worse
dictionary the moment they cross the threshold, and nothing in the tool's
output tells them so.

The symptom is not new; what is new is the explanation. In August 2024 an
engineer integrating zstd dictionaries at Roblox opened a thread on the zstd
tracker describing their tuning process, and recorded among their
observations that "the max dict size is really sensitive, and doesn't just act
as an upper bound: with 2048 & 4096 chunks with 1 copy each, a max dict size
of 512KB gets 90x ratio but 550KB gets 14x ratio" [#4127]. The thread was
answered and closed as completed in December 2024; the trainer's behaviour is
unchanged in the current release, and no explanation of the collapse was
recorded.

We do not reproduce that magnitude — their data is private and their payloads
(~435 KB) sit well outside the small-record regime dictionaries target — but
the signature is the one measured above, on public data, and the mechanism in
§3.2 accounts for it. We are careful to claim only this: the same failure mode
is reachable on zstd's own benchmark corpus, and it has a specific cause.

## 3.2 Mechanism

Three interacting behaviours produce the collapse. We trace each to source and
confirm it by instrumented rebuild.

**M1 — epoch geometry scales with the request, not the data.**
`COVER_computeEpochs` (cover.c:734–749) sets
`epochs.num = maxDictSize / k / passes` and `epochs.size = nbDmers /
epochs.num`. The number of epochs is therefore proportional to the *requested*
budget: raising `--maxdict` shatters a fixed corpus into more, smaller epochs,
from each of which the builder may select at most one k-byte segment. At
`k = 50` and `maxdict = 2 MiB`, a 36 MB training set is divided into ~42 000
epochs of under a kilobyte each.

**M2 — a fixed-count early termination.**
The build loop exits after `maxZeroScoreRun` consecutive epochs yield no
segment covering a not-yet-selected d-mer. `fastcover` hard-codes this to 10
(fastcover.c:406) while using `passes = 1`; `cover` scales it but caps it at
100 (cover.c:763). Ten dead epochs out of tens of thousands is trivially
reached on redundant corpora, so the build stops with the output buffer
substantially unfilled — the direct cause of the shrunken dictionaries above.

**M3 — the selection objective rewards starvation.** This is the root cause.
`COVER_checkTotalCompressedSize` seeds each candidate's score with the
*emitted* dictionary size (cover.c:899) and adds the compressed size of the
check set; `COVER_best_finish` (cover.c:998) keeps the minimum. The objective
therefore cannot distinguish a dictionary that is small *because it is
efficient* from one that is small *because its build starved*. As the
requested budget grows, every well-formed candidate's score inflates by the
full budget while a starved candidate's score stays pinned at its stall size,
so beyond a corpus-dependent threshold the optimizer deterministically selects
the most degenerate build available.

We confirmed this directly. On `gharchive` at a 2 MiB request, the winning
candidate (`k = 50`) had the *worst* compression term of all five candidates —
it compressed the check set 397 KB worse than the runner-up — and won purely
on a 1.62 MB dictionary-size advantage created by its own failure to fill the
buffer. Decomposing the `github_users` collapse by re-running the winning
configuration at the pre-threshold size shows roughly two thirds of the lost
ratio attributable to this selection error and one third to genuine dilution
of an oversized dictionary on a 6 MB corpus.

**M4 — the shrink-to-fit discards the best content (minor).** When the buffer
does fill, `ZDICT_finalizeDictionary` must make room for the entropy header
and shrinks the content by up to ~248 bytes (zdict.c:902–905), keeping the
*first* `dictContentSize` bytes (zdict.c:935). But the builder deliberately
fills back-to-front so that the highest-scoring segments occupy the tail,
nearest the data and cheapest to reference (cover.c:795–799). The shrink
therefore discards precisely the content the builder worked hardest to place.

## 3.3 Why nobody noticed: the diagnostics are dead

The builder contains exactly the output that would expose all of the above —
`"Breaking content into %u epochs of size %u"` and `"Constructed dictionary of
size %u"` — and neither line can ever print. Both `COVER_ctx_init` and
`FASTCOVER_ctx_init` assign `ctx->displayLevel` from the caller's notification
level (cover.c:639; fastcover.c:320) and then `memset` the entire context to
zero a few lines later (cover.c:658; fastcover.c:343). Every diagnostic that
reads the value back therefore sees 0 and is suppressed at *every* `-v` level.

The bug is easy to miss precisely because the trainer still appears to report
normally: diagnostics emitted inside the init functions themselves use the
function parameter, which is still in scope, and print as intended. Only the
build-loop messages — the ones describing epoch geometry and final fill — are
silenced. A user whose 2 MiB request produced a 415 KiB dictionary sees
nothing unusual in the output.

We supply a two-line fix (§8, patch 1).

## 3.4 Size blindness

Beyond the degenerate regime, the trainer never searches dictionary size at
all. It performs a grid search over `(d, k)` but treats `--maxdict` as fixed
input, and the one mechanism intended to address this — `shrinkDict` — is
inert: both optimize entry points declare a local `const unsigned
shrinkDict = 0` (cover.c:1214; fastcover.c:638) and propagate *that* into each
candidate's parameters (cover.c:1294; fastcover.c:723), overwriting whatever
the caller requested, so the documented `--train-cover=...,shrink` option has
no effect and the shrink path (`cover.c:1090`, which returns early when
`shrinkDict == 0`) never runs.

This matters because the optimum is not near the default. Across our five
corpora the best budget ranges from 32 KiB to beyond 2 MiB, and the 110 KiB
default costs up to 22% of achievable ratio (`gharchive`, L19). Worse, the
ratio-versus-budget curve is not merely mis-centred but locally non-monotone
(Figure~\ref{fig:sizecurves}): on `github_users` at L3 a 4 KiB dictionary
performs within 0.6% of the 110 KiB default while intermediate sizes fall
below both. Practitioners have no way to find this without a manual sweep, and
§3.1 shows that a naive sweep is actively dangerous.

## 3.5 Level blindness

The trainer optimizes for a single compression level — level 3 by default
inside `ZDICT_trainFromBuffer`, regardless of the level the caller will
actually use. We find that three independent construction decisions invert
between fast and high levels.

*Size response.* At levels 1–3 the ratio-versus-budget curve peaks early and
declines; at level 19 it rises monotonically well past 1 MiB (§3.4).

*Repeat-offset seeding.* zstd dictionaries may seed the decoder's three recent
offsets. The trainer computes the most common offsets and then discards them,
writing the defaults `{1,4,8}`; the code that would use them sits behind
`#if 0` with the comment that "the impact of statistics is not properly
evaluated" (zdict.c:828–838). We evaluate it: seeding the measured top three
offsets is worth +0.3–0.4% at level 19 and *costs* 0.2–0.6% at level 1.

*Content refinement.* Curating dictionary content by measured reference
density (§5.2) gains 1.7–6.6% at level 19 and approximately nothing at
level 3.

The mechanism is the same in all three cases: fast levels index only part of a
large dictionary into small hash tables, where later positions overwrite
earlier ones, and their greedy parsers cannot cost-compare a seeded repeat
offset against an explicit one. Level 19's optimal parser indexes everything
and evaluates costs. A dictionary is therefore not level-portable, yet the
trainer produces one artifact and the format offers no way to say which level
it was built for.

A fourth instance appears when the consumer is a different codec entirely:
LZ4's match window is capped at 64 KiB, so only the final 64 KiB of any
dictionary is reachable. Our size-searched dictionaries — tuned for zstd —
are therefore mis-sized for LZ4, and only their size-matched variants win
there (+4.4% at LZ4's fast level, §6.4). Dictionary construction must know its
consumer; today it does not.

---
**TODO before submission**
- [x] `shrinkDict` hard-coding re-verified on 82d322c (cover.c:1214/1294,
      fastcover.c:638/723; dead path at cover.c:1090).
- [x] M4 shrink direction re-verified on 82d322c (zdict.c:902-905 shrinks,
      zdict.c:935 `memmove` keeps the FIRST dictContentSize bytes, i.e. drops
      the tail the builder filled last and valued most).
- [ ] M4 magnitude: state the header-size bound from this checkout rather than
      the pre-wipe "~248 bytes" figure (HBUFFSIZE-derived; measure directly).
- [ ] Decide whether the M3 decomposition experiment gets its own table or stays prose.
- [ ] Land patch 1 upstream if accepted before submission; cite the PR.

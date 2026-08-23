# Upstream contributions to facebook/zstd — staged, NOT yet submitted

Everything here is prepared for review and held pending Yugen's approval;
nothing has been posted to the zstd repository or any public tracker.

Source verified against `facebook/zstd` main @ `82d322c` (2026-08-01, reports
as v1.6.0), the same checkout under `../third_party/zstd`.

## Status

| # | Item | Type | Confidence | Build-verified |
|---|---|---|---|---|
| 1 | `0001-dictBuilder-fix-suppressed-diagnostics.patch` | Bug fix | High — inspection-confirmed, trivially correct | **Pending** (deferred to avoid CPU contention with the running benchmark campaign, whose wall-clock timings feed the paper) |
| 2 | Selection-objective fix (charge requested capacity) | Behaviour change | High diagnosis, needs design discussion upstream | Not started |
| 3 | Zero-score tolerance scaling | Behaviour change | Medium — heuristic constant | Not started |
| 4 | `finalizeDictionary` shrink direction | Bug fix (minor) | Medium | Not started |
| 5 | Issue writeup: budget degeneracy diagnosis | Report | — | — |

## 1. Suppressed diagnostics (ready)

`ctx->displayLevel` is assigned and then cleared by the `memset` that zeroes
the context (`cover.c:639` vs `:658`; `fastcover.c:320` vs `:343`). Every
diagnostic reading it back is dead at all verbosity levels — including the
epoch-geometry and "Constructed dictionary of size" lines that would tell a
user their build stopped early and under-filled `--maxdict`.

Independent significance: this is *why* budget degeneracy (item 5) is
invisible in practice. Small, uncontroversial, no behaviour change beyond
restoring intended output — the natural first contribution.

Verification plan before submission:
1. Build patched + unpatched `zstd` from the same checkout.
2. Run `zstd --train-cover -r <corpus> --maxdict=2097152 -v -v -v` on a corpus
   known to trigger early termination (gharchive train).
3. Expect: unpatched prints no epoch/size lines; patched prints
   "Breaking content into N epochs of size M" and "Constructed dictionary of
   size X" where X << 2097152. That contrast is also the screenshot for the
   issue writeup.

## 2. Selection objective (the root cause)

`COVER_checkTotalCompressedSize` seeds the score with the *emitted* dictionary
size (`cover.c:899`), so the `(d,k)` optimizer minimises
`emitted_size + compressed(check set)`. A build that terminates early and
emits a fraction of `--maxdict` therefore scores as if it were parsimonious,
and past a corpus-dependent budget the optimizer deterministically prefers the
most degenerate candidate. Measured consequence: requesting a *larger*
dictionary yields a *smaller* one and up to −28% ratio.

Minimal fix: charge every candidate the requested capacity (available in
`COVER_selectDict`) rather than its emitted size, so candidates compete on
compression at equal budget. Alternative: propagate an "underfilled" flag from
the build and de-prioritise such candidates.

This one changes selection behaviour, so it should follow the issue writeup
and the diagnostics fix rather than arrive cold.

## 3–4

Scale `maxZeroScoreRun` with `epochs.num` in fastcover (currently a hard 10,
with `passes=1`, i.e. ten dead epochs out of tens of thousands ends the build);
and make `ZDICT_finalizeDictionary`'s shrink-to-fit drop content from the
front rather than the tail, since the builder deliberately places its
best segments at the tail.

## 5. Issue writeup

Long-form diagnosis to accompany/quote in the paper. Should reference the
existing report of the same class of failure (issue #4127) without claiming to
have reproduced that exact magnitude — our measured collapses are ~20–28% on
public corpora, versus the 90x→14x reported there on private data.

## House rules for this directory

- Nothing is submitted without explicit approval.
- No claim leaves here that has not been reproduced in the current checkout.
- Patches are attributed to Yugen; AI assistance is disclosed in the paper and
  in any submission that asks.

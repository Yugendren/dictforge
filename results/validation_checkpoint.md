# Validation checkpoint: github_users rebuild vs. recorded reference numbers

Purpose: after rebuilding into durable storage post-tmp-wipe, confirm the
rebuild (corpus split + built zstd binary) reproduces two previously
recorded reference numbers. This is a sanity check on the *pipeline*, not
a claim about the paper's headline results.

Build: `third_party/zstd` shallow clone of facebook/zstd (main @ 82d322c,
reports as v1.6.0), built via `make -j8 zstd && make -j8 lib`.

Split: `corpora/fetch_corpora.py`, github_users corpus, 9,114 files
extracted from the v1.1.3 release sample tarball, split via
`sorted(names)` + `random.Random(1729).shuffle`, 80/20 ->
7,291 train / 1,823 heldout. File count matches spec exactly (~9,114).

## Check (a): no-dict, level 3, batch compression of heldout

Command:
```
zstd -q -f -3 -r heldout --output-dir-flat=<tmpdir>
```

| | Recorded (expected) | Reproduced | Delta |
|---|---|---|---|
| heldout raw bytes | 1,495,279 | 1,491,789 | -3,490 (-0.233%) |
| ratio | 2.956026 | 2.953698 | -0.002328 (-0.0787%) |

Raw byte count is close but not exact (0.23% low), meaning the heldout
file *set* differs slightly from the original run despite using the
documented split algorithm (sorted-then-seeded-shuffle) exactly as
specified. Root cause not fully identified — same file count (9,114),
same algorithm, deterministic Python `random.Random`, yet a different
subset landed in heldout. Possible explanations not yet ruled out:
extraction/tar path producing a different sort key in some edge case,
or the original run's raw file set differing by a handful of files.
Not investigated further since the resulting ratio deviation (0.079%)
is small and well inside any reasonable tolerance.

**Verdict: (a) reproduces closely.** Small (~0.2%) raw-byte and
ratio deviation, plausibly attributable to a slightly different file
subset in heldout, not a broken pipeline.

## Check (b): default-trained dictionary (maxdict=112640), level 3, on heldout

Commands:
```
zstd --train -r train --maxdict=112640 -o dictionary
zstd -q -f -3 -D dictionary -r heldout --output-dir-flat=<tmpdir>
```

Dictionary size produced: 112,640 bytes (matches --maxdict exactly).

| | Recorded (expected) | Reproduced | Delta |
|---|---|---|---|
| ratio | ~9.865986 | 9.646165 | -0.219821 (-2.229%) |

Spec's stated acceptance rule: "if (a) matches but (b) is within ±0.5%,
accept (trainer nondeterminism across zstd versions possible)."

**(a) matches closely (0.079%) but (b) misses the ±0.5% tolerance by a
factor of ~4.5x (2.229% vs 0.5%).** This does NOT meet the spec's
stated acceptance criterion. Reporting as a genuine, unresolved
deviation rather than rounding it into "accept."

Plausible explanation: `zstd --train`'s default optimizer (COVER/
fastCOVER parameter search) is known to be sensitive to trainer
version/build (parameter grid, tie-breaking, thread scheduling can
change which (k,d) candidate wins). The build here is zstd main@82d322c
(reports as v1.6.0); the original run's zstd version/commit is unknown
(not pinned by the spec beyond "the built zstd"). A ~2% swing in
trained-dictionary quality from trainer-version drift is plausible but
NOT independently confirmed here — no A/B against an older zstd tag was
run, so this remains a hypothesis, not a finding.

**Recommendation:** do not treat 9.865986 as a validated reference
number for this rebuild's zstd version. If exact reproduction matters
(e.g., for the paper's baseline claims), either (i) pin the exact zstd
commit/tag used in the original run and rebuild against that, or (ii)
treat 9.865986 as approximate/non-reproducible and re-baseline all
paper claims against the *current* build's own dictionary-trainer
measurements rather than the old recorded number.

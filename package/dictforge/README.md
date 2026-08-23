# dictforge

A zstd dictionary trainer that trains dictionaries beating `zstd --train`
defaults, by fixing three things the stock trainer doesn't do:

- **Auto size selection.** The stock trainer never searches dictionary
  size; you pick a `--maxdict` and it's used almost verbatim. The optimal
  size varies by orders of magnitude across corpora. dictforge sweeps a
  size ladder and picks the size that actually validates best.
- **Target-level awareness.** Dictionary construction decisions that help
  at fast compression levels can hurt at high levels, and vice versa.
  dictforge optimizes for the level you say you'll actually use
  (`--target-level`), not a level-agnostic proxy.
- **Validation-gated refinement, with an anytime never-worse floor.**
  Every optimization stage (content refinement, repeat-offset seeding,
  refit-on-full-data) is only kept if it measurably improves a held-out
  validation split. Because the cheapest, safest candidate is evaluated
  first and every later stage can only replace the current best if it
  wins on validation, dictforge never hands back something worse than
  its own floor candidate -- and it stays clear of the stock trainer's
  budget-degeneracy cliff, where asking for a *larger* dictionary can
  silently produce a *smaller, worse* one past an internal cap.

Output is a **100% standard zstd dictionary file** -- the same format
`zstd --train` produces. dictforge is a training-time tool only; the
dictionary it writes works with any stock zstd binary, any zstd library
binding, any language, no dictforge runtime dependency at all.

## Quickstart

```bash
# 1. Build the native helpers (only needed for --target-level >= 16;
#    skip this if you only ever train at lower levels).
./build_native.sh              # needs a zstd source checkout with `make lib` run;
                                # see build_native.sh for the ZSTD_DIR override

# 2. Train a dictionary.
python3 train.py \
    --train-dir /path/to/your/small/records \
    --out my.dict \
    --target-level 19 \
    --time-budget-s 600

# 3. Use it with ANY stock zstd -- dictforge is out of the picture now.
zstd -19 -D my.dict -o record.zst record.json
zstd -d  -D my.dict -o record.json record.zst
```

Or, installed (`pip install .`):

```bash
dictforge --train-dir /path/to/records --out my.dict --target-level 19
```

Run `./test_smoke.sh` for a fast (~1 minute) end-to-end check: it trains
on a tiny synthetic corpus and round-trips files through your system's
stock `zstd`.

## How it works

1. **fit/val split.** The training directory is split 85/15 (seeded,
   deterministic). All decisions in stages 1-3 are made on the 15%
   validation split; the 85% fit split is what dictionaries are actually
   trained on.
2. **Stage 1 -- size & family search.** Sweeps a fixed ladder of
   `--maxdict` sizes (2KB up to your `--max-budget`, default 2MB) against
   both zstd's `fastcover` and `cover` trainers, cheapest first, plus an
   unconditional small "floor" candidate evaluated before anything else
   so there's always a safe fallback even under a tiny time budget.
   Tracks the emitted dictionary's *actual* size against what was
   requested (the stock trainer can silently emit something much smaller
   than asked) and picks whichever candidate has the best validation
   ratio.
3. **Stage 2 -- coverage-driven refinement** (only engages when
   `--target-level >= 16`). Measures which regions of the dictionary
   real compressions actually reference (via the `refine_dict` native
   helper), evicts weakly-referenced regions, and refills the freed
   space with excerpts from the worst-compressing fit files. Each round
   is only kept if it shrinks the fit-set's total compressed size.
4. **Stage 3 -- repeat-offset seeding** (only engages when
   `--target-level >= 16`). Measures the match offsets real compressions
   actually pick (via the `offset_hist` native helper) and reseeds the
   dictionary's repeat-offset codes with the most common ones, keeping
   the patch only if it improves the held-out validation ratio.
5. **Refit-on-full.** If stages 2-3 never improved on the stage 1
   winner, the winning (trainer, size) pair is retrained one more time
   on the *full* training directory (fit+val, no held-out split) and
   kept only if it's at least as good on the validation split as the
   stage 1 result.

A `<out>.meta.json` sidecar records every decision made along the way
(every candidate tried, every accept/reject and why) for inspection.

## Honest caveats

- **Gains are corpus- and level-dependent.** This is a size/level/content
  search over the same underlying zstd dictionary mechanism, not a new
  compression algorithm; how much it helps depends on how much structure
  your records actually share and which level you compress at.
- **Training takes minutes, not seconds.** The stock trainer runs in
  ~1-2 seconds; dictforge evaluates many candidates against a validation
  split, so a full run is minutes, driven by `--time-budget-s`. This
  cost is paid once, offline, at training time -- it has no effect on
  compression or decompression speed of the dictionary it produces.
- **Stages 2 and 3 only engage at `--target-level >= 16.`** Below that,
  dictforge is effectively "stage 1 with a size/family search"; the
  finer refinement stages target the behavior of zstd's high-effort
  match finders specifically and were not found to reliably help at
  fast levels.
- **Level-19-targeted large dictionaries cost compression speed.**
  Bigger dictionaries at high levels mean more work per compression
  call. This is a compression-time cost only -- decompression speed with
  a zstd dictionary is unaffected by dictionary size or content
  regardless of how the dictionary was produced.

<!-- BENCHMARK TABLE: regenerate from results/benchmark_v2.csv when campaign completes -->

## License

MIT, see `LICENSE`.

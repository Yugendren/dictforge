# dictforge — zstd dictionary trainer project (rebuilt after 2026-08-22 tmp wipe)

## Layout

- `third_party/zstd` — gitignored shallow clone of facebook/zstd, built with
  `make -j8 zstd && make -j8 lib`. See `tools/README_zstd_build.md`.
- `corpora/fetch_corpora.py` — idempotent generator for the five benchmark
  corpora (github_users, gharchive, weblogs, apijson, csvrows), each split
  80/20 train/heldout with seed 1729. Corpus data itself is gitignored
  (large); only the generator script is committed. Run:
  `python3 corpora/fetch_corpora.py [corpus_name ...]`
- `tools/bench_nodict.sh CORPUS LEVEL` — batch no-dictionary compression
  ratio for a corpus's heldout split.
- `tools/corpora_manifest.py` — file-count/byte-size summary across all
  built corpora.
- `results/validation_checkpoint.md` — rebuild validation against two
  previously recorded reference numbers (github_users no-dict and
  trained-dict ratios). One check reproduced within ~0.08%; the other
  (trained-dict ratio) missed the spec's stated ±0.5% tolerance by
  ~2.2% and is reported as an open, unresolved deviation — see that
  file for detail and a recommendation.
- `paper/` — paper draft materials.

## Status (as of this rebuild)

All five corpora built with file counts matching spec exactly:
github_users 9,114 / gharchive 20,000 / weblogs 15,000 / apijson 10,000
(deduped by DOI, no collisions) / csvrows 10,000 (500,000 rows deduped by
unique_key, 50/file).

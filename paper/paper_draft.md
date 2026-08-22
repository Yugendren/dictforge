# Dictionary Training for Zstandard, Revisited: Diagnosis and a Level-Aware Trainer
*Draft v0.2 (reconstructed into durable storage 2026-08-22 after tmp wipe; content consolidated from v0.1 + all session edits). Target: DCC 2027, deadline Oct 2, 2026. 10-page IEEE.*

## Abstract (draft)
Zstandard's dictionary trainer (COVER/fastCOVER, WWW 2016) is the de-facto standard for small-record compression, deployed in RocksDB, ScyllaDB, and the emerging shared-dictionary web ecosystem — yet it has received no algorithmic attention since 2018 despite documented pathological failures. We present the first mechanistic diagnosis of its behavior on real corpora, identifying three defects: (1) *budget degeneracy* — past internal caps, requesting a larger dictionary silently yields a smaller one, collapsing compression ratio by up to 28% in step-function drops on zstd's own benchmark corpus (consistent with, though smaller than, a well-known unresolved issue report); (2) *size blindness* — the trainer never searches dictionary size, though the optimal size varies by two orders of magnitude across corpora and the default budget leaves 2–22% compression on the table; (3) *level blindness* — construction decisions (content selection, size response, repcode seeding) have opposite effects at fast versus high compression levels, yet the trainer targets a single level. Guided by this diagnosis, we build a validation-driven trainer that searches size and trainer family under a target-level objective with an anytime never-worse guarantee, refines dictionary content by measuring which regions real compressions reference, and seeds repeat-offset codes — each stage validation-gated. On five real corpora spanning JSON records, event streams, server logs, and tabular data, our trainer improves compression ratio over `zstd --train` defaults by 1.8–13.8% at level 3 and 6.9–32.3% at level 19; the dictionaries also transfer to Brotli's raw-dictionary mode (+2–27% over the incumbent trainer's). Output is 100%-standard-format, usable by every deployed zstd. Tool and all experiments are open source.

## 1. Introduction
Hook: dictionaries are how zstd compresses small data; small data is where modern systems live (KV stores, logs, telemetry, RFC 9842 web dictionaries). One trainer serves all of it; it is a 2016 heuristic, frozen since 2018, whose maintainer states the ideal builder "does not exist."
Contributions: (1) first mechanistic quantitative diagnosis (budget degeneracy root cause; size blindness; level blindness) incl. likely root cause of issue #4127; (2) level-aware validation-driven trainer with anytime-safety, each component ablated; (3) 5-corpus, 2-level, 7-variant benchmark incl. brotli generator, klauspost builddict, equal-compute stock sweep, no-training controls; cross-codec transfer to Brotli; (4) layout-sensitivity observation (fast-level ratios swing 5–16% under content permutation; L19 barely moves) with honest negative results for naive layout optimization; (5) open-source tool, standard-format output.

## 2. Background
- 2.1 Dictionaries in zstd: prefix semantics, entropy tables, repcodes, attach/copy, level-dependent match finders (verified against zstd dev@82d322c).
- 2.2 COVER/fastCOVER mechanics: d-mer document frequency, greedy segment cover, epochs, (d,k) grid, final selection by real compression (COVER_selectDict); the disabled repcode-seeding path (#if 0, zdict.c ~828); the inert shrinkDict path (hard-coded off in optimize mode). TODO: re-verify dead-code findings against latest release tag.
- 2.3 Deployment reality (source-line verified): RocksDB (ZDICT_trainFromBuffer when use_zstd_dict_trainer=true default; finalize-raw fallback; level 3 default), ScyllaDB (same entry, 110KB, RPC at L1, SSTable L3), Cassandra CEP-54 trunk (zstd-jni level-patched ZDICT_trainFromBuffer, 64KB default, nodetool dictionary import), zstd CLI (fastcover optimize d=8 steps=4 f=20 accel=1, 110KB).

## 3. Diagnosis (measurements on real corpora)
### 3.1 Budget degeneracy (the "cliff")
Measured: github_users/cover requested 524,288→786,432: emitted 524,288→468,431; L3 ratio 10.10→7.28 (−27.9%). gharchive/fastcover: emitted 1,048,576→874,574→476,179 as request grows. MECHANISM (traced & verified via instrumented rebuild):
- M1 epoch geometry: epochs.num = maxDictSize/k/passes grows with REQUESTED budget → sub-KB epochs at small k (cover.c:734-749).
- M2 zero-score termination: build aborts after fixed dead-epoch run (fastcover hardcodes 10, passes=1; fastcover.c:406) → silent underfill.
- M3 ROOT CAUSE: optimizer objective = emitted_dict_size + compressed(check set) (cover.c:899,998) — cannot distinguish "small because efficient" from "small because starved"; past a threshold it deterministically selects the most degenerate build (verified: at 2MiB the winner had the WORST compression term of all candidates). Decomposition: ~2/3 of collapse = degenerate selection, ~1/3 inherent oversize dilution.
- M4 (minor): finalize shrink-to-fit drops the TAIL (highest-value bytes, ≤248B) (zdict.c:902-935).
- BONUS BUG: ctx->displayLevel zeroed by memset AFTER assignment in both trainers (cover.c:639/658, fastcover.c:320/343) → all epoch/fill diagnostics suppressed at every -v level; why the pathology is invisible.
Classification: M2/M3/displayLevel = implementation defects; content-exhaustion pinning = inherent; --maxdict semantics = doc gap. Four upstream fixes drafted (charge requested capacity; scale zero-score tolerance; fix displayLevel; shrink from front).
### 3.2 Size blindness
Ratio-vs-budget curves 2KB–2MB: peaks vary 32KB→>2MB by corpus/level; default 110KB loses up to 22.4% (gharchive L19); curves locally non-monotonic. Trainer greps (d,k) but never size; shrinkDict dead.
### 3.3 Level blindness (three independent confirmations)
(a) size-response inverts by level (L1–3 peak small then decline; L19 monotone rising); (b) repcode seeding: +0.3–0.4% L19, −0.2–0.6% L1 (first proper evaluation of the #if 0 path); (c) refinement: +1.7–6.6% L19, ~0 at L3. Mechanism: fast levels index only a suffix (hash overwrite) with greedy parsers; btopt indexes all and cost-models. NEW (cross-codec, 3rd instance): LZ4's 64KB window makes our size-searched dicts mis-fit (only matched-size content wins) — construction must know its consumer.

## 4. Problem statement (short)
min over D, |D|≤B of Σ|zstd_L(x|D)|. NP-hardness heritage (Storer–Szymanski; smallest-grammar APX-hardness); we treat as blackbox optimization with a ~1-second exact oracle. Conceptual core: evaluation is cheap; the design question is how to spend an evaluation budget.

## 5. Design: validation-driven, level-aware trainer
- 5.0 85/15 fit/validation split; all decisions on validation; heldout single-shot.
- 5.1 Stage 1: size & family search, cheap-trainer-first ordering, unconditional floor candidate (fastcover@min(B,110K)) evaluated first, degeneracy guard (record actual emitted size), refit-on-full for stage1 winners (validation-gated).
- 5.2 Stage 2 (L≥16): reference-measured refinement — coverage map via sequence extraction; evict weakly-referenced regions (dead fraction only 0–4%; wins come from weak content); refill from worst-compressing samples; per-round fit-set acceptance. Coverage-map validation via synthetic planted-region test.
- 5.3 Stage 3 (L≥16): repcode seeding from measured top offsets, validation-gated.
- 5.4 Level gating rationale (ties to §3.3).
- 5.5 THEOREM (half page): Prop 1 (budget monotonicity): C(B1) ⊆ C(B2) for B1≤B2 ⇒ selected validation ratio monotone non-decreasing in B. Prop 2 (anytime floor): any time budget covering the floor candidate ⇒ selected ≥ incumbent-equivalent. Cheap-first ordering preserves Prop 1 under truncation (fastcover sub-ladder complete in ~10s). CONTRAST: stock optimizer violates monotonicity empirically (−20–28% measured). Guarantees on validation measure; heldout deviation bounded by selection noise (measured ≤0.14%). Honest scope: propositions are lightweight by design (DCC-normal; cf. Machete).

## 6. Evaluation
- Setup: 5 corpora (github_users/gharchive/weblogs-NASA/apijson-Crossref/csvrows-NYC311; sources+licenses table), Apple M4 + x86 Linux replication (TODO on PC), zstd dev@82d322c + stock 1.5.7 compat checks, levels 3/19 (+L1 parity), train-only fitting, heldout single-shot.
- 6.1 Main results (recorded; regenerate from rebuilt pipeline): vs default trainer: L3 +1.8/+9.5/+13.8/+5.5/+5.1%; L19 +6.9/+25.2/+32.3/+16.6/+28.5% (avg ~+22%). MATCHED-SIZE columns (parity): Cassandra-64KB: +3.3/+0.1/+0.8/+0.9/0.0%; Scylla-L1: +0.6..+6.1% all wins; RocksDB finalize-raw fallback ranks below default everywhere. Pareto framing headline (TODO figures).
- 6.2 Ablation: tuned(stage1-only) captures majority; stage2 adds +1.0–6.6pp at L19 (4/5 corpora; one −0.14% heldout regression disclosed); stages inert at L3 by design.
- 6.3 Cost: training 2s (default) vs minutes–83min (ours; table); amortization argument + equal-compute baseline (E1: TODO rerun in rebuilt env; partial recorded: gharchive L3 equal-compute cover 8.410 vs ours 8.440; github_users L19 10.270 vs ours 10.605 — gap survives at L19, closes at L3; equal-compute sweep itself hit degenerate builds, rescued only by validation selection = safety argument demonstrated in the control).
- 6.3b Throughput (recorded): decompression flat all 20 configs (±5%); L19 compression −30..−48% MB/s with 2MB dicts (Pareto disclosure; speed-constrained mode = future); L3 neutral. Memory: TODO (E4).
- 6.3c Bootstrap CIs (10k, paired, seed 1729): all 10 full-vs-default deltas positive at 95%; weakest +1.18%; note i.i.d. caveat for temporally correlated corpora.
- 6.4 Cross-codec: Brotli raw-dictionary mode: ours beat default-trainer dicts +2.3..+27.3% (q5/q11); advantage survives header stripping (content-driven). LZ4: no transfer as-is (64KB window; matched-size content +4.4% at fast level) → codec-aware oracle mode as tool feature.
- 6.5 Layout sensitivity: same-content permutations swing L1 ratios 5–16%, L19 ±1%; zstd's tail-packing near-locally-optimal (value-order heuristic loses; 200-iter hill-climb +0.1%); principled hash-aware placement = open problem.
- 6.6 Reproducibility: byte-identical dicts across reruns; seeds; open tool; (post-wipe rebuild doubles as independent replication).

## 7. Related work
COVER/WWW16 (16 citations vs 607 vendored copies — absorbed nameless); klauspost builddict (-zlevel flag = prior level-targeting, parity claimed at best, MUST cite + benchmark); brotli durchschlag; RLZ line (Puglisi et al.; refinement positioned as adaptation of RLZ pruning ideas); Niesen dedup theory; paramgrill + Meta managed-compression blog (parameter tuning disclosed, never published); OpenZL (heavyweight alternative direction).

## 8. Discussion / limitations
Corpus/level dependence; fast-level story = auto-sizing + safety not content magic; matched-size honesty (0–3.3% at Cassandra-64KB); single-machine throughput until x86 replication; per-level dict variants and speed-constrained search as future; upstreaming path (4 fixes PR-able); ML-guided scoring loop deferred (companion work).

## 9. Conclusion
Diagnosis-first beats algorithm-first; a 1-second exact oracle changes the design space; 2016 heuristic + 2026 evaluation discipline = double-digit archival gains, guaranteed-safe defaults, two-codec transfer.

---
## TODO tracker (pre-submission)
- [ ] REBUILD: regenerate all artifacts in durable repo; validate against recorded numbers (in progress).
- [ ] E1–E4 rerun (equal-compute, klauspost, brotli-matched, memory) — was in flight at tmp wipe; partial E1 recorded above.
- [ ] Matched-size main-table columns + Pareto figures (4–6 figures total incl. cliff plot, size curves, Pareto scatter).
- [ ] x86 Linux throughput replication + RocksDB integration demo (Yugen's PC).
- [ ] Re-verify dead-code/#if0 findings against latest release tag.
- [ ] Upstream: submit displayLevel memset fix + objective-charging patch; reference in paper.
- [ ] Retire NASA corpus from headline duty (31-year-old data jab); keep as one of five.
- [ ] LaTeX port (IEEEtran 10pp single-column per DCC), references.bib.
- [ ] Fresh scoop-scan ≤2 weeks before submission. Author/affiliation + AI-assistance disclosure.

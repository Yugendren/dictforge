# §7 Related work (prose draft)

*Positioning is adversarial by design: for each neighbour we state what it
already does, so that our contribution is what remains after subtraction.*

## Dictionary construction

The trainer we study descends directly from Liao, Petri, Moffat and Wirth's
work on relative Lempel-Ziv dictionary construction [COVER], which framed
dictionary building as a covering problem over sampled substrings and
evaluated it on multi-gigabyte web collections. zstd adopted the method,
named it `--train-cover`, and later added a faster approximation
(`fastCOVER`, the current CLI default) that replaces exact per-sample d-mer
counting with a lossy hash table. Both are unchanged in substance since 2018.
We reuse the covering heuristic itself without modification; our contribution
lies entirely in the selection layer around it.

A note on that paper's reception is relevant to our own dissemination
strategy: it has been cited on the order of a dozen times while its code, in
vendored form, appears in hundreds of public repositories. The technique was
absorbed into zstd under a name the paper never used, and credit followed the
feature flag rather than the citation. We take the lesson that a compression
contribution reaches practitioners through a named, maintained artifact, and
reaches the literature only if the paper, the name and the artifact are the
same object.

**Existing alternative trainers.** Two exist in practice. The Go
implementation in `klauspost/compress` emits standard-format zstd
dictionaries and exposes a level-targeting flag — which partially anticipates
our level-awareness argument, and which we therefore benchmark directly. Its
own documentation describes it as experimental and states it will not match
zstd's builder on similar data; our measurements agree and are harsher: across
all twenty corpus-level cells it loses to the stock zstd default, and on one
corpus it silently emits 17 KiB dictionaries regardless of the budget
requested (§6). Brotli's research `dictionary_generator` builds raw-content
dictionaries by a related covering method (`durchschlag`); we include it as a
baseline in the raw-content setting, where it is likewise not competitive
(§6.4). We are aware of no other public tool that targets dictionary quality.

**Parameter tuning as the adjacent problem.** zstd's repository contains
`tests/paramgrill.c`, an unmaintained internal optimizer for compression
parameters (not dictionaries), and Meta has publicly described running it
across production workloads inside an internal managed-compression system. No
methodology, measurements, or working tool from that line has been published.
This is adjacent to our work rather than overlapping — it tunes the encoder,
we build the dictionary — but it establishes that per-workload specialisation
of zstd is known industrial practice and unpublished science. The same is true
of the successor direction, OpenZL, which trains format-specific compression
graphs; its dictionary and training story remains future work in its own
release notes.

## Relative Lempel-Ziv and reference construction

Our refinement stage (§5.2) — evicting dictionary regions that measured
parses do not reference, and refilling from poorly-compressing samples — is
an adaptation of ideas from the RLZ literature, where reference pruning and
reference selection have been studied for repetitive collections
[Hoobin et al.; Puglisi et al.]. We claim novelty not in the idea of pruning
by reference but in doing it against the production encoder's own sequence
stream, at the granularity that encoder actually matches on, with each round
gated by measured compressed size — and in the finding that literal dead
content is rare (0–4%) so that the gains come from displacing *weakly*
referenced content instead.

## Compression papers that diagnose before they optimise

Methodologically, our paper follows a pattern established by recent practical
compression work: measure precisely why the incumbent underperforms, then
design against that measurement. FSST opens by showing that a general-purpose
compressor expands short strings; ALP shows that a lineage of floating-point
codecs never beat a general-purpose compressor on ratio while being far
slower; BtrBlocks changes the unit of measurement from throughput to dollars
per scan; FastCDC profiles chunking to find that hash judgment dominates its
cost. Each contribution is legible because the diagnosis is legible. Our §3 is
written in the same spirit, and like those papers we report the cases where we
lose (§6.2, §6.3) rather than only where we win.

## Theoretical context

Choosing an optimal external dictionary is not a tractable problem in
general: optimal external-pointer macro schemes are NP-complete
[Storer & Szymanski], and the closely related smallest-grammar problem is
APX-hard [Charikar et al.; Casel et al.]. We make no approximation claim.
Our propositions (§5.5) are of a different and deliberately modest kind: they
constrain the *selection* rule, guaranteeing monotonicity in budget and a
never-worse floor against the incumbent, both of which the deployed trainer
violates empirically. Information-theoretic analysis of deduplication
[Niesen] treats a related question — how boundary synchronisation governs
achievable redundancy elimination — but does not address dictionary
construction for a specific encoder.

## Deployment context

Trained dictionaries are used in production by RocksDB, ScyllaDB and, as of
CEP-54, Cassandra; the web ecosystem has recently standardised dictionary
transport, and the browser-side path uses raw-content dictionaries with
Brotli as well as zstd. This matters for our evaluation design: it dictates
the configurations we must match (64 KiB dictionaries at level 3; RPC traffic
at level 1; a no-trainer fallback path), and it is the reason §6 reports
equal-size comparisons at those settings rather than only at our own chosen
sizes.

---
**TODO for integration**
- [ ] Replace bracketed names with \cite keys once references.bib verification lands.
- [ ] Confirm the COVER citation-count claim ("order of a dozen") against a current source, or soften to "few".
- [ ] Decide whether the paramgrill/managed-compression paragraph belongs here or in Discussion as future work.
- [ ] Cross-check the klauspost "17 KiB regardless of budget" figure against the final E2 table.

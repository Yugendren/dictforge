# Next-angle research note: dictionary staleness / drift

Status: NOT part of the DCC submission. Input for the post-submission decision.
Sources: two peer research sessions (2026-08-28), verified against upstream trackers.

## The core distinction (the peer's contribution, and the framing to keep)

Every shipped preset-dictionary implementation is one of two kinds:

**Write-scoped (self-refreshing).** Dictionary trained at write time over exactly
the data it compresses; its lifetime is the lifetime of one immutable unit.
Drift is *structurally impossible*. Cost: never amortises across units.
  - RocksDB (per-SST, retrained each compaction)
  - StarRocks (per column per segment; dict page in that segment's footer)
  - Vortex (trained from the array's own frames, stored in its buffer)
  - Lucene BEST_COMPRESSION (per block group, rebuilt at merge)
  - ClickHouse #110611 as sketched (per part, trained on first block)

**Corpus-scoped (persistent).** Dictionary outlives the data it was trained on
and is applied to future writes. The only camp where drift accumulates:
  - ScyllaDB
  - Cassandra (CEP-54)
  - The web / RFC 9842

That is the entire corpus-scoped population: three systems. Parquet has no
preset-dictionary support and cannot represent one (CompressionCodec is a bare
thrift enum); ORC-45 has been open since 2016 with no code; Iceberg/Delta have
never discussed it.

Why this matters: it pre-answers the obvious reviewer objection ("RocksDB
retrains constantly and is fine") by explaining *why* it is fine, and it
explains the weak practitioner-pain signal — almost nobody has a dictionary
that CAN go stale.

## Evidence that the lifecycle question is unanswered everywhere

- Cassandra shipped dictionary compression in 6.0 and DELETED its auto-training
  code: CASSANDRA-21154, "Remove traces of auto-training ... it is not
  implemented fully for now", description "These should go as it is dead code".
  Deferred to 7.x (CASSANDRA-20937, in progress). Note the CEP-54 wiki still
  describes auto-training as in scope — the wiki is stale vs what shipped.
- CASSANDRA-20939, "Add metrics for ZSTD dictionary compression": status
  Triage Needed. The feature ships with zero observability.
- CASSANDRA-21192 / 21179: timer guardrails on training frequency — confirms
  the shipped model is timer-guardrailed *manual* training.
- ClickHouse #110611 (Milovidov, open, 2026-07): design sketch only; trains on
  a single block to dodge the training-cost question, and pre-declares "the
  benefits will be marginal ... the main goal is to research 'what if'".
- Lance #6141 (open since 2026-03, no team response): lays out per-page /
  per-column / per-dataset scoping as a table; only per-dataset is
  corpus-scoped and its lifecycle is not addressed at all.
- Across every system examined, the retrain trigger is a fixed timer or a
  manual button. Acceptance gates exist (ScyllaDB's 0.95 factor, Cassandra's
  "noticeably better") but they are *candidate-acceptance* gates — they only
  work after you have already paid the training cost. No pre-training
  staleness signal exists anywhere.

## Why retraining is not free (motivate on cost, not on user pain)

- ScyllaDB #25313: "each time we create a new dictionary, we spawn a family of
  sstables that share that dictionary" — accumulation to node memory exhaustion.
- StarRocks: shared dictionaries create *irreversible format commitments*. Read
  support was merged alone (PR #77355, merged 2026-08-20) precisely because the
  write side "makes a cluster undowngradable": a dict-compressed page is a ZSTD
  frame against a raw dictionary (dictID=0), so its header carries no "needs a
  dictionary" signal and an older binary decompresses it *without* the
  dictionary and hits corruption. Enabling/retraining is gated on fleet-wide
  release floors, not just CPU.

Practitioner-pain evidence is genuinely weak (two GitHub hits globally, no HN
discussion). Motivate on retraining cost and format risk instead.

## Pre-registered checks, in order (both must pass before running corpora)

1. **Population check (cheap, do first).** Confirm ScyllaDB dictionaries in
   practice persist long enough for drift to accumulate. If the 900s autotrainer
   tick plus 86400s retrain period replaces dictionaries faster than the data
   distribution moves, then the only shipped corpus-scoped storage engine is
   effectively write-scoped in operation and the addressable population drops
   from three to two. Answerable from source/config semantics alone.
2. **Decay-curve check.** Train at T0, replay a real corpus forward, measure
   against a continuously-retrained control. KILL TRIGGER: if curves are flat
   over months on real corpora, the topic collapses and every system's 24h timer
   is simply over-provisioned. Keep this exactly as worded.

Corpus selection: SSTable-style record streams primary (ScyllaDB is the only
shipped corpus-scoped storage engine). HTTP bundle histories sidestep drift by
construction — weight lower.

Feasibility: Mac-scale. Bounded by zstd throughput; no cluster needed.

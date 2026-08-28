# §2 Background

*Written for direct porting into main.tex. Every mechanical claim below was
re-checked against facebook/zstd main @ 82d322c (v1.6.0), in this checkout at
`third_party/zstd`, independently of any prior notes; line references are to
that checkout. Claims about external deployments (RocksDB, ScyllaDB,
Cassandra) are marked `[CITE: ...]` because those projects are not checked
out here — the integration pass must resolve them against the actual
upstream source before submission.*

## 2.1 Dictionaries in zstd

A zstd dictionary produced by the trainer is not a hint or a side-channel —
it is a self-describing binary artifact in a documented wire format, and any
of its bytes are as inspectable as a compressed frame's. `doc/zstd_compression_format.md:1491`
lays out the layout as four fields in order: `Magic_Number`, `Dictionary_ID`,
`Entropy_Tables`, `Content`. The magic number is the 4-byte little-endian
value `0xEC30A437` (`doc/zstd_compression_format.md:1494`; the same constant
is `ZSTD_MAGIC_DICTIONARY` at `lib/zstd.h:143`, "valid since v0.7.0"). The
4-byte `Dictionary_ID` that follows must be nonzero if present, with the
ranges `<= 32767` and `>= 2^31` reserved for a future public registry
(`doc/zstd_compression_format.md:1496-1510`). `Entropy_Tables` stores, in
fixed order, a Huffman table for literals and FSE tables for offsets, match
lengths, and literal lengths, followed by exactly 12 bytes: three 4-byte
little-endian recent-offset seeds that replace the codec's usual `{1,4,8}`
startup values, each constrained to be nonzero and no larger than the
dictionary's content size (`doc/zstd_compression_format.md:1513-1523`).
Everything after that header is `Content`, which "act[s] as a 'past' in
front of data to compress or decompress" — i.e. a virtual history that
sequence commands may reference by offset as though it had just been
decoded, up to `Window_Size`, after which it becomes unreachable
(`doc/zstd_compression_format.md:1525-1533`). Zstd also accepts dictionaries
with none of this structure — any buffer of at least 8 bytes can be used as
"raw content," which is treated purely as the `Content` field with no magic,
ID, or tables (`doc/zstd_compression_format.md:1481-1484`; the loader chooses
between the two via `ZSTD_dct_auto` / `ZSTD_dct_rawContent` / `ZSTD_dct_fullDict`,
`lib/zstd.h:1374-1376`). Trained dictionaries always use the full,
magic-prefixed format.

Both sides of the codec parse this format independently and identically.
On decompression, `ZSTD_loadEntropy_intoDDict` (`lib/decompress/zstd_ddict.c:89-117`)
checks the magic at offset 0 (`:102-103`), reads the dictionary ID at offset 4
(`:109`), and hands the remainder to `ZSTD_loadDEntropy` for the tables; the
resulting `ZSTD_DDict`'s content pointer is later wired directly into the
decompression context as `prefixStart`/`dictEnd`
(`ZSTD_copyDDictParameters`, `lib/decompress/zstd_ddict.c:56-86`), so decoding
treats dictionary bytes exactly as if they were already-decoded output. On
compression, `ZSTD_loadCEntropy` (`lib/compress/zstd_compress.c:5085-5174`)
skips the 8-byte magic+ID header (`:5090-5092`), reads the Huffman CTable and
the three FSE tables in the same fixed order — offsets, match lengths,
literal lengths (`:5097-5149`) — then reads the 12-byte rep-offset block and
rejects a dictionary whose rep offsets are zero or exceed the content size
(`:5151-5171`). The two loaders are format-symmetric: a dictionary produced
by any compliant writer is fully specified by these four fields, and any
compliant zstd binary — the reference implementation or a reimplementation —
can consume it without linking against, patching, or even having built
`dictBuilder`. This is the first fact the paper leans on: a third-party
trainer that emits bytes in this format is a drop-in replacement for
`zstd --train`, requiring no change to any zstd library or client.

The `Content` bytes, however, are not indexed uniformly — how much of them a
given compressor can actually reach as match candidates depends on the
compression level's match-finder configuration, not on the dictionary alone.
`ZSTD_loadDictionaryContent` (`lib/compress/zstd_compress.c:4925-5064`) is
the single function responsible for indexing dictionary content into the
match finder, and it switches on `params->cParams.strategy` (`:5003-5060`):
`ZSTD_fast` fills one hash table (`ZSTD_fillHashTable`, `:5005-5007`);
`ZSTD_dfast` fills two (`ZSTD_fillDoubleHashTable`, `:5008-5014`);
`ZSTD_greedy`/`ZSTD_lazy`/`ZSTD_lazy2` populate either a row-hash or a
chain-based hash table depending on `useRowMatchFinder` (`:5016-5041`); and
the binary-tree family `ZSTD_btlazy2`/`ZSTD_btopt`/`ZSTD_btultra`/`ZSTD_btultra2`
fully sorts the dictionary into a binary tree via `ZSTD_updateTree`
(`:5043-5056`, with the comment "we want the dictionary table fully
sorted" at `:5043`). These are not equivalent-effort operations: a hash
table records at most one (or a handful of) candidate positions per hash
bucket, so later dictionary content can silently overwrite the index entries
of earlier content, while the binary-tree strategies retain every position.
Within the hash-table strategies there is a second axis of variation,
`ZSTD_dictTableLoadMethod_e` (`lib/compress/zstd_compress_internal.h:548`,
values `ZSTD_dtlm_fast` / `ZSTD_dtlm_full`): for the `ZSTD_fast` strategy,
`ZSTD_fillHashTableForCDict` (used only when digesting a dictionary into a
`ZSTD_CDict`, always with `dtlm_full`; asserted at `lib/compress/zstd_fast.c:31`,
called at `lib/compress/zstd_compress.c:5618`) inserts every third position
directly and additionally fills any still-empty bucket at the two skipped
offsets (`lib/compress/zstd_fast.c:33-48`), whereas `ZSTD_fillHashTableForCCtx`
(used everywhere else a dictionary or prefix is attached to a raw `CCtx` —
streaming dictionary loads, `ZSTD_CCtx_refPrefix`, and one-shot dictionary
compression — always with `dtlm_fast`, asserted at `lib/compress/zstd_fast.c:68`,
called at e.g. `lib/compress/zstd_compress.c:5336, 5349, 5491, 5878, 6447`)
only inserts the sparser stride positions and does not backfill
(`lib/compress/zstd_fast.c:53-77`). The practical consequence is that the
same dictionary bytes, at the same compression level and strategy, are
indexed more completely when pre-digested into a `ZSTD_CDict` than when
attached as a raw prefix per call. This is the second fact the paper leans
on: "the dictionary" is not one fixed object from the match finder's point
of view — how much of it is reachable is a function of level (which selects
strategy, hash/chain log sizes, and `dtlm`), not of the dictionary file
alone.

Two further mechanisms round out the picture. First, whether a `CCtx`
*attaches* to a digested `ZSTD_CDict`'s tables directly or *copies* them into
its own workspace is decided by `ZSTD_shouldAttachDict`
(`lib/compress/zstd_compress.c:2333-2346`) against per-strategy size cutoffs
in `attachDictSizeCutoffs` (`:2320-2331`): 8 KB for `ZSTD_fast`, `ZSTD_btultra`,
and `ZSTD_btultra2`; 16 KB for `ZSTD_dfast`; and 32 KB for `ZSTD_greedy`,
`ZSTD_lazy`, `ZSTD_lazy2`, `ZSTD_btlazy2`, and `ZSTD_btopt`. Attachment is
chosen when the pledged source size is at or below the cutoff (or unknown,
or the caller forces attachment), and copying is chosen above it or when the
caller forces a copy; a "dedicated dictionary search" `CDict` always
attaches regardless of size. Second, dictionaries carry not just content but
statistics, and those statistics can be reused across blocks rather than
re-transmitted: `ZSTD_loadCEntropy` marks each loaded table's repeat-mode
flag (`HUF_repeat_valid`/`FSE_repeat_valid` vs. the conservative
`_check` variants) so that a compressed block referencing the dictionary's
Huffman or FSE tables can skip re-encoding them, subject to
`ZSTD_dictNCountRepeat` (`lib/compress/zstd_compress.c:5071-5083`), which
downgrades a table to `_check` whenever the dictionary's own symbol coverage
is narrower than what a given block would need. The three recent-offset
seeds described above ride the same mechanism at the sequence level: they
are read into `bs->rep[0..2]` (`lib/compress/zstd_compress.c:5152-5155`) and
become the encoder/decoder's starting "recent offsets," in place of the
codec's built-in defaults `repStartValue = {1, 4, 8}`
(`lib/common/zstd_internal.h:65`).

## 2.2 How the trainer works — COVER and fastCOVER

Both COVER and fastCOVER solve the same problem — pick dictionary content
that will be referenced often — with the same coarse algorithm (count d-mer
frequencies, greedily select high-scoring windows epoch by epoch, fill the
output buffer from the back), but they diverge sharply in how they count.

**Frequency counting.** COVER builds an exact suffix array over all
`d`-length substrings ("d-mers") of the training corpus and groups equal
d-mers together with a generic `COVER_groupBy` (`lib/dictBuilder/cover.c:404-420`).
`COVER_group` (`:426-478`) then computes each d-mer's frequency as a
*document frequency with deduplication*: walking each group of positions
sharing the same d-mer, it increments the count only once per training
sample, tracking sample boundaries via `ctx->offsets` and a binary search
(`COVER_lower_bound`) to detect when a new sample has started
(`:441-471`); the comment at `:453-455` states the rationale directly —
"[d]ictionaries only help for the first reference to the dmer. After that
zstd can reference the match from the previous reference. So only count each
dmer once for each sample it is in." fastCOVER abandons the exact suffix
array for a fixed-size hash table of `2^f` counters and counts *raw
occurrences*, with neither deduplication nor collision resolution:
`FASTCOVER_computeFrequency` (`lib/dictBuilder/fastcover.c:277-295`) slides
across each training sample with a stride of `skip+1` positions
(`accelParams.skip`, controlled by the `accel` parameter) and increments
`freqs[dmerIndex]++` at every step (`:291`), where `dmerIndex` is
`FASTCOVER_hashPtrToIndex` — `ZSTD_hash6Ptr` or `ZSTD_hash8Ptr` truncated to
`f` bits (`lib/dictBuilder/fastcover.c:84-89`). Because the table has only
`2^f` slots and two different d-mers can hash to the same slot, this
counting is lossy in the literal sense: unrelated d-mers' occurrence counts
can be summed together, and a d-mer that appears many times within one
sample is counted every time rather than once. This is the algorithmic price
fastCOVER pays for replacing an O(corpus) suffix-array build with an
O(corpus) streaming hash pass.

**Epoch partitioning and greedy selection.** Both variants divide the
corpus's d-mer index into epochs and select at most one segment from each.
`COVER_computeEpochs` (`lib/dictBuilder/cover.c:734-749`) sets
`epochs.num = max(1, maxDictSize / k / passes)` and
`epochs.size = nbDmers / epochs.num`, falling back to a floor of
`minEpochSize = k * 10` epochs when the corpus is too small to support the
requested count; `COVER_buildDictionary` calls it with `passes = 4`
hard-coded at the call site (`:761-762`). Within each epoch,
`COVER_selectSegment` (`:492-569`) slides a window of `k - d + 1` d-mer
positions across the epoch, maintaining
`score = sum of freqs of distinct d-mers currently inside the window`: a
d-mer's frequency is added to the score only the first time it enters the
window (`*newDmerOcc == 0` check, `:517-523`) and subtracted only when its
last occurrence leaves (`:534-538`), so repeated d-mers within one window
are not double-counted. The best-scoring window in the epoch is kept
(`:541-544`), trimmed of any zero-frequency d-mers at its boundary
(`:546-560`), and then — critically — every d-mer it covers has its
frequency zeroed in the shared `freqs` array (`:561-567`, "Zero out the
frequency of each dmer covered by the chosen segment") so that later epochs
cannot re-select content this epoch already claimed. The winning segments
are copied into the output buffer from the back forward:
`COVER_buildDictionary` (`:754-807`) decrements a `tail` cursor and
`memcpy`s each segment to `dict + tail` (`:795-799`), with the comment
explaining why — "We fill the dictionary from the back to allow the best
segments to be referenced with the smallest offsets" — since dictionary
content sits immediately before the data being compressed, and the closer
content is to that boundary, the cheaper (in bits) an offset referencing it
is.

**Grid search and selection by measured compression.** Neither `d` nor `k`
is fixed a priori; the "optimize" entry points sweep a grid and keep the
winner. `ZDICT_optimizeTrainFromBuffer_cover`
(`lib/dictBuilder/cover.c:1197-1213`) defaults `d` to the range `{6, 8}`
(step 2) and `k` to `[50, 2000]` with a step size of
`max((kMaxK - kMinK) / steps, 1)` where `steps` itself defaults to 40; the
identical structure appears in `ZDICT_optimizeTrainFromBuffer_fastCover`
(`lib/dictBuilder/fastcover.c:628-638`). For every `(d, k)` pair tried, the
resulting candidate dictionary is finalized (entropy tables and header
attached via `ZDICT_finalizeDictionary`) and then actually scored by
compression: `COVER_checkTotalCompressedSize`
(`lib/dictBuilder/cover.c:868-918`) builds a real `ZSTD_CDict` from the
candidate bytes at the caller's requested compression level
(`:892-894`), compresses every sample in the held-out check set with
`ZSTD_compress_usingCDict`, and sums the compressed sizes, seeded at
`dictBufferCapacity` (`:899`); `COVER_best_finish`
(`:982-1013` and following) keeps whichever candidate, across all worker
threads, produced the smallest total. Selection is therefore not a proxy
metric — it is literal measured compression of a check set, split from the
training samples by `splitPoint` (`ZDICT_cover_params_t`/`ZDICT_fastCover_params_t`).

**CLI defaults.** Running `zstd --train` with no further flags exercises
exactly this fastCOVER-optimize path with a specific, hard-coded
configuration. `dictType dict = fastCover;` is the CLI's default builder
(`programs/zstdcli.c:937`), and `defaultFastCoverParams()`
(`programs/zstdcli.c:593-605`) sets `d = 8`, `f = 20`, `steps = 4`,
`splitPoint = 0.75`, and `accel = DEFAULT_ACCEL` where
`DEFAULT_ACCEL` is `1` (`programs/zstdcli.c:98`); `k` is left at `0`. Because
`k` (and, redundantly, `d`) are unset, the CLI's own optimize flag —
`int const optimize = !fastCoverParams.k || !fastCoverParams.d;`
(`programs/zstdcli.c:1501`) — evaluates true, so `d` is pinned to 8
(`kMinD = kMaxD = 8` inside the library's optimize function, since
`parameters->d != 0`) while `k` is grid-searched over `[50, 2000]` in 4
steps. `--maxdict` defaults to `g_defaultMaxDictSize = 110 KB`
(`programs/zstdcli.c:85`), which — with `KB` defined as `*(1 << 10)`
(`programs/fileio_common.h:22`) — is `110 * 1024 = 112640` bytes. This is
not a CLI-only convention: the library's own zero-configuration entry point,
`ZDICT_trainFromBuffer` (`lib/dictBuilder/zdict.c:1111-1127`), hard-codes
the identical `d = 8, steps = 4` and leaves `f`, `accel`, and `splitPoint` at
`0`, which fall through to the same defaults inside
`ZDICT_optimizeTrainFromBuffer_fastCover` (`f = 20` via `DEFAULT_F`,
`lib/dictBuilder/fastcover.c:46`; `accel = 1` via `DEFAULT_ACCEL`,
`fastcover.c:47`; `splitPoint = 0.75` via `FASTCOVER_DEFAULT_SPLITPOINT`,
`fastcover.c:45`) — using, for the check-set scoring, `ZSTD_CLEVEL_DEFAULT = 3`
(`lib/zstd.h:134`). The CLI and "just call the library function" are the
same code path with the same numbers.

## 2.3 Deployment reality

Every production system this paper compares against ultimately calls one of
two zstd entry points: `ZDICT_trainFromBuffer` — verified above to be
fastCOVER-optimize with `d=8, steps=4, f=20, accel=1, splitPoint=0.75`,
scored at `ZSTD_CLEVEL_DEFAULT = 3` — or, when no trainer is invoked at all,
`ZDICT_finalizeDictionary` (`lib/zdict.h:227-265`, implementation beginning
`lib/dictBuilder/zdict.c:862`), which skips COVER/fastCOVER's content
*selection* step entirely: it takes a caller-supplied content buffer (which
may simply be a slice of raw, unselected samples) and attaches only the
format header — magic, dictionary ID, entropy tables, and rep-offset seeds —
around it. Both entry points are zstd-side and verified in this checkout;
which specific downstream systems call which, and with what parameters, is
not something this repository can verify without the upstream source
checked out, and each such claim below is marked accordingly.

**Verified against live upstream source, 2026-08-28** (this replaces the
earlier `[CITE: ...]` placeholders below with checked facts; see
`references.bib` and the corresponding footnotes in `main.tex` §2.2 for the
exact commit pins).

RocksDB's dictionary path is opt-in and *off by default*:
`CompressionOptions::max_dict_bytes` defaults to `0`
(`include/rocksdb/compression_type.h:238`, confirmed on tag `v11.8.1` and on
`main`). Once a caller enables it, `use_zstd_dict_trainer` defaults to
`true` (`compression_type.h:302`), meaning our trainer path — not
`ZDICT_finalizeDictionary` — is RocksDB's own default once dictionaries are
turned on, at 110 KB and level 3. So Experiment D (RocksDB's no-trainer
fallback, §6.4) is deliberately exercising an *opt-in, non-default*
configuration, not evidence that RocksDB moved away from the trainer.

ScyllaDB trains both RPC-compression and SSTable dictionaries through the
same 110 KiB, direct `ZDICT_trainFromBuffer` call
(`message/dict_trainer.hh:76`: `max_dict_size = 110*1024`;
`message/dict_trainer.cc:87`; reused for SSTable dictionaries via
`sstable_dict_autotrainer.cc` → `storage_service::train_dict`
(`service/storage_service.cc:3387`) → `main.cc:2073`
(`netw::zdict_train(sample, {})`), traced directly in `scylladb/scylladb`
master@`233d822abf17cfc2f0813c48169b4f1f3d4d15c9`). The two paths are *not*
equally deployed, however: RPC-dictionary training defaults to off
(`rpc_dict_training_when = NEVER`, `db/config.cc:1201`; and
`internode_compression_enable_advanced` defaults to `false`,
`db/config.cc:1199`) — so our level-1/110 KiB evaluation cell is honestly a
setting *motivated by* ScyllaDB's RPC path, not a claim that anyone runs it
in production. SSTable dictionary compression is the case we previously
understated: it has been available, opt-in, since release 2025.2, and per
commit `adf9c426c2e053126b21fa08a1ecf23c4d96c7b1` (2025-10-31,
"`db/config`: Change Default SSTable Compressor to
`LZ4WithDictsCompressor`") it became ScyllaDB's *default* SSTable
compressor starting with release 2025.4 — confirmed by diffing
`db/config.cc` across `branch-2025.2` (still `LZ4Compressor` default),
`branch-2025.4`, and `branch-2026.1` (both default to
`LZ4WithDictsCompressor`). This means the 110 KiB/level-3 configuration we
already report as "the deployed default" in the main results table is,
specifically for ScyllaDB, production-representative, not merely a
convenient evaluation choice.

Apache Cassandra's CEP-54 is **not** merged to a released version, despite
what an earlier draft of this section claimed. The tracking epic,
[CASSANDRA-20902](https://issues.apache.org/jira/browse/CASSANDRA-20902),
has JIRA status "In Progress" with fix version `7.x` (verified via the
Apache JIRA REST API, 2026-08-28). Its implementation exists only on the
`cassandra-6.0` branch, which itself has shipped only as far as a
`6.0-alpha2` preview (`dlcdn.apache.org/cassandra/` lists 3.0.32, 3.11.19,
4.0.21, 4.1.12, 5.0.9, and 6.0-alpha2 — the latest general release is
5.0.x, no 6.0 GA exists). A related ticket,
[CASSANDRA-21154](https://issues.apache.org/jira/browse/CASSANDRA-21154)
("Remove traces of auto-training for Zstd dictionaries as it is not
implemented fully for now"), is Resolved/Fixed and its description
explicitly calls the removed auto-training code "dead code." We keep the
64 KiB budget as an evaluation target because it is the code's own
default — `CompressionDictionaryTrainingConfig`'s builder sets
`maxDictionarySize = 65536` (line 65 of
`src/java/org/apache/cassandra/db/compression/CompressionDictionaryTrainingConfig.java`,
confirmed present on `cassandra-6.0`@`a0b991d2067968c4965b55b1b7b687cf08591ece`
and on `trunk`, and confirmed *absent* on the `cassandra-5.0` branch,
consistent with CEP-54 being unreleased). CEP-54's own design document
(the Confluence CEP page) recommends a dictionary size "around 100KiB,"
closer to the 110 KiB figure used elsewhere in this paper than to the
code's 64 KiB implementation default — worth flagging as a discrepancy
between the design doc and the current implementation.

We use these three budgets/levels as evaluation targets in the parity
section regardless, but each should be labeled honestly by what it is: a
shipped, now-default configuration (ScyllaDB SSTable, Cassandra's 64 KiB),
an evaluation setting *motivated by* an off-by-default production path
(ScyllaDB RPC), or a setting drawn from unreleased code (Cassandra's full
CEP-54) — not uniformly "production."

---
**TODO before submission**
- [x] Resolved every `[CITE: ...]` marker above against live RocksDB,
      ScyllaDB, and Cassandra source, with exact file:line and commit pins
      (2026-08-28); see `references.bib`.
- [x] Confirmed RocksDB's dictionary-compression default (off) and its
      trainer-vs-finalize default (trainer, once enabled) against
      `include/rocksdb/compression_type.h` directly — this is a different
      "level 3" default from `ZSTD_CLEVEL_DEFAULT` (`lib/zstd.h:134`) and
      the two should not be conflated; noted above.
- [x] Confirmed CEP-54's status is *not* current-release: JIRA epic
      CASSANDRA-20902 is "In Progress" (fix version 7.x), and the
      implementation lives only on the unreleased `cassandra-6.0` branch.
- [ ] Decide whether §2.1's attach-vs-copy and dtlm-fast/full material is
      needed in this much detail here, or whether it should be trimmed once
      it's clear how much §5 (content refinement) and §6.4 (LZ4 transfer)
      actually lean on it — currently kept in full because §3.5's "fast
      levels index only part of a large dictionary" claim depends on it.

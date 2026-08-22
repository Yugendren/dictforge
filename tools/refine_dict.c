/*
 * refine_dict.c -- dictionary coverage introspection + finalize helper.
 *
 * Three modes:
 *
 *   validate
 *       Self-test. Builds an 8KB pseudo-random content buffer, plants a
 *       known 200-byte excerpt inside a synthetic sample, runs the
 *       coverage machinery on it, and checks the matched dictionary bytes
 *       land only in the planted window. Must PASS before the "coverage"
 *       mode is trusted for anything else.
 *
 *   coverage <dict> <content_offset> <samples_dir> <level> <bucket>
 *       For each regular file in samples_dir, generate zstd sequences
 *       against <dict> (loaded as a full formatted or raw-content
 *       dictionary) and attribute matched bytes that land inside the
 *       dictionary's content region back to their content-relative
 *       offset, bucketed by <bucket> bytes. Prints per-bucket counts plus
 *       summary totals.
 *
 *   finalize <content_file> <samples_dir> <out> <level>
 *       Wraps ZDICT_finalizeDictionary(): treats content_file as the raw
 *       dictionary content, samples_dir as the training/finalization
 *       sample set, and writes the finalized (headered) dictionary to
 *       <out>.
 *
 * Build:
 *   cc -O2 -I ../third_party/zstd/lib refine_dict.c \
 *      ../third_party/zstd/lib/libzstd.a -o refine_dict
 * (ZSTD_STATIC_LINKING_ONLY is defined below, not on the command line.)
 */

#define ZSTD_STATIC_LINKING_ONLY
#include "zstd.h"
#include "zdict.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <dirent.h>
#include <sys/stat.h>

/* ------------------------------------------------------------------ */
/* small utilities                                                     */
/* ------------------------------------------------------------------ */

static uint8_t *read_file(const char *path, size_t *outSize) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long sz = ftell(f);
    if (sz < 0) { fclose(f); return NULL; }
    rewind(f);
    uint8_t *buf = malloc((size_t)sz > 0 ? (size_t)sz : 1);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    if (got != (size_t)sz) { free(buf); return NULL; }
    *outSize = (size_t)sz;
    return buf;
}

static int path_cmp(const void *a, const void *b) {
    const char *sa = *(const char * const *)a;
    const char *sb = *(const char * const *)b;
    return strcmp(sa, sb);
}

/* List regular files directly under dir, sorted by name. Caller owns the
 * returned array and each string in it. */
static int list_dir_sorted(const char *dir, char ***outPaths, size_t *outCount) {
    DIR *d = opendir(dir);
    if (!d) return -1;

    size_t cap = 64, n = 0;
    char **paths = malloc(cap * sizeof(char *));
    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        if (ent->d_name[0] == '.') continue;
        char full[4096];
        snprintf(full, sizeof(full), "%s/%s", dir, ent->d_name);
        struct stat st;
        if (stat(full, &st) != 0 || !S_ISREG(st.st_mode)) continue;
        if (n == cap) { cap *= 2; paths = realloc(paths, cap * sizeof(char *)); }
        paths[n++] = strdup(full);
    }
    closedir(d);
    qsort(paths, n, sizeof(char *), path_cmp);
    *outPaths = paths;
    *outCount = n;
    return 0;
}

static void free_paths(char **paths, size_t n) {
    for (size_t i = 0; i < n; i++) free(paths[i]);
    free(paths);
}

/* LCG matching glibc's classic constants: X_{n+1} = 1103515245*X_n + 12345
 * (mod 2^32). Deterministic given a seed; exact constants don't matter for
 * correctness, only that content/noise streams are self-consistent. */
static void lcg_fill(uint8_t *buf, size_t n, uint32_t seed) {
    uint32_t state = seed;
    for (size_t i = 0; i < n; i++) {
        state = (uint32_t)(state * 1103515245u + 12345u);
        buf[i] = (uint8_t)((state >> 16) & 0xFFu);
    }
}

/* ------------------------------------------------------------------ */
/* coverage core                                                       */
/* ------------------------------------------------------------------ */

/* Run ZSTD_generateSequences on one sample against a loaded dictionary,
 * and attribute matched bytes that resolve to negative window offsets
 * (i.e. inside the dictionary's content region) back to content-relative
 * byte positions, bucketed by bucketSize.
 *
 * dictBuf/dictSize: the full dictionary buffer as loaded into the CCtx.
 * contentSize: size of the *content* region at the tail of dictBuf, used
 *   only for coordinate mapping (content bytes are addressed as if they
 *   occupy [-contentSize, 0) immediately before the sample).
 */
static void run_coverage_one_sample(const uint8_t *dictBuf, size_t dictSize,
                                     size_t contentSize,
                                     const uint8_t *sampleBuf, size_t sampleSize,
                                     int level, size_t bucketSize,
                                     unsigned long long *bucketCounts,
                                     size_t nbBuckets,
                                     unsigned long long *totalMatchBytes) {
    if (sampleSize == 0) return;

    ZSTD_CCtx *cctx = ZSTD_createCCtx();
    ZSTD_CCtx_setParameter(cctx, ZSTD_c_compressionLevel, level);
    ZSTD_CCtx_loadDictionary(cctx, dictBuf, dictSize);

    size_t bound = ZSTD_sequenceBound(sampleSize);
    ZSTD_Sequence *seqs = malloc(bound * sizeof(ZSTD_Sequence));
    size_t nbSeqs = ZSTD_generateSequences(cctx, seqs, bound, sampleBuf, sampleSize);
    if (ZSTD_isError(nbSeqs)) {
        fprintf(stderr, "refine_dict: ZSTD_generateSequences failed: %s\n",
                ZSTD_getErrorName(nbSeqs));
        free(seqs);
        ZSTD_freeCCtx(cctx);
        return;
    }

    long long p = 0;
    for (size_t i = 0; i < nbSeqs; i++) {
        unsigned ll = seqs[i].litLength;
        unsigned ml = seqs[i].matchLength;
        unsigned off = seqs[i].offset;

        if (ml > 0) {
            long long windowStart = (long long)(p + ll) - (long long)off;
            if (windowStart < 0) {
                long long hitLen = ml;
                if (hitLen > -windowStart) hitLen = -windowStart;
                long long dictStart = (long long)contentSize + windowStart;
                for (long long b = 0; b < hitLen; b++) {
                    long long pos = dictStart + b;
                    if (pos >= 0 && (size_t)pos < contentSize) {
                        bucketCounts[(size_t)pos / bucketSize]++;
                        (*totalMatchBytes)++;
                    }
                }
            }
        }
        p += (long long)ll + (long long)ml;
    }
    (void)nbBuckets;

    free(seqs);
    ZSTD_freeCCtx(cctx);
}

/* ------------------------------------------------------------------ */
/* mode: validate                                                       */
/* ------------------------------------------------------------------ */

static int mode_validate(void) {
    const size_t contentSize = 8192;
    uint8_t *content = malloc(contentSize);
    lcg_fill(content, contentSize, 12345);

    const size_t noiseLen = 64, excerptLen = 200;
    const size_t sampleSize = noiseLen + excerptLen + noiseLen;
    uint8_t *sample = malloc(sampleSize);

    uint8_t noise[128];
    lcg_fill(noise, sizeof(noise), 999331); /* fresh, independent stream */

    memcpy(sample, noise, noiseLen);
    memcpy(sample + noiseLen, content + 3000, excerptLen);
    memcpy(sample + noiseLen + excerptLen, noise + noiseLen, noiseLen);

    size_t bucketSize = 1; /* byte-level buckets for an exact self-check */
    size_t nbBuckets = contentSize;
    unsigned long long *counts = calloc(nbBuckets, sizeof(unsigned long long));
    unsigned long long total = 0;

    run_coverage_one_sample(content, contentSize, contentSize, sample, sampleSize,
                             19, bucketSize, counts, nbBuckets, &total);

    unsigned long long outside = 0;
    for (size_t i = 0; i < nbBuckets; i++) {
        if (counts[i] > 0 && (i < 3000 || i >= 3200)) outside += counts[i];
    }

    int pass = 1;
    if (total < 150 || total > 250) pass = 0;
    if (outside > 0) pass = 0;

    printf("VALIDATE total_match_bytes=%llu expected~200 outside_window_bytes=%llu\n",
           total, outside);
    printf("%s\n", pass ? "PASS" : "FAIL");

    free(content);
    free(sample);
    free(counts);
    return pass ? 0 : 1;
}

/* ------------------------------------------------------------------ */
/* mode: coverage                                                       */
/* ------------------------------------------------------------------ */

static int mode_coverage(int argc, char **argv) {
    if (argc != 7) {
        fprintf(stderr, "usage: refine_dict coverage <dict> <content_offset> "
                         "<samples_dir> <level> <bucket>\n");
        return 2;
    }
    const char *dictPath = argv[2];
    long contentOffset = atol(argv[3]);
    const char *samplesDir = argv[4];
    int level = atoi(argv[5]);
    long bucketArg = atol(argv[6]);

    if (bucketArg <= 0) { fprintf(stderr, "refine_dict: bucket must be > 0\n"); return 2; }
    size_t bucketSize = (size_t)bucketArg;

    size_t dictSize;
    uint8_t *dictBuf = read_file(dictPath, &dictSize);
    if (!dictBuf) { fprintf(stderr, "refine_dict: cannot read dict %s\n", dictPath); return 1; }
    if (contentOffset < 0 || (size_t)contentOffset > dictSize) {
        fprintf(stderr, "refine_dict: content_offset out of range\n");
        free(dictBuf);
        return 1;
    }
    size_t contentSize = dictSize - (size_t)contentOffset;

    char **paths; size_t nFiles;
    if (list_dir_sorted(samplesDir, &paths, &nFiles) != 0) {
        fprintf(stderr, "refine_dict: cannot list samples dir %s\n", samplesDir);
        free(dictBuf);
        return 1;
    }

    size_t nbBuckets = (contentSize + bucketSize - 1) / bucketSize;
    if (nbBuckets == 0) nbBuckets = 1;
    unsigned long long *counts = calloc(nbBuckets, sizeof(unsigned long long));
    unsigned long long total = 0;

    for (size_t i = 0; i < nFiles; i++) {
        size_t sSize;
        uint8_t *sBuf = read_file(paths[i], &sSize);
        if (!sBuf) {
            fprintf(stderr, "refine_dict: warning: cannot read sample %s, skipping\n", paths[i]);
            continue;
        }
        run_coverage_one_sample(dictBuf, dictSize, contentSize, sBuf, sSize, level,
                                 bucketSize, counts, nbBuckets, &total);
        free(sBuf);
    }

    size_t deadBuckets = 0;
    for (size_t i = 0; i < nbBuckets; i++) if (counts[i] == 0) deadBuckets++;

    printf("DICT_CONTENT_SIZE %zu\n", contentSize);
    printf("FILES %zu\n", nFiles);
    printf("TOTAL_DICT_MATCH_BYTES %llu\n", total);
    printf("TOTAL_BUCKETS %zu\n", nbBuckets);
    printf("DEAD_BUCKETS %zu\n", deadBuckets);
    for (size_t i = 0; i < nbBuckets; i++) {
        printf("BUCKET %zu %llu\n", i, counts[i]);
    }

    free(counts);
    free_paths(paths, nFiles);
    free(dictBuf);
    return 0;
}

/* ------------------------------------------------------------------ */
/* mode: finalize                                                       */
/* ------------------------------------------------------------------ */

static int mode_finalize(int argc, char **argv) {
    if (argc != 6) {
        fprintf(stderr, "usage: refine_dict finalize <content_file> <samples_dir> <out> <level>\n");
        return 2;
    }
    const char *contentFile = argv[2];
    const char *samplesDir = argv[3];
    const char *outPath = argv[4];
    int level = atoi(argv[5]);

    size_t contentSize;
    uint8_t *content = read_file(contentFile, &contentSize);
    if (!content) { fprintf(stderr, "refine_dict: cannot read content file %s\n", contentFile); return 1; }

    char **paths; size_t nFiles;
    if (list_dir_sorted(samplesDir, &paths, &nFiles) != 0 || nFiles == 0) {
        fprintf(stderr, "refine_dict: no samples found in %s\n", samplesDir);
        free(content);
        return 1;
    }

    size_t *sizes = malloc(nFiles * sizeof(size_t));
    uint8_t **bufs = malloc(nFiles * sizeof(uint8_t *));
    size_t totalSamplesSize = 0;
    for (size_t i = 0; i < nFiles; i++) {
        bufs[i] = read_file(paths[i], &sizes[i]);
        if (!bufs[i]) { fprintf(stderr, "refine_dict: cannot read sample %s\n", paths[i]); sizes[i] = 0; bufs[i] = malloc(1); }
        totalSamplesSize += sizes[i];
    }

    uint8_t *concat = malloc(totalSamplesSize > 0 ? totalSamplesSize : 1);
    size_t off = 0;
    for (size_t i = 0; i < nFiles; i++) {
        memcpy(concat + off, bufs[i], sizes[i]);
        off += sizes[i];
        free(bufs[i]);
    }
    free(bufs);

    size_t maxDictSize = contentSize + 8192;
    uint8_t *dst = malloc(maxDictSize);

    ZDICT_params_t params;
    memset(&params, 0, sizeof(params));
    params.compressionLevel = level;

    size_t rc = ZDICT_finalizeDictionary(dst, maxDictSize, content, contentSize,
                                          concat, sizes, (unsigned)nFiles, params);
    if (ZDICT_isError(rc)) {
        fprintf(stderr, "refine_dict: ZDICT_finalizeDictionary failed: %s\n",
                ZDICT_getErrorName(rc));
        free(dst); free(concat); free(sizes); free(content); free_paths(paths, nFiles);
        return 1;
    }

    FILE *f = fopen(outPath, "wb");
    if (!f) {
        fprintf(stderr, "refine_dict: cannot open %s for writing\n", outPath);
        free(dst); free(concat); free(sizes); free(content); free_paths(paths, nFiles);
        return 1;
    }
    fwrite(dst, 1, rc, f);
    fclose(f);

    printf("FINALIZED_SIZE %zu\n", rc);

    free(dst);
    free(concat);
    free(sizes);
    free(content);
    free_paths(paths, nFiles);
    return 0;
}

/* ------------------------------------------------------------------ */

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: refine_dict validate|coverage|finalize ...\n");
        return 2;
    }
    if (strcmp(argv[1], "validate") == 0) return mode_validate();
    if (strcmp(argv[1], "coverage") == 0) return mode_coverage(argc, argv);
    if (strcmp(argv[1], "finalize") == 0) return mode_finalize(argc, argv);
    fprintf(stderr, "refine_dict: unknown mode '%s'\n", argv[1]);
    return 2;
}

/*
 * offset_hist.c -- histogram the match offsets zstd actually picks against
 * a dictionary, to find candidate repeat-offset seeds for patch_repcodes.
 *
 * usage: offset_hist <dict> <samples_dir>
 *
 * For each regular file under samples_dir: load <dict> into a fresh CCtx,
 * run ZSTD_generateSequences() at level 3, and walk the resulting
 * sequences in order, skipping block-delimiter markers (matchLength==0 &&
 * offset==0). Two histograms are built over the raw ZSTD_Sequence.offset
 * values (the actual match distance, not translated to content
 * coordinates):
 *
 *   weighted : only the first two non-delimiter sequences of each file,
 *              weight 3 for the first, weight 1 for the second -- this
 *              emphasizes offsets that recur near the start of files,
 *              which are exactly the kind of thing a fixed repeat-offset
 *              seed can pay off on every single file.
 *   all      : every non-delimiter sequence in every file, weight 1 each
 *              -- an unweighted diagnostic view of the overall offset
 *              distribution.
 *
 * Prints the top 10 offsets (by weight, ties broken by offset ascending)
 * for each histogram.
 *
 * Build:
 *   cc -O2 -I ../third_party/zstd/lib offset_hist.c \
 *      ../third_party/zstd/lib/libzstd.a -o offset_hist
 */

#define ZSTD_STATIC_LINKING_ONLY
#include "zstd.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <dirent.h>
#include <sys/stat.h>

/* ------------------------------------------------------------------ */
/* small utilities (mirrors refine_dict.c)                             */
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

/* ------------------------------------------------------------------ */
/* open-addressing uint64->uint64 histogram                            */
/* ------------------------------------------------------------------ */

typedef struct { uint64_t key; unsigned long long count; int used; } HEntry;
typedef struct { HEntry *entries; size_t cap; size_t n; } HMap;

static void hmap_init(HMap *m, size_t cap) {
    m->cap = cap;
    m->n = 0;
    m->entries = calloc(cap, sizeof(HEntry));
}

static uint64_t hmap_hash(uint64_t k) {
    k ^= k >> 33; k *= 0xff51afd7ed558ccdULL;
    k ^= k >> 33; k *= 0xc4ceb9fe1a85ec53ULL;
    k ^= k >> 33;
    return k;
}

static void hmap_grow(HMap *m);

static void hmap_add(HMap *m, uint64_t key, unsigned long long delta) {
    if ((m->n + 1) * 10 >= m->cap * 7) hmap_grow(m); /* keep load factor < 0.7 */
    size_t idx = (size_t)(hmap_hash(key) & (m->cap - 1));
    for (;;) {
        HEntry *e = &m->entries[idx];
        if (!e->used) { e->used = 1; e->key = key; e->count = delta; m->n++; return; }
        if (e->key == key) { e->count += delta; return; }
        idx = (idx + 1) & (m->cap - 1);
    }
}

static void hmap_grow(HMap *m) {
    HMap bigger;
    hmap_init(&bigger, m->cap * 2);
    for (size_t i = 0; i < m->cap; i++) {
        if (m->entries[i].used) hmap_add(&bigger, m->entries[i].key, m->entries[i].count);
    }
    free(m->entries);
    *m = bigger;
}

static int entry_cmp_desc(const void *a, const void *b) {
    const HEntry *ea = (const HEntry *)a;
    const HEntry *eb = (const HEntry *)b;
    if (ea->count != eb->count) return (ea->count < eb->count) ? 1 : -1;
    if (ea->key != eb->key) return (ea->key < eb->key) ? -1 : 1;
    return 0;
}

static void hmap_print_top10(const HMap *m, const char *label) {
    HEntry *sorted = malloc((m->n > 0 ? m->n : 1) * sizeof(HEntry));
    size_t k = 0;
    for (size_t i = 0; i < m->cap; i++) {
        if (m->entries[i].used) sorted[k++] = m->entries[i];
    }
    qsort(sorted, k, sizeof(HEntry), entry_cmp_desc);
    printf("%s\n", label);
    size_t top = k < 10 ? k : 10;
    for (size_t i = 0; i < top; i++) {
        printf("OFFSET %llu %llu\n", (unsigned long long)sorted[i].key, sorted[i].count);
    }
    free(sorted);
}

/* ------------------------------------------------------------------ */

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: offset_hist <dict> <samples_dir>\n");
        return 2;
    }
    const char *dictPath = argv[1];
    const char *samplesDir = argv[2];
    const int level = 3;

    size_t dictSize;
    uint8_t *dictBuf = read_file(dictPath, &dictSize);
    if (!dictBuf) { fprintf(stderr, "offset_hist: cannot read dict %s\n", dictPath); return 1; }

    char **paths; size_t nFiles;
    if (list_dir_sorted(samplesDir, &paths, &nFiles) != 0) {
        fprintf(stderr, "offset_hist: cannot list samples dir %s\n", samplesDir);
        free(dictBuf);
        return 1;
    }

    HMap weighted, all;
    hmap_init(&weighted, 1024);
    hmap_init(&all, 1024);

    for (size_t i = 0; i < nFiles; i++) {
        size_t sSize;
        uint8_t *sBuf = read_file(paths[i], &sSize);
        if (!sBuf || sSize == 0) { free(sBuf); continue; }

        ZSTD_CCtx *cctx = ZSTD_createCCtx();
        ZSTD_CCtx_setParameter(cctx, ZSTD_c_compressionLevel, level);
        ZSTD_CCtx_loadDictionary(cctx, dictBuf, dictSize);

        size_t bound = ZSTD_sequenceBound(sSize);
        ZSTD_Sequence *seqs = malloc(bound * sizeof(ZSTD_Sequence));
        size_t nbSeqs = ZSTD_generateSequences(cctx, seqs, bound, sBuf, sSize);

        if (!ZSTD_isError(nbSeqs)) {
            int nonDelimSeen = 0;
            for (size_t s = 0; s < nbSeqs && nonDelimSeen < 2; s++) {
                if (seqs[s].matchLength == 0 && seqs[s].offset == 0) continue; /* delimiter */
                unsigned long long w = (nonDelimSeen == 0) ? 3 : 1;
                hmap_add(&weighted, seqs[s].offset, w);
                nonDelimSeen++;
            }
            for (size_t s = 0; s < nbSeqs; s++) {
                if (seqs[s].matchLength == 0 && seqs[s].offset == 0) continue; /* delimiter */
                hmap_add(&all, seqs[s].offset, 1);
            }
        } else {
            fprintf(stderr, "offset_hist: warning: generateSequences failed on %s: %s\n",
                    paths[i], ZSTD_getErrorName(nbSeqs));
        }

        free(seqs);
        ZSTD_freeCCtx(cctx);
        free(sBuf);
    }

    printf("FILES %zu\n", nFiles);
    hmap_print_top10(&weighted, "TOP10_WEIGHTED");
    hmap_print_top10(&all, "TOP10_ALL");

    free(weighted.entries);
    free(all.entries);
    free_paths(paths, nFiles);
    free(dictBuf);
    return 0;
}

/*
 * dict_header_size.c -- print a finalized zstd dictionary's header size
 * (the byte offset at which the raw content region begins), via the
 * library's own ZDICT_getDictHeaderSize().
 *
 * Why this exists (not just a byte-search for the default repcode field):
 * patch_repcodes.py locates the raw-content boundary by searching for the
 * *default* repeat-offset signature (01 00 00 00 04 00 00 00 08 00 00 00)
 * that ZDICT_finalizeDictionary always emits. That search is exactly right
 * for a freshly finalized dictionary, but trainer.py's stage 3 overwrites
 * those same 12 bytes in place with corpus-specific repeat offsets (see
 * <dict>.meta.json's "stage3" block) -- so on a dict where stage 3 was
 * accepted (any target_level >= 16 "full" dict in this project), the
 * default signature is gone and the byte-search finds nothing. Patching
 * only overwrites the 12-byte field's *value*, never its position or the
 * position of anything after it, so the true header size is unaffected --
 * we just need a way to compute it that doesn't depend on the current
 * repcode values. ZDICT_getDictHeaderSize() parses the dictionary's
 * magic/dictID/entropy-table region directly and works regardless of
 * whether the repcode field has been patched.
 *
 * Usage:
 *   dict_header_size <dict_file>
 * Prints the header size (decimal, one line) to stdout. Content bytes are
 * dict_bytes[header_size:].
 *
 * Build:
 *   cc -O2 -I ../third_party/zstd/lib dict_header_size.c \
 *      ../third_party/zstd/lib/libzstd.a -o dict_header_size
 */
#include <stdio.h>
#include <stdlib.h>
#define ZDICT_STATIC_LINKING_ONLY
#include "zdict.h"

static unsigned char *read_file(const char *path, size_t *outSize) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long sz = ftell(f);
    if (sz < 0 || fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    unsigned char *buf = malloc((size_t)sz);
    if (!buf) { fclose(f); return NULL; }
    if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) { fclose(f); free(buf); return NULL; }
    fclose(f);
    *outSize = (size_t)sz;
    return buf;
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: dict_header_size <dict_file>\n");
        return 2;
    }
    size_t dictSize;
    unsigned char *buf = read_file(argv[1], &dictSize);
    if (!buf) {
        fprintf(stderr, "dict_header_size: cannot read %s\n", argv[1]);
        return 1;
    }
    size_t hsize = ZDICT_getDictHeaderSize(buf, dictSize);
    free(buf);
    if (ZDICT_isError(hsize)) {
        fprintf(stderr, "dict_header_size: ZDICT_getDictHeaderSize error: %s\n", ZDICT_getErrorName(hsize));
        return 1;
    }
    printf("%zu\n", hsize);
    return 0;
}

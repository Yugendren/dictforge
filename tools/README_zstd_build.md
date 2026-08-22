# zstd build

third_party/zstd is a shallow clone (--depth 1) of facebook/zstd (main branch, HEAD at build time
resolved to v1.6.0-labeled sources). Built with:

    make -j8 zstd   # programs/zstd CLI
    make -j8 lib    # lib/libzstd.a + shared lib

Binary used for all corpus extraction and benchmarking: third_party/zstd/programs/zstd

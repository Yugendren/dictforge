# klauspost/compress builddict (E2 baseline)

third_party/klauspost_builddict/builddict is `go install`ed from
`github.com/klauspost/compress/dict/cmd/builddict@latest` (v1.19.2 at build
time). It is the only standard-format (zstd-compatible) dictionary trainer
alternative found outside libzstd itself; used as the E2 comparison point in
the benchmark_v2 campaign.

Build:

    GOBIN=/path/to/bin go install github.com/klauspost/compress/dict/cmd/builddict@latest
    cp /path/to/bin/builddict third_party/klauspost_builddict/builddict

CLI relevant flags: `-len` (output dict size), `-zlevel` (0-4, klauspost's
own speed/compression tier -- NOT the same scale as zstd's 1-22 CLI `-#`
levels; run_campaign.py maps our L3/L19 to zlevel 1/4 as an approximation
and records this caveat in the E2 CSV rows), `-o` (output path), positional
arg = directory to walk for training samples.

Verified compatible with our third_party/zstd build: a builddict-produced
dictionary round-trips correctly through `zstd -D <dict> -c`/`-d` (both
default `-zcompat=true` format).

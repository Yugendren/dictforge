#!/usr/bin/env python3
"""
fetch_corpora.py -- build the dictforge benchmark corpora.

Five corpora, each split 80/20 train/heldout with a fixed seed (1729):
  github_users : small JSON files from the zstd v1.1.3 release sample set
  gharchive    : per-event JSON files from a GH Archive hour dump
  weblogs      : grouped lines from the NASA HTTP access log (Jul '95)
  apijson      : compact per-work JSON from the Crossref API
  csvrows      : grouped rows (with header) from NYC 311 service requests

Design goals: idempotent (re-running skips already-completed steps) and
deterministic (same seed -> same train/heldout split, given the same raw
input). All corpora are written to corpora/{name}/{train,heldout}/*.json
or *.txt/*.csv depending on the corpus's native format.

Split algorithm (must match exactly for reproducibility):
    names = sorted(os.listdir(raw_dir))
    random.Random(1729).shuffle(names)
    n_train = round(len(names) * 0.8)
    train, heldout = names[:n_train], names[n_train:]
"""

import gzip
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

SEED = 1729
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ZSTD_BIN = ROOT / "third_party" / "zstd" / "programs" / "zstd"
RAW_DIR = HERE / "_raw"
MAILTO = "19thkingisreal@gmail.com"

RAW_DIR.mkdir(parents=True, exist_ok=True)


def log(msg):
    print(f"[fetch_corpora] {msg}", flush=True)


def download(url, dest, headers=None):
    """Download url -> dest, skip if dest already exists and is non-empty."""
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"  already have {dest.name} ({dest.stat().st_size} bytes), skipping download")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"  downloading {url} -> {dest}")
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "dictforge/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.rename(dest)
    log(f"  done: {dest.stat().st_size} bytes")
    return dest


def split_train_heldout(raw_dir, out_dir, seed=SEED, frac_train=0.8):
    """Split files already sitting in raw_dir into out_dir/{train,heldout}."""
    raw_dir = Path(raw_dir)
    out_dir = Path(out_dir)
    train_dir = out_dir / "train"
    heldout_dir = out_dir / "heldout"
    if train_dir.exists() and heldout_dir.exists() and any(train_dir.iterdir()) and any(heldout_dir.iterdir()):
        log(f"  split already exists at {out_dir}, skipping")
        return
    train_dir.mkdir(parents=True, exist_ok=True)
    heldout_dir.mkdir(parents=True, exist_ok=True)

    names = sorted(p.name for p in raw_dir.iterdir() if p.is_file())
    rng = random.Random(seed)
    rng.shuffle(names)
    n_train = round(len(names) * frac_train)
    train_names = names[:n_train]
    heldout_names = names[n_train:]

    for n in train_names:
        shutil.copy2(raw_dir / n, train_dir / n)
    for n in heldout_names:
        shutil.copy2(raw_dir / n, heldout_dir / n)

    log(f"  split {len(names)} files -> {len(train_names)} train / {len(heldout_names)} heldout")


# ---------------------------------------------------------------------------
# a. github_users
# ---------------------------------------------------------------------------

def fetch_github_users():
    name = "github_users"
    log(f"=== {name} ===")
    url = "https://github.com/facebook/zstd/releases/download/v1.1.3/github_users_sample_set.tar.zst"
    archive = RAW_DIR / "github_users_sample_set.tar.zst"
    download(url, archive)

    extract_root = RAW_DIR / "github_users_extracted"
    raw_flat = RAW_DIR / f"{name}_raw"
    if not raw_flat.exists() or not any(raw_flat.iterdir()):
        extract_root.mkdir(parents=True, exist_ok=True)
        # decompress .tar.zst -> .tar using our built zstd, then untar
        tar_path = extract_root / "github_users_sample_set.tar"
        if not tar_path.exists():
            subprocess.run(
                [str(ZSTD_BIN), "-d", "-f", "-q", str(archive), "-o", str(tar_path)],
                check=True,
            )
        subprocess.run(["tar", "-xf", str(tar_path), "-C", str(extract_root)], check=True)

        # Flatten: find all regular files under extract_root (excluding the .tar itself)
        # into raw_flat with unique names.
        raw_flat.mkdir(parents=True, exist_ok=True)
        count = 0
        for dirpath, dirnames, filenames in os.walk(extract_root):
            for fn in filenames:
                if fn == "github_users_sample_set.tar":
                    continue
                src = Path(dirpath) / fn
                dest = raw_flat / fn
                if dest.exists():
                    # disambiguate collisions
                    dest = raw_flat / f"{Path(dirpath).name}_{fn}"
                shutil.copy2(src, dest)
                count += 1
        log(f"  flattened {count} files into {raw_flat}")

    split_train_heldout(raw_flat, HERE / name)


# ---------------------------------------------------------------------------
# b. gharchive
# ---------------------------------------------------------------------------

def fetch_gharchive():
    name = "gharchive"
    log(f"=== {name} ===")
    url = "https://data.gharchive.org/2024-01-01-15.json.gz"
    archive = RAW_DIR / "2024-01-01-15.json.gz"
    download(url, archive)

    raw_flat = RAW_DIR / f"{name}_raw"
    if not raw_flat.exists() or not any(raw_flat.iterdir()):
        raw_flat.mkdir(parents=True, exist_ok=True)
        limit = 20000
        count = 0
        with gzip.open(archive, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # validate it's parseable JSON before writing (gharchive is
                # newline-delimited JSON, one event per line)
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    continue
                fname = f"event_{count:06d}.json"
                (raw_flat / fname).write_text(line + "\n", encoding="utf-8")
                count += 1
                if count >= limit:
                    break
        log(f"  wrote {count} event files")

    split_train_heldout(raw_flat, HERE / name)


# ---------------------------------------------------------------------------
# c. weblogs
# ---------------------------------------------------------------------------

def fetch_weblogs():
    name = "weblogs"
    log(f"=== {name} ===")
    url = "https://ita.ee.lbl.gov/traces/NASA_access_log_Jul95.gz"
    archive = RAW_DIR / "NASA_access_log_Jul95.gz"
    download(url, archive)

    raw_flat = RAW_DIR / f"{name}_raw"
    if not raw_flat.exists() or not any(raw_flat.iterdir()):
        raw_flat.mkdir(parents=True, exist_ok=True)
        max_lines = 300000
        group = 20
        lines_buf = []
        file_idx = 0
        n_lines = 0
        with gzip.open(archive, "rt", encoding="latin-1", errors="replace") as f:
            for line in f:
                lines_buf.append(line)
                n_lines += 1
                if len(lines_buf) == group:
                    fname = f"chunk_{file_idx:06d}.log"
                    (raw_flat / fname).write_text("".join(lines_buf), encoding="utf-8")
                    file_idx += 1
                    lines_buf = []
                if n_lines >= max_lines:
                    break
        if lines_buf:
            fname = f"chunk_{file_idx:06d}.log"
            (raw_flat / fname).write_text("".join(lines_buf), encoding="utf-8")
            file_idx += 1
        log(f"  wrote {file_idx} chunk files from {n_lines} lines")

    split_train_heldout(raw_flat, HERE / name)


# ---------------------------------------------------------------------------
# d. apijson (Crossref)
# ---------------------------------------------------------------------------

def fetch_apijson():
    name = "apijson"
    log(f"=== {name} ===")
    select = ("DOI,title,author,published,type,publisher,container-title,volume,"
              "issue,page,ISSN,subject,is-referenced-by-count,abstract,funder,license,link")
    raw_pages = RAW_DIR / f"{name}_pages"
    raw_pages.mkdir(parents=True, exist_ok=True)

    for offset in range(0, 10000, 1000):
        page_file = raw_pages / f"page_{offset:05d}.json"
        if page_file.exists() and page_file.stat().st_size > 0:
            log(f"  page offset={offset} already fetched, skipping")
            continue
        url = (f"https://api.crossref.org/works?rows=1000&offset={offset}"
               f"&select={select}&mailto={MAILTO}")
        log(f"  fetching offset={offset}")
        req = urllib.request.Request(url, headers={"User-Agent": f"dictforge/1.0 (mailto:{MAILTO})"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            page_file.write_bytes(data)
        except urllib.error.HTTPError as e:
            log(f"  WARNING: offset={offset} failed: {e}")
        time.sleep(1.0)  # be polite: 1 req/s

    raw_flat = RAW_DIR / f"{name}_raw"
    if not raw_flat.exists() or not any(raw_flat.iterdir()):
        raw_flat.mkdir(parents=True, exist_ok=True)
        seen_dois = set()
        count = 0
        for page_file in sorted(raw_pages.glob("page_*.json")):
            try:
                obj = json.loads(page_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log(f"  WARNING: {page_file} is not valid JSON, skipping")
                continue
            items = obj.get("message", {}).get("items", [])
            for item in items:
                doi = item.get("DOI")
                if not doi or doi in seen_dois:
                    continue
                seen_dois.add(doi)
                fname = f"work_{count:06d}.json"
                (raw_flat / fname).write_text(
                    json.dumps(item, separators=(",", ":"), ensure_ascii=False),
                    encoding="utf-8",
                )
                count += 1
        log(f"  wrote {count} deduped work files (by DOI)")

    split_train_heldout(raw_flat, HERE / name)


# ---------------------------------------------------------------------------
# e. csvrows (NYC 311)
# ---------------------------------------------------------------------------

def fetch_csvrows():
    name = "csvrows"
    log(f"=== {name} ===")
    raw_pages = RAW_DIR / f"{name}_pages"
    raw_pages.mkdir(parents=True, exist_ok=True)

    header_line = None
    for offset in range(0, 500000, 50000):
        page_file = raw_pages / f"page_{offset:06d}.csv"
        if page_file.exists() and page_file.stat().st_size > 0:
            log(f"  page offset={offset} already fetched, skipping")
            continue
        url = (f"https://data.cityofnewyork.us/resource/erm2-nwe9.csv"
               f"?$limit=50000&$offset={offset}&$order=unique_key")
        log(f"  fetching offset={offset}")
        req = urllib.request.Request(url, headers={"User-Agent": "dictforge/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
            page_file.write_bytes(data)
        except urllib.error.HTTPError as e:
            log(f"  WARNING: offset={offset} failed: {e}")
        time.sleep(0.5)

    raw_flat = RAW_DIR / f"{name}_raw"
    if not raw_flat.exists() or not any(raw_flat.iterdir()):
        raw_flat.mkdir(parents=True, exist_ok=True)
        seen_keys = set()
        rows = []  # list of raw CSV lines (excluding header), deduped
        header = None
        for page_file in sorted(raw_pages.glob("page_*.csv")):
            text = page_file.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            if not lines:
                continue
            if header is None:
                header = lines[0]
            for line in lines[1:]:
                if not line.strip():
                    continue
                key = line.split(",", 1)[0]
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                rows.append(line)
        log(f"  {len(rows)} deduped rows (by unique_key) across pages")

        group = 50
        file_idx = 0
        for i in range(0, len(rows), group):
            chunk = rows[i:i + group]
            fname = f"rows_{file_idx:06d}.csv"
            content = header + "\n" + "\n".join(chunk) + "\n"
            (raw_flat / fname).write_text(content, encoding="utf-8")
            file_idx += 1
        log(f"  wrote {file_idx} grouped csv files")

    split_train_heldout(raw_flat, HERE / name)


# ---------------------------------------------------------------------------

CORPORA = {
    "github_users": fetch_github_users,
    "gharchive": fetch_gharchive,
    "weblogs": fetch_weblogs,
    "apijson": fetch_apijson,
    "csvrows": fetch_csvrows,
}


def main():
    args = sys.argv[1:]
    targets = args if args else list(CORPORA.keys())
    for t in targets:
        if t not in CORPORA:
            log(f"unknown corpus '{t}', valid: {list(CORPORA.keys())}")
            sys.exit(1)
    for t in targets:
        CORPORA[t]()
    log("all requested corpora complete")


if __name__ == "__main__":
    main()

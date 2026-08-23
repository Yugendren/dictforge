#!/usr/bin/env python3
"""
make_figures.py -- generate dictforge paper figures from a benchmark CSV.

Reads results/benchmark_v2.csv (schema: experiment,corpus,level,variant,
dict_size,ratio,train_wall_s,note) and emits paper/figures/*.pdf + *.png:

  F1 cliff        -- E1 candidate rows only: requested size (x, log2) vs
                      emitted dict_size and vs ratio (twin y-axes), one
                      panel per corpus, marking rows whose note contains
                      "degenerate". Corpora with no E1 candidate rows are
                      skipped.
  F2 size_curves  -- ratio vs dict_size, one panel per (corpus, level),
                      scatter + a connecting line for any variant family
                      that forms a size ladder (>=2 points), with the
                      110KB (112640B) default marked by a vertical line.
  F3 pareto       -- ratio vs dict_size scatter labelled by variant, one
                      panel per (corpus, level), with the Pareto frontier
                      (max ratio achievable at or below a given size)
                      highlighted.
  F4 gains        -- grouped bar chart of % improvement of "full" and
                      "tuned" over "default", per corpus, one subplot per
                      level.

This script must never crash on a partial CSV: any figure or panel that's
missing the rows it needs is skipped with a printed warning, and the
other figures/panels still render.

Usage:
    python3 tools/make_figures.py [--csv results/benchmark_v2.csv]
                                   [--out-dir paper/figures]
"""
import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# A candidate counts as degenerate when it emitted materially less than the
# budget requested; same threshold the trainer uses for its own guard.
DEGENERACY_FRAC = 0.90

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_CSV = REPO_ROOT / "results" / "benchmark_v2.csv"
DEFAULT_OUT_DIR = REPO_ROOT / "paper" / "figures"

DEFAULT_DICT_BYTES = 112640  # 110KB, the zstd CLI / RocksDB / ScyllaDB default

# stock matplotlib qualitative palette; extended with tab20b/tab20c so we
# don't run out of distinct colors even with many variant families
_PALETTE = (
    list(plt.get_cmap("tab20").colors)
    + list(plt.get_cmap("tab20b").colors)
    + list(plt.get_cmap("tab20c").colors)
)


def warn(msg):
    print(f"[make_figures] WARNING: {msg}", file=sys.stderr)


def info(msg):
    print(f"[make_figures] {msg}")


# ---------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------

def load_rows(csv_path):
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            try:
                rows.append({
                    "experiment": raw["experiment"],
                    "corpus": raw["corpus"],
                    "level": int(raw["level"]),
                    "variant": raw["variant"],
                    "dict_size": int(raw["dict_size"]),
                    "ratio": float(raw["ratio"]),
                    "train_wall_s": float(raw["train_wall_s"]) if raw["train_wall_s"] else 0.0,
                    "note": raw.get("note") or "",
                })
            except (KeyError, ValueError) as e:
                warn(f"skipping malformed row {raw!r}: {e}")
    return rows


_TRAILING_SIZE_RE = re.compile(r"_(\d+)$")


def variant_requested_size(variant):
    """For variants like 'cover_16384' or 'equalcompute_cover_cand_16384',
    return the trailing integer (the requested/candidate size). None if
    the variant has no trailing _<digits> (e.g. 'winner', 'default')."""
    m = _TRAILING_SIZE_RE.search(variant)
    return int(m.group(1)) if m else None


def variant_family(variant):
    """Canonical family name for color grouping: strips a trailing
    _<digits> suffix so a size ladder (cover_16384, cover_65536, ...)
    shares one color."""
    return _TRAILING_SIZE_RE.sub("", variant) or variant


def build_color_map(rows):
    """Deterministic variant-family -> color mapping, stable across all
    figures in a single run (built once from the full row set)."""
    families = sorted({variant_family(r["variant"]) for r in rows})
    cmap = {}
    for i, fam in enumerate(families):
        cmap[fam] = _PALETTE[i % len(_PALETTE)]
    return cmap


def color_for(variant, color_map):
    return color_map.get(variant_family(variant), "0.5")


LEVEL_LINESTYLES = {}
_LINESTYLE_CYCLE = ["-", "--", ":", "-."]


def linestyle_for(level):
    if level not in LEVEL_LINESTYLES:
        LEVEL_LINESTYLES[level] = _LINESTYLE_CYCLE[len(LEVEL_LINESTYLES) % len(_LINESTYLE_CYCLE)]
    return LEVEL_LINESTYLES[level]


def savefig(fig, out_dir, name):
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{name}.pdf"
    png_path = out_dir / f"{name}.png"
    fig.savefig(pdf_path)
    fig.savefig(png_path, dpi=150)
    plt.close(fig)
    info(f"wrote {pdf_path}")
    info(f"wrote {png_path}")
    return [pdf_path, png_path]


# ---------------------------------------------------------------------
# F1: cliff plot (E1 equal-compute candidate sweep)
# ---------------------------------------------------------------------

def make_f1_cliff(rows, out_dir, color_map):
    written = []
    e1_rows = [r for r in rows if r["experiment"] == "E1" and r["variant"] != "winner"]
    e1_rows = [r for r in e1_rows if variant_requested_size(r["variant"]) is not None]
    if not e1_rows:
        warn("F1 cliff: no E1 candidate rows found (need experiment=='E1' rows "
             "with a trailing _<size> in variant); skipping figure entirely")
        return written

    corpora = sorted({r["corpus"] for r in e1_rows})
    fig, axes = plt.subplots(1, len(corpora), figsize=(6 * len(corpora), 4.5), squeeze=False)
    axes = axes[0]

    for ax, corpus in zip(axes, corpora):
        crows = [r for r in e1_rows if r["corpus"] == corpus]
        levels = sorted({r["level"] for r in crows})
        ax2 = ax.twinx()
        family = variant_family(crows[0]["variant"])
        base_color = color_for(crows[0]["variant"], color_map)

        # The candidate dictionaries are trained once and merely *evaluated* at
        # each level, so emitted size is level-independent: plot it once, and
        # one ratio curve per evaluation level.
        emitted_drawn = False
        degenerate_drawn = False
        for level in levels:
            lrows = sorted((r for r in crows if r["level"] == level),
                            key=lambda r: variant_requested_size(r["variant"]))
            req = [variant_requested_size(r["variant"]) for r in lrows]
            emitted = [r["dict_size"] for r in lrows]
            ratio = [r["ratio"] for r in lrows]
            ls = linestyle_for(level)

            if not emitted_drawn:
                ax.plot(req, emitted, color=base_color, linestyle="-", marker="o",
                        label="emitted dict_size")
                emitted_drawn = True
            ax2.plot(req, ratio, color=base_color, linestyle=ls, marker="x",
                      alpha=0.55, label=f"ratio (L{level})")

            # Degeneracy is derived from the measurements, not from note text:
            # a build is degenerate when it emitted materially less than the
            # budget it was asked for. (The runner records both numbers but
            # does not annotate the note, so trusting the note under-reports.)
            degenerate = [(r_, e_) for r_, e_ in zip(req, emitted)
                          if r_ and e_ < DEGENERACY_FRAC * r_]
            if degenerate and not degenerate_drawn:
                dx, dy = zip(*degenerate)
                ax.scatter(dx, dy, facecolors="none", edgecolors="red", s=140,
                           linewidths=1.6, zorder=5,
                           label=f"degenerate (emitted < {DEGENERACY_FRAC:.0%} of request)")
                degenerate_drawn = True

        ax.set_xscale("log", base=2)
        ax.set_xlabel("requested dictionary budget (bytes, log2)")
        ax.set_ylabel(f"emitted dict_size (bytes) [{family}]")
        ax2.set_ylabel("compression ratio")
        ax.set_title(f"{corpus}")
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="best")
        ax.grid(True, which="both", alpha=0.25)

    fig.suptitle("F1: budget-degeneracy cliff (equal-compute candidate sweep)")
    fig.tight_layout()
    written += savefig(fig, out_dir, "f1_cliff")
    return written


# ---------------------------------------------------------------------
# F2: size curves
# ---------------------------------------------------------------------

def make_f2_size_curves(rows, out_dir, color_map):
    written = []
    plot_rows = [r for r in rows if r["dict_size"] > 0]
    if not plot_rows:
        warn("F2 size_curves: no rows with dict_size > 0; skipping figure entirely")
        return written

    corpora = sorted({r["corpus"] for r in plot_rows})
    levels = sorted({r["level"] for r in plot_rows})
    fig, axes = plt.subplots(len(corpora), len(levels),
                              figsize=(5.5 * len(levels), 4 * len(corpora)),
                              squeeze=False)

    any_panel_drawn = False
    for i, corpus in enumerate(corpora):
        for j, level in enumerate(levels):
            ax = axes[i][j]
            cell_rows = [r for r in plot_rows if r["corpus"] == corpus and r["level"] == level]
            if not cell_rows:
                warn(f"F2 size_curves: no data for corpus={corpus} level={level}; "
                     f"leaving panel blank")
                ax.text(0.5, 0.5, "no data", ha="center", va="center",
                        transform=ax.transAxes, color="0.6")
                ax.set_title(f"{corpus} L{level}")
                continue

            any_panel_drawn = True
            by_family = defaultdict(list)
            for r in cell_rows:
                by_family[variant_family(r["variant"])].append(r)

            for fam, frows in sorted(by_family.items()):
                col = color_map.get(fam, "0.5")
                frows_sorted = sorted(frows, key=lambda r: r["dict_size"])
                xs = [r["dict_size"] for r in frows_sorted]
                ys = [r["ratio"] for r in frows_sorted]
                ax.scatter(xs, ys, color=col, s=28, label=fam, zorder=3)
                if len(frows_sorted) >= 2:
                    ax.plot(xs, ys, color=col, linestyle="-", alpha=0.7, zorder=2)

            ax.axvline(DEFAULT_DICT_BYTES, color="black", linestyle=":", linewidth=1,
                       label="110KB default")
            ax.set_xscale("log")
            ax.set_xlabel("dict_size (bytes, log)")
            ax.set_ylabel("compression ratio")
            ax.set_title(f"{corpus} L{level}")
            ax.legend(fontsize=6, loc="best")
            ax.grid(True, which="both", alpha=0.25)

    if not any_panel_drawn:
        warn("F2 size_curves: every (corpus, level) panel was empty; skipping figure entirely")
        plt.close(fig)
        return written

    fig.suptitle("F2: compression ratio vs dictionary size")
    fig.tight_layout()
    written += savefig(fig, out_dir, "f2_size_curves")
    return written


# ---------------------------------------------------------------------
# F3: Pareto scatter
# ---------------------------------------------------------------------

def pareto_frontier(points):
    """points: list of (dict_size, ratio, label). Returns the subsequence
    (sorted by dict_size ascending) of points not dominated by any other
    point (dominated = another point has size <= and ratio >= , with at
    least one strict)."""
    pts_sorted = sorted(points, key=lambda p: p[0])
    frontier = []
    best_ratio = float("-inf")
    for p in pts_sorted:
        if p[1] > best_ratio:
            frontier.append(p)
            best_ratio = p[1]
    return frontier


def make_f3_pareto(rows, out_dir, color_map):
    written = []
    plot_rows = [r for r in rows if r["dict_size"] > 0]
    if not plot_rows:
        warn("F3 pareto: no rows with dict_size > 0; skipping figure entirely")
        return written

    corpora = sorted({r["corpus"] for r in plot_rows})
    levels = sorted({r["level"] for r in plot_rows})
    fig, axes = plt.subplots(len(corpora), len(levels),
                              figsize=(5.5 * len(levels), 4.2 * len(corpora)),
                              squeeze=False)

    any_panel_drawn = False
    for i, corpus in enumerate(corpora):
        for j, level in enumerate(levels):
            ax = axes[i][j]
            cell_rows = [r for r in plot_rows if r["corpus"] == corpus and r["level"] == level]
            if not cell_rows:
                warn(f"F3 pareto: no data for corpus={corpus} level={level}; leaving panel blank")
                ax.text(0.5, 0.5, "no data", ha="center", va="center",
                        transform=ax.transAxes, color="0.6")
                ax.set_title(f"{corpus} L{level}")
                continue

            any_panel_drawn = True
            points = [(r["dict_size"], r["ratio"], r["variant"]) for r in cell_rows]
            # stagger label offsets in a small cycle so points that land
            # close together (common near the frontier's cheap end) don't
            # print their labels directly on top of each other
            offset_cycle = [(4, 4), (4, -11), (-6, 8), (-6, -13)]
            for k, (x, y, label) in enumerate(sorted(points, key=lambda p: p[0])):
                col = color_for(label, color_map)
                ax.scatter([x], [y], color=col, s=24, alpha=0.75, zorder=2)
                dx, dy = offset_cycle[k % len(offset_cycle)]
                ha = "left" if dx >= 0 else "right"
                ax.annotate(label, (x, y), fontsize=5, alpha=0.85,
                            xytext=(dx, dy), textcoords="offset points", ha=ha)

            frontier = pareto_frontier(points)
            if len(frontier) >= 1:
                fx = [p[0] for p in frontier]
                fy = [p[1] for p in frontier]
                ax.plot(fx, fy, color="black", linestyle="--", linewidth=1.3,
                         marker="D", markersize=5, zorder=4, label="Pareto frontier")

            ax.set_xscale("log")
            ax.set_xlabel("dict_size (bytes, log)")
            ax.set_ylabel("compression ratio")
            ax.set_title(f"{corpus} L{level}")
            ax.legend(fontsize=6, loc="best")
            ax.grid(True, which="both", alpha=0.25)

    if not any_panel_drawn:
        warn("F3 pareto: every (corpus, level) panel was empty; skipping figure entirely")
        plt.close(fig)
        return written

    fig.suptitle("F3: Pareto frontier (ratio vs dictionary size)")
    fig.tight_layout()
    written += savefig(fig, out_dir, "f3_pareto")
    return written


# ---------------------------------------------------------------------
# F4: gains bar chart
# ---------------------------------------------------------------------

def make_f4_gains(rows, out_dir, color_map):
    written = []
    main_rows = [r for r in rows if r["experiment"] == "MAIN"]
    if not main_rows:
        warn("F4 gains: no MAIN experiment rows found; skipping figure entirely")
        return written

    # index[(corpus, level, variant)] = ratio
    index = {}
    for r in main_rows:
        index[(r["corpus"], r["level"], r["variant"])] = r["ratio"]

    corpora = sorted({r["corpus"] for r in main_rows})
    levels = sorted({r["level"] for r in main_rows})
    tuned_variants = ["tuned", "full"]

    fig, axes = plt.subplots(1, len(levels), figsize=(5.5 * len(levels), 4.5), squeeze=False)
    axes = axes[0]
    any_bar_drawn = False

    for ax, level in zip(axes, levels):
        bar_labels = []
        group_x = []
        n_bars_per_group = len(tuned_variants)
        width = 0.8 / n_bars_per_group
        x0 = 0

        for corpus in corpora:
            default_ratio = index.get((corpus, level, "default"))
            if default_ratio is None:
                warn(f"F4 gains: no 'default' MAIN row for corpus={corpus} level={level}; "
                     f"skipping this corpus/level group")
                continue
            drew_any_for_group = False
            for k, variant in enumerate(tuned_variants):
                ratio = index.get((corpus, level, variant))
                if ratio is None:
                    warn(f"F4 gains: no '{variant}' MAIN row for corpus={corpus} level={level}; "
                         f"skipping this bar")
                    continue
                pct = (ratio - default_ratio) / default_ratio * 100.0
                xpos = x0 + k * width
                ax.bar(xpos, pct, width=width * 0.9, color=color_map.get(variant, "0.5"),
                       label=variant if x0 == 0 else None)
                drew_any_for_group = True
                any_bar_drawn = True
            if drew_any_for_group:
                bar_labels.append(corpus)
                group_x.append(x0 + width * (n_bars_per_group - 1) / 2)
                x0 += 1

        ax.set_xticks(group_x)
        ax.set_xticklabels(bar_labels, rotation=20, ha="right")
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_ylabel("% ratio improvement over default")
        ax.set_title(f"level {level}")
        ax.grid(True, axis="y", alpha=0.25)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(fontsize=8, loc="best")

    if not any_bar_drawn:
        warn("F4 gains: no (corpus, level) had both default and a tuned/full variant; "
             "skipping figure entirely")
        plt.close(fig)
        return written

    fig.suptitle("F4: % ratio improvement of tuned/full over default")
    fig.tight_layout()
    written += savefig(fig, out_dir, "f4_gains")
    return written


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = ap.parse_args()

    csv_path = Path(args.csv)
    out_dir = Path(args.out_dir)

    if not csv_path.exists():
        sys.exit(f"make_figures.py: CSV not found: {csv_path}")

    rows = load_rows(csv_path)
    info(f"loaded {len(rows)} rows from {csv_path}")
    if not rows:
        sys.exit("make_figures.py: CSV loaded but contains no usable rows")

    color_map = build_color_map(rows)

    written = []
    figure_makers = [
        ("F1 cliff", make_f1_cliff),
        ("F2 size_curves", make_f2_size_curves),
        ("F3 pareto", make_f3_pareto),
        ("F4 gains", make_f4_gains),
    ]
    for name, fn in figure_makers:
        try:
            written += fn(rows, out_dir, color_map)
        except Exception as e:  # noqa: BLE001 -- never let one figure kill the run
            warn(f"{name}: unexpected error, skipping figure entirely: {e!r}")

    info(f"done: {len(written)} file(s) written to {out_dir}")
    for p in written:
        print(f"  {p}")


if __name__ == "__main__":
    main()

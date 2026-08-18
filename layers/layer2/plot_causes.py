#!/usr/bin/env python3
"""
plot_causes.py -- a more readable version of the cause histogram.

Three problems with a plain linear bar chart of cause counts:

  1. LINEAR SCALE HIDES THE TAIL. const.absent is ~30% of every diff, so on a linear
     axis everything below ~1% flattens to zero. But that flattened tail is where the
     causes you can actually act on live: `tactic.absent`, `attr.absent`,
     `syntax.token-absent`, `option.absent`. They are rare, cheap to fix, and each one
     is near-certain to bite if you touch the relevant feature.

  2. LOUD AND SILENT CAUSES LOOK THE SAME. Some causes make the compiler stop with a
     location. Others produce no diagnostic at all -- the code still builds, and behaves
     differently. Those deserve separate colour, not a shared axis.

  3. RAW COUNTS MEASURE THE ENVIRONMENT, NOT YOUR EXPOSURE. `const.absent` in the
     tens of thousands is mostly declarations you have never referenced.

Usage:
    python plot_causes.py causes/4.16.0__4.20.0.ndjson
    python plot_causes.py causes/*.ndjson              # aggregates, see warning below

WARNING when passing several files: only aggregate rows of the same kind. Summing an
adjacent hop together with a span that covers it double-counts. See chain.csv's `kind`
column.
"""

import sys, os, collections
import matplotlib
matplotlib.use("Agg")                      # headless: write a file, don't open a window
import matplotlib.pyplot as plt

# The closed cause set, in the same order as EnvDiff.lean's `allCauses`.
ALL = ['const.absent','const.renamed','const.kind-changed','const.arity-changed',
 'const.binders-changed','const.universes-changed','const.type-changed','const.class-changed',
 'const.reducibility-changed','const.protected-changed','const.fields-changed',
 'const.ctors-changed','instance.absent','instance.priority-changed',
 'deprecation.alias-absent','tactic.absent','syntax.category-absent','syntax.token-absent',
 'syntax.kind-absent','token.absent','attr.absent','option.absent','option.default-changed',
 'simp.lemma-absent','simp.unfold-absent']

# Causes that produce NO compiler error. The build succeeds; behaviour differs.
SILENT = {'option.default-changed','const.reducibility-changed','instance.priority-changed',
          'simp.lemma-absent','simp.unfold-absent'}


def histogram(paths):
    """Count `sev=break` records per cause across one or more .ndjson cause files."""
    counts = collections.Counter()
    for path in paths:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                if '"sev":"break"' not in line:
                    continue
                # Json.mkObj emits keys alphabetically, so "cause" is always first.
                i = line.index('"cause":"') + 9
                counts[line[i:line.index('"', i)]] += 1
    return counts


def plot(counts, out_png, title):
    names = ALL                                   # fixed order => charts are comparable
    vals = [counts.get(c, 0) for c in names]
    colors = ['#d1495b' if c in SILENT else '#4c9f9f' for c in names]

    fig, ax = plt.subplots(figsize=(13, 6.5))
    bars = ax.bar(range(len(names)), vals, color=colors)

    # Log scale so three orders of magnitude are all legible at once. Zero can't be
    # plotted on a log axis, so bottom is clamped just under 1 and zeros stay flat.
    ax.set_yscale('log')
    ax.set_ylim(bottom=0.7)
    ax.set_ylabel('Count (log scale)')
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=45, ha='right', fontsize=9)

    # Print the actual number above each bar: on a log axis, eyeballing heights is unreliable.
    for b, v in zip(bars, vals):
        if v > 0:
            ax.text(b.get_x() + b.get_width() / 2, v * 1.15, str(v),
                    ha='center', va='bottom', fontsize=7.5)

    loud_total = sum(v for c, v in zip(names, vals) if c not in SILENT)
    silent_total = sum(v for c, v in zip(names, vals) if c in SILENT)
    ax.set_title(f"{title}\n"
                 f"{loud_total + silent_total} breaks: "
                 f"{loud_total} compiler-visible, {silent_total} silent")

    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color='#4c9f9f', label='Compiler visible'),
                       Patch(color='#d1495b', label='Silent')],
              loc='upper right')
    ax.grid(axis='y', alpha=0.3, which='both')
    fig.tight_layout()
    fig.savefig(out_png, dpi=150)
    print(f"wrote {out_png}")


if __name__ == '__main__':
    paths = sys.argv[1:]
    if not paths:
        sys.exit(__doc__)
    if len(paths) > 1:
        print(f"NOTE: aggregating {len(paths)} files -- make sure they are all the same "
              f"`kind` (all hops, or all spans), or the totals double-count.")
    counts = histogram(paths)
    label = os.path.basename(paths[0]) if len(paths) == 1 else f"{len(paths)} cause files"
    plot(counts, 'causes.png', f"Break causes: {label}")

    # Ranked text summary -- often more actionable than the chart.
    total = sum(counts.values())
    print(f"\n{'cause':32}{'count':>8}{'share':>8}  kind")
    for c, n in counts.most_common():
        tag = 'SILENT' if c in SILENT else 'loud'
        print(f"{c:32}{n:>8}{n / total:>7.1%}  {tag}")

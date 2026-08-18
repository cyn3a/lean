"""Layer 4 CLI.

    python -m layer4 discover --repo ./mathlib4 -o out/windows.jsonl
    python -m layer4 mine     --repo ./mathlib4 -w out/windows.jsonl -o out/
    python -m layer4 rules    --pairs out/pairs.jsonl -o out/rules.jsonl
    python -m layer4 synth    --repo ./mathlib4 --rules out/rules.jsonl -n 2000
    python -m layer4 replay   --repo ./mathlib4 --pairs out/pairs.jsonl --limit 200
    python -m layer4 stats    --pairs out/pairs.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import discover as disc
from . import emit, mine as mining, rules as ruleslib
from .discover import Window
from .gitio import Git, clone


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _git(args) -> Git:
    if args.clone_url:
        _log(f"cloning {args.clone_url} -> {args.repo} (blob:none)")
        return clone(args.clone_url, args.repo, branch=args.mainline)
    g = Git(args.repo)
    for w in g.check_clone_health():
        _log(f"warning: {w}")
    return g


# ------------------------------------------------------------------ commands

def cmd_discover(args) -> int:
    git = _git(args)
    windows = disc.discover(
        git, args.mainline, modes=tuple(args.modes), limit=args.limit,
        include_dep_adaptations=not args.no_dep_adaptations)
    if args.kind:
        windows = [w for w in windows if w.kind in args.kind]
    n = emit.write_jsonl([w.to_json() for w in windows], args.out)
    _log(f"discovered {n} windows -> {args.out}")
    for w in windows[:15]:
        _log(f"  {w.date or '----------'}  {w.kind:16s} {w.key:24s} {w.source}")
    if len(windows) > 15:
        _log(f"  ... and {len(windows) - 15} more")
    return 0


def cmd_mine(args) -> int:
    git = _git(args)
    if args.windows:
        windows = [Window(**{k: v for k, v in row.items()})
                   for row in emit.read_jsonl(args.windows)]
    else:
        windows = disc.discover(git, args.mainline, modes=tuple(args.modes),
                                limit=args.limit)
    if args.max_windows:
        windows = windows[:args.max_windows]
    _log(f"mining {len(windows)} windows")

    pairs = mining.mine(
        git, windows, progress=_log, context=args.context,
        keep_noise=args.keep_noise, min_confidence=args.min_confidence,
        max_hunk_lines=args.max_hunk_lines)
    rows = [p.to_json() for p in pairs]
    _log(f"raw pairs: {len(rows)}")

    rows, _counts = emit.dedup(rows, keep_per_signature=args.keep_per_signature)
    _log(f"after dedup: {len(rows)}")

    rows = emit.rebalance(rows, cap_per_label=args.cap_per_label,
                          cap_per_signature=args.cap_per_signature, seed=args.seed)
    _log(f"after rebalance: {len(rows)}")

    outdir = Path(args.out)
    emit.write_jsonl(rows, outdir / "pairs.jsonl")
    report = emit.label_report(rows)
    if args.split:
        buckets, used = emit.split(rows, by=args.split_by, seed=args.seed)
        if used != args.split_by:
            _log(f"warning: --split-by {args.split_by} yielded <3 groups; "
                 f"fell back to {used}. Mine more windows for a "
                 f"forward-in-time toolchain split.")
        args.split_by = used
        for name, rs in buckets.items():
            emit.write_jsonl(rs, outdir / f"{name}.jsonl")
        report["splits"] = {k: len(v) for k, v in buckets.items()}
        report["split_by"] = args.split_by
    emit.write_manifest(outdir / "manifest.json",
                        windows=[w.to_json() for w in windows],
                        config={k: v for k, v in vars(args).items()
                                if isinstance(v, (str, int, float, bool, type(None), list))},
                        report=report)
    _log(json.dumps({k: report[k] for k in
                     ("total", "distinct_labels", "top_label_share",
                      "distinct_files", "distinct_decls") if k in report}, indent=2))
    for row in report["by_label"][:12]:
        _log(f"  {row['pct']:6.2f}%  {row['n']:6d}  {row['label']}")
    return 0


def cmd_rules(args) -> int:
    pairs = emit.read_jsonl(args.pairs)
    rs = ruleslib.induce(pairs, min_support=args.min_support)
    ruleslib.save_rules(rs, args.out)
    _log(f"induced {len(rs)} rules -> {args.out}")
    for r in rs[:15]:
        desc = (f"{r.forward.get('from')} -> {r.forward.get('to')}"
                if r.kind == "substitution" else "<window>")
        _log(f"  support={r.support:5d} files={r.files:4d} {r.label:28s} {desc}")
    return 0


def cmd_synth(args) -> int:
    git = _git(args)
    rs = ruleslib.load_rules(args.rules)
    tip = git.rev_parse(args.at or args.mainline)
    listing = git.run("ls-tree", "-r", "--name-only", tip).split("\n")
    lean_files = [p for p in listing
                  if p.endswith(".lean") and p.startswith(tuple(args.prefix))]
    import random
    random.Random(args.seed).shuffle(lean_files)
    lean_files = lean_files[:args.scan_files]
    _log(f"scanning {len(lean_files)} files at {tip[:10]}")

    contents = {}
    for p in lean_files:
        txt = git.file_at(tip, p)
        if txt:
            contents[p] = txt
    samples = ruleslib.synthesize(
        rs, contents, n=args.n, max_per_rule=args.max_per_rule,
        sites_per_file=args.sites_per_file, seed=args.seed,
        toolchain=(git.file_at(tip, "lean-toolchain") or "").strip())
    emit.write_jsonl(samples, args.out)
    _log(f"synthesised {len(samples)} breakages -> {args.out}")
    return 0


def cmd_replay(args) -> int:
    from . import replay as rp
    pairs = emit.read_jsonl(args.pairs)
    if args.label:
        pairs = [p for p in pairs if p.get("label") in args.label]
    results = rp.replay(Path(args.repo), pairs, limit=args.limit,
                        progress=_log, build_deps=not args.no_build,
                        timeout=args.timeout)
    emit.write_jsonl([r.to_json() for r in results], args.out)
    cal = rp.calibration(results, pairs)
    emit.write_manifest(Path(args.out).with_name("calibration.json"), calibration=cal)
    _log(json.dumps(cal, indent=2))
    return 0


def cmd_stats(args) -> int:
    pairs = emit.read_jsonl(args.pairs)
    print(json.dumps(emit.label_report(pairs), indent=2))
    return 0


# -------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="layer4", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--repo", default="./mathlib4", help="path to clone")
        sp.add_argument("--clone-url", default=None,
                        help="clone this URL into --repo first")
        sp.add_argument("--mainline", default="master")
        return sp

    d = common(sub.add_parser("discover", help="find adaptation windows"))
    d.add_argument("--modes", nargs="+", default=["squash", "branch"],
                   choices=["squash", "toolchain", "branch", "merge"])
    d.add_argument("--kind", nargs="*", default=None)
    d.add_argument("--limit", type=int, default=None, help="max commits to scan")
    d.add_argument("--no-dep-adaptations", action="store_true")
    d.add_argument("-o", "--out", default="out/windows.jsonl")
    d.set_defaults(func=cmd_discover)

    m = common(sub.add_parser("mine", help="windows -> labelled repair pairs"))
    m.add_argument("-w", "--windows", default=None)
    m.add_argument("--modes", nargs="+", default=["squash", "branch"])
    m.add_argument("--limit", type=int, default=None)
    m.add_argument("--max-windows", type=int, default=None)
    m.add_argument("--context", type=int, default=6)
    m.add_argument("--max-hunk-lines", type=int, default=60)
    m.add_argument("--min-confidence", type=float, default=0.0)
    m.add_argument("--keep-noise", action="store_true",
                   help="retain formatting/comment-only hunks")
    m.add_argument("--cap-per-label", type=int, default=2000,
                   help="0/None to disable; guards against class collapse")
    m.add_argument("--keep-per-signature", type=int, default=1,
                   help="contextually distinct copies to retain per edit shape")
    m.add_argument("--cap-per-signature", type=int, default=8)
    m.add_argument("--split", action="store_true", default=True)
    m.add_argument("--no-split", dest="split", action="store_false")
    m.add_argument("--split-by", default="toolchain",
                   choices=sorted(emit.SPLIT_KEYS))
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("-o", "--out", default="out")
    m.set_defaults(func=cmd_mine)

    r = sub.add_parser("rules", help="induce reversible breakage rules")
    r.add_argument("--pairs", default="out/pairs.jsonl")
    r.add_argument("--min-support", type=int, default=2)
    r.add_argument("-o", "--out", default="out/rules.jsonl")
    r.set_defaults(func=cmd_rules)

    s = common(sub.add_parser("synth", help="apply reversed rules to clean code"))
    s.add_argument("--rules", default="out/rules.jsonl")
    s.add_argument("--at", default=None, help="commit to break (default: mainline)")
    s.add_argument("--prefix", nargs="+", default=["Mathlib/"])
    s.add_argument("-n", type=int, default=1000)
    s.add_argument("--max-per-rule", type=int, default=20)
    s.add_argument("--sites-per-file", type=int, default=1)
    s.add_argument("--scan-files", type=int, default=1500)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("-o", "--out", default="out/synthetic.jsonl")
    s.set_defaults(func=cmd_synth)

    v = sub.add_parser("replay", help="verify labels against real diagnostics")
    v.add_argument("--repo", default="./mathlib4")
    v.add_argument("--pairs", default="out/pairs.jsonl")
    v.add_argument("--label", nargs="*", default=None)
    v.add_argument("--limit", type=int, default=100)
    v.add_argument("--timeout", type=int, default=900)
    v.add_argument("--no-build", action="store_true")
    v.add_argument("-o", "--out", default="out/replayed.jsonl")
    v.set_defaults(func=cmd_replay)

    t = sub.add_parser("stats", help="label distribution report")
    t.add_argument("--pairs", default="out/pairs.jsonl")
    t.set_defaults(func=cmd_stats)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "cap_per_label", None) == 0:
        args.cap_per_label = None
    if getattr(args, "cap_per_signature", None) == 0:
        args.cap_per_signature = None
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

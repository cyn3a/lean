#!/usr/bin/env python3
"""
Execute the churn-repair construction for ONE commit x ONE file.

Construction
------------
Let C be a mathlib4 master commit, P = C~1 its parent, and F a Mathlib .lean file
that C modified *as a call-site adaptation* (i.e. C changed the library elsewhere
and had to fix F to keep master green).

    library    := the whole tree at C, minus F
    broken     := contents of F at P     (old usage, new library)
    fix        := contents of F at C     (the human's actual repair)
    diagnostic := Lean's messages when elaborating `broken` against `library`

Validity conditions, all checked here:
    (control)  F@C elaborates clean against library C      -> harness is sound
    (break)    F@P does NOT elaborate clean against library C
    (restore)  F@C elaborates clean again afterwards       -> no state leakage

If (break) fails, there is NO pair. That is a real, reportable, useful result --
it is exactly what happens when the churn is a *deprecation with an alias*, since
the alias keeps the old code compiling (with a warning).

Two oracles are run and cross-checked:
  1. `lake env lean --json <opts> F`  -> structured messages (positions, severities)
  2. `lake build <module>`            -> CI-faithful pass/fail (uses lakefile leanOptions)
If they disagree on pass/fail, the scraped option set is wrong and the script says so.

Usage:
  python3 30_make_triple.py --repo ~/mathlib4 \
      --commit <sha> --file Mathlib/Foo/Bar.lean --out triples/
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import (  # noqa: E402
    checkout, decl_at, deprecation_warnings, errors, git, mathlib_lean_opts,
    module_of, olean_exists, parse_lean_json, parse_text_messages,
    removed_line_ranges, run, split_decls, tail, toolchain_of,
)


def overlapping_decls(decls, ranges):
    hit = []
    for d in decls:
        for (a, b) in ranges:
            if not (b < d["start_line"] or a > d["end_line"]):
                hit.append(d)
                break
    return hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--file", required=True, help="e.g. Mathlib/Order/Basic.lean")
    ap.add_argument("--out", default="triples")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--skip-cache-get", action="store_true",
                    help="skip if you already ran 20_probe_cache.py on this commit")
    ap.add_argument("--no-lake-build-oracle", action="store_true",
                    help="skip the second (CI-faithful) oracle; halves elaboration cost")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser()
    rel = args.file
    fpath = repo / rel
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    checkout(repo, args.commit)
    sha = git(repo, "rev-parse", args.commit).strip()
    parent = git(repo, "rev-parse", sha + "^").strip()
    subject = git(repo, "log", "-1", "--format=%s", sha).strip()
    tc = toolchain_of(repo)
    mod = module_of(rel)

    print(f"[i] commit    {sha[:12]}  {subject}")
    print(f"[i] parent    {parent[:12]}")
    print(f"[i] file      {rel}   (module {mod})")
    print(f"[i] toolchain {tc}")

    run(["elan", "toolchain", "install", tc], timeout=1800)

    if not args.skip_cache_get:
        r = run(["lake", "exe", "cache", "get"], cwd=repo, timeout=args.timeout)
        print(f"[i] cache get rc={r['rc']} in {r['seconds']}s")
        if r["rc"] != 0:
            print(tail(r["stdout"] + r["stderr"], 2000))
            raise SystemExit("[abort] cache get failed; nothing downstream is meaningful")
    print(f"[i] olean for {mod} present: {olean_exists(repo, mod)}")

    opts, opt_note = mathlib_lean_opts(repo)
    print(f"[i] lean opts ({opt_note}):\n      {' '.join(opts)}")

    if not fpath.exists():
        raise SystemExit(f"[abort] {rel} does not exist at {sha[:12]}")
    post_src = fpath.read_text(encoding="utf-8")
    try:
        pre_src = git(repo, "show", f"{parent}:{rel}")
    except RuntimeError:
        raise SystemExit(f"[abort] {rel} did not exist at parent -> new file, not a repair")
    if pre_src == post_src:
        raise SystemExit(f"[abort] {rel} is unchanged by {sha[:12]} -> no pair here")

    rec = {
        "schema": "lean-churn-triple/v0",
        "commit": sha, "parent": parent, "pr": None, "subject": subject,
        "toolchain": tc, "file": rel, "module": mod,
        "lean_opts": opts, "lean_opts_note": opt_note,
    }
    import re as _re
    m = _re.search(r"\(#(\d+)\)\s*$", subject)
    rec["pr"] = int(m.group(1)) if m else None

    try:
        # ---------- (control) F@C must elaborate clean ----------
        print("\n[1/4] control: elaborating F@C (should be clean) ...")
        c0 = run(["lake", "env", "lean", "--json", *opts, rel], cwd=repo, timeout=args.timeout)
        c0_msgs = parse_lean_json(c0["stdout"])
        c0_err = errors(c0_msgs)
        print(f"      rc={c0['rc']} in {c0['seconds']}s  errors={len(c0_err)} "
              f"warnings={sum(1 for m in c0_msgs if m['severity'] == 'warning')}")
        if c0_err:
            print("      [!!] F@C does NOT elaborate clean. Your harness is wrong, not Mathlib.")
            print("           Most likely the scraped lean options are off, or the cache is stale.")
            for m in c0_err[:3]:
                print(f"           {m['line']}:{m['col']} {m['text'][:200]}")
        rec["control_pre_edit"] = {
            "rc": c0["rc"], "seconds": c0["seconds"],
            "n_errors": len(c0_err), "clean": len(c0_err) == 0,
        }

        # ---------- (break) install F@P, elaborate against library C ----------
        print("\n[2/4] break: installing F@P and elaborating against library C ...")
        fpath.write_text(pre_src, encoding="utf-8")

        b = run(["lake", "env", "lean", "--json", *opts, rel], cwd=repo, timeout=args.timeout)
        b_msgs = parse_lean_json(b["stdout"])
        if not b_msgs and (b["stdout"] or b["stderr"]):
            b_msgs = parse_text_messages(b["stdout"] + "\n" + b["stderr"])
        b_err = errors(b_msgs)
        b_depr = deprecation_warnings(b_msgs)
        print(f"      rc={b['rc']} in {b['seconds']}s  errors={len(b_err)} "
              f"deprecation-warnings={len(b_depr)}")
        for m in (b_err or b_depr)[:5]:
            print(f"        {m['severity']} {m['line']}:{m['col']}  {str(m['text'])[:160]}")

        oracle = None
        if not args.no_lake_build_oracle:
            print("\n[3/4] oracle: `lake build` on the broken file (CI semantics) ...")
            ob = run(["lake", "build", mod], cwd=repo, timeout=args.timeout)
            ob_msgs = parse_text_messages(ob["stdout"] + "\n" + ob["stderr"])
            oracle = {
                "rc": ob["rc"], "seconds": ob["seconds"],
                "n_errors": len(errors(ob_msgs)),
                "n_deprecation_warnings": len(deprecation_warnings(ob_msgs)),
                "raw_tail": tail(ob["stdout"] + "\n" + ob["stderr"], 4000),
            }
            agree = (ob["rc"] != 0) == (len(b_err) > 0)
            oracle["agrees_with_lean_env"] = agree
            print(f"      rc={ob['rc']} in {ob['seconds']}s  "
                  f"agrees_with_lake_env_lean={agree}")
            if not agree:
                print("      [!] Oracles disagree -> your `-D` option set does not match the "
                      "lakefile's leanOptions. Fix before trusting anything.")
        else:
            print("\n[3/4] oracle: skipped (--no-lake-build-oracle)")

    finally:
        git(repo, "checkout", "--", rel)

    # ---------- (restore) F@C must build clean again ----------
    print("\n[4/4] restore: rebuilding F@C ...")
    rb = run(["lake", "build", mod], cwd=repo, timeout=args.timeout)
    print(f"      rc={rb['rc']} in {rb['seconds']}s")
    rec["control_post_restore"] = {"rc": rb["rc"], "seconds": rb["seconds"], "clean": rb["rc"] == 0}

    # ---------- declaration attribution ----------
    pre_decls = split_decls(pre_src)
    diff_u0 = git(repo, "diff", "-U0", "--no-renames", parent, sha, "--", rel)
    changed = overlapping_decls(pre_decls, removed_line_ranges(diff_u0))
    err_decls, seen = [], set()
    for m in (b_err or b_depr):
        d = decl_at(pre_decls, m.get("line"))
        key = (d["name"], d["start_line"]) if d else None
        if d and key not in seen:
            seen.add(key)
            err_decls.append(d)

    # ---------- verdict ----------
    if len(b_err) > 0:
        break_class = "HARD"
    elif len(b_depr) > 0:
        break_class = "SOFT_DEPRECATION_WARNING_ONLY"
    else:
        break_class = "NONE"

    single = (break_class != "NONE"
              and len(changed) == 1 and len(err_decls) == 1
              and changed[0]["name"] == err_decls[0]["name"])

    rec.update({
        "broken": {"source": pre_src, "sha": parent},
        "fix": {"source": post_src, "sha": sha},
        "unified_diff": "".join(difflib.unified_diff(
            pre_src.splitlines(keepends=True), post_src.splitlines(keepends=True),
            fromfile=f"a/{rel}@{parent[:8]}", tofile=f"b/{rel}@{sha[:8]}")),
        "diagnostic": {
            "rc": b["rc"], "seconds": b["seconds"], "messages": b_msgs,
            "n_errors": len(b_err), "n_deprecation_warnings": len(b_depr),
        },
        "oracle_lake_build": oracle,
        "attribution": {
            "declarations_in_pre_file": len(pre_decls),
            "changed_declarations": changed,
            "error_declarations": err_decls,
            "single_declaration_pair": single,
        },
        "verdict": {
            "break_class": break_class,
            "is_valid_pair": (break_class != "NONE"
                              and rec["control_pre_edit"]["clean"]
                              and rec["control_post_restore"]["clean"]),
        },
    })

    stem = f"{sha[:12]}__{rel.replace('/', '.')[:-5]}"
    (outdir / f"{stem}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                                         encoding="utf-8")

    md = [f"# churn-repair triple  {sha[:12]}  PR #{rec['pr']}", "",
          f"- **subject**: {subject}", f"- **file**: `{rel}`",
          f"- **toolchain**: `{tc}`",
          f"- **break class**: `{break_class}`",
          f"- **valid pair**: `{rec['verdict']['is_valid_pair']}`",
          f"- **single-declaration**: `{single}`", "",
          "## diagnostic", "```", tail("\n".join(
              f"{m['severity']} {rel}:{m['line']}:{m['col']}: {m['text']}"
              for m in (b_err or b_depr)), 6000) or "(none)", "```", "",
          "## fix (human's actual diff, pre -> post)", "```diff",
          tail(rec["unified_diff"], 8000), "```"]
    (outdir / f"{stem}.md").write_text("\n".join(md), encoding="utf-8")

    print("\n================ VERDICT ================")
    print(f"break class        : {break_class}")
    print(f"control F@C clean  : {rec['control_pre_edit']['clean']}")
    print(f"restore F@C clean  : {rec['control_post_restore']['clean']}")
    print(f"errors on F@P      : {len(b_err)}")
    print(f"depr warnings F@P  : {len(b_depr)}")
    print(f"changed decls      : {[d['name'] for d in changed]}")
    print(f"error decls        : {[d['name'] for d in err_decls]}")
    print(f"single-decl pair   : {single}")
    print(f"VALID TRIPLE       : {rec['verdict']['is_valid_pair']}")
    print(f"\nwrote {outdir / (stem + '.json')}")
    print(f"wrote {outdir / (stem + '.md')}")


if __name__ == "__main__":
    main()

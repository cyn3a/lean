#!/usr/bin/env python3
"""
Mine mathlib4 master for commits that are candidate sources of REAL churn-repair pairs.

Three commit classes, in decreasing order of expected yield:

  ALIAS_REMOVAL   The commit DELETES `@[deprecated ...]` aliases / declarations.
                  Any code still using the old name now hard-errors.
                  ==> best source of genuine, unambiguous breaks.

  DEPRECATION     The commit ADDS `@[deprecated ...]`.
                  Careful: Mathlib deprecations ship a backwards-compatible alias,
                  so the old code STILL COMPILES, with a warning. This is only a
                  "break" if you adopt warning-as-error semantics. Verify, do not assume.

  OTHER           No deprecation markers, but Mathlib .lean files were modified.
                  Contains signature changes, simp-set changes, generalizations —
                  the real workhorses of hard breakage. Noisy; needs filtering.

For each commit we split modified Mathlib files into:
  dep_source_files : where the deprecation marker was added/removed  (the CAUSE)
  adapt_files      : other modified .lean files                      (the REPAIRS)
`adapt_files` is the estimated pair yield of that commit.

Usage:
  python3 10_pick_commits.py --repo ~/mathlib4 --mode deprecations --n 10
  python3 10_pick_commits.py --repo ~/mathlib4 --mode window --n 10
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import git  # noqa: E402

DEPR_RE = re.compile(r"@\[[^\]]*\bdeprecated\b|^\s*deprecated_module\b")
PR_RE = re.compile(r"\(#(\d+)\)\s*$")
US = "\x1f"


def log_commits(repo, ref, skip, n):
    out = git(repo, "log", "--first-parent", ref,
              f"--skip={skip}", f"-n{n}", f"--format=%H{US}%ct{US}%an{US}%s")
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, ts, author, subject = line.split(US, 3)
        m = PR_RE.search(subject)
        rows.append({
            "sha": sha,
            "unix_time": int(ts),
            "author": author,
            "subject": subject,
            "pr": int(m.group(1)) if m else None,
        })
    return rows


def name_status(repo, sha):
    out = git(repo, "show", "--pretty=", "--name-status", "--no-renames", sha)
    res = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        res.append((parts[0][0], parts[-1]))
    return res


def file_diff(repo, sha, path):
    return git(repo, "show", "--pretty=", "-U0", "--no-renames", sha, "--", path)


def classify(repo, sha):
    ns = name_status(repo, sha)
    all_paths = [p for _, p in ns]

    def is_mathlib_lean(p):
        return p.startswith("Mathlib/") and p.endswith(".lean")

    mod = [p for st, p in ns if st == "M" and is_mathlib_lean(p)]
    deleted = [p for st, p in ns if st == "D" and is_mathlib_lean(p)]

    dep_added, dep_removed, adapt = [], [], []
    for p in mod:
        d = file_diff(repo, sha, p)
        a = r = 0
        for line in d.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                if DEPR_RE.search(line[1:]):
                    a += 1
            elif line.startswith("-") and not line.startswith("---"):
                if DEPR_RE.search(line[1:]):
                    r += 1
        if a:
            dep_added.append(p)
        if r:
            dep_removed.append(p)
        if not a and not r:
            adapt.append(p)

    deleted_deprecated_files = [p for p in deleted if "Deprecated" in p]

    if dep_removed or deleted_deprecated_files:
        klass = "ALIAS_REMOVAL"
    elif dep_added:
        klass = "DEPRECATION"
    elif adapt:
        klass = "OTHER"
    else:
        klass = "NONE"

    return {
        "class": klass,
        "toolchain_bump": ("lean-toolchain" in all_paths),
        "manifest_bump": ("lake-manifest.json" in all_paths),
        "n_files_touched": len(all_paths),
        "dep_added_files": dep_added,
        "dep_removed_files": dep_removed,
        "deleted_deprecated_files": deleted_deprecated_files,
        "adapt_files": adapt,
        "expected_pair_yield": len(adapt),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", default="origin/master")
    ap.add_argument("--scan", type=int, default=400,
                    help="how many master commits to scan backwards")
    ap.add_argument("--skip-recent", type=int, default=30,
                    help="skip the N newest commits; their cache may not exist yet")
    ap.add_argument("--mode", choices=["deprecations", "window"], default="deprecations")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--anchor", default=None,
                    help="window mode: newest commit of the 10-commit window")
    ap.add_argument("--min-yield", type=int, default=1)
    ap.add_argument("--out", default="candidates.json")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser()
    commits = log_commits(repo, args.ref, args.skip_recent, args.scan)
    print(f"[i] scanning {len(commits)} commits from {args.ref} "
          f"(skipping {args.skip_recent} newest)", file=sys.stderr)

    for i, c in enumerate(commits):
        c.update(classify(repo, c["sha"]))
        if (i + 1) % 50 == 0:
            print(f"[i] classified {i + 1}/{len(commits)}", file=sys.stderr)

    if args.mode == "window":
        anchor = args.anchor
        if anchor is None:
            pool = [c for c in commits
                    if c["class"] in ("ALIAS_REMOVAL", "DEPRECATION")
                    and c["expected_pair_yield"] >= args.min_yield
                    and not c["toolchain_bump"]]
            if not pool:
                raise SystemExit("[abort] no deprecation commit found to anchor the window")
            anchor = pool[0]["sha"]
            print(f"[i] anchoring window at {anchor[:12]}  {pool[0]['subject']}", file=sys.stderr)
        shas = [line.strip() for line in
                git(repo, "log", "--first-parent", f"-n{args.n}", "--format=%H", anchor).splitlines()
                if line.strip()]
        by_sha = {c["sha"]: c for c in commits}
        picked = []
        for sha in reversed(shas):  # oldest -> newest
            picked.append(by_sha.get(sha) or dict(sha=sha, **classify(repo, sha)))
    else:
        pool = [c for c in commits
                if c["class"] in ("ALIAS_REMOVAL", "DEPRECATION")
                and c["expected_pair_yield"] >= args.min_yield
                and not c["toolchain_bump"]]
        pool.sort(key=lambda c: (c["class"] != "ALIAS_REMOVAL", -c["expected_pair_yield"]))
        picked = pool[: args.n]
        picked.sort(key=lambda c: c["unix_time"])  # oldest -> newest for cache locality

    Path(args.out).write_text(json.dumps(picked, indent=2), encoding="utf-8")

    n_alias = sum(1 for c in commits if c["class"] == "ALIAS_REMOVAL")
    n_dep = sum(1 for c in commits if c["class"] == "DEPRECATION")
    print(f"\n[survey over {len(commits)} commits]", file=sys.stderr)
    print(f"  ALIAS_REMOVAL : {n_alias}", file=sys.stderr)
    print(f"  DEPRECATION   : {n_dep}", file=sys.stderr)
    print(f"  total adapt_files in deprecation-ish commits: "
          f"{sum(c['expected_pair_yield'] for c in commits if c['class'] in ('ALIAS_REMOVAL','DEPRECATION'))}",
          file=sys.stderr)

    print(f"\n[picked {len(picked)} -> {args.out}]  (oldest first)")
    print(f"{'sha':14} {'class':14} {'yield':>5}  {'PR':>7}  subject")
    for c in picked:
        print(f"{c['sha'][:12]:14} {c['class']:14} {c['expected_pair_yield']:>5}  "
              f"{str(c.get('pr')):>7}  {c.get('subject', '')[:70]}")


if __name__ == "__main__":
    main()

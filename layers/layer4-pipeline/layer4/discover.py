"""Find adaptation windows.

Reality check performed against leanprover-community/mathlib4 on 2026-07-28:

* There are currently **zero** live `bump/v4.X.0` or `lean-pr-testing-NNNN`
  refs. Both are ephemeral and get deleted. Live infra refs use newer, less
  regular names (`bump_to_v4.28.1`, `last_bump_for_v4.31.0`).
* mathlib4 master has **7 merge commits total**, all from 2021. Bump branches
  are *squash-merged*, so merge-topology mining finds nothing.
* What is durable is the squashed commit on master, e.g.
  ``chore: bump toolchain to v4.33.0-rc1 (#41779)`` -- one commit carrying the
  whole collapsed bump branch (2433 files, 8421+/2018- for v4.33.0-rc1).

So the primary discovery mode is **squashed commits on master**, with live
branches as an opportunistic secondary source and merge topology as a
fallback for forks that do use real merges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

from .gitio import Git, Commit

TOOLCHAIN_FILE = "lean-toolchain"

#: Ordered most-specific-first. Every pattern must expose a `key` group.
MESSAGE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("bump", re.compile(
        r"^chore:?\s*bump\s+toolchain\s+to\s+`?(?P<key>v?[\d.]+(?:-rc\d+)?|nightly-[\d-]+)`?",
        re.I)),
    ("bump", re.compile(r"^chore:?\s*bump\s+(?:lean|to)\s+`?(?P<key>v?4[\d.]+\S*)`?", re.I)),
    ("bump", re.compile(r"^bump:?\s*Lean\s*4?\s*to\s+(?P<key>\S+)", re.I)),
    ("dep-adaptation", re.compile(
        r"^chore:?\s*adaptations?\s+for\s+(?P<key>[\w+]+#\d+|[\w /-]+?)\s*(?:\(#\d+\))?$", re.I)),
    ("dep-adaptation", re.compile(
        r"^chore:?\s*adapt(?:ations?)?\s+(?:to|after)\s+(?P<key>\S+)", re.I)),
    ("backport", re.compile(
        r"^chore.*backport.*from\s+(?P<key>nightly-testing|bump/\S+)", re.I)),
]

#: Live branch shapes, oldest convention first. `key` group required.
BRANCH_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("bump", re.compile(r"^bump/v(?P<key>\d+\.\d+\.\d+)$")),
    ("bump-nightly", re.compile(r"^bump/nightly-(?P<key>\d{4}-\d{2}-\d{2})$")),
    ("bump", re.compile(r"^bump_to_v(?P<key>[\d.]+)$")),
    ("bump", re.compile(r"^last_bump_for_v(?P<key>[\d.]+)$")),
    ("lean-pr-testing", re.compile(r"^lean-pr-testing-(?P<key>\d+)$")),
    ("nightly-testing", re.compile(r"^nightly-testing(?:-(?P<key>[\w.-]+))?$")),
]

LS_REMOTE_GLOBS = (
    "bump/*", "bump_to_*", "last_bump_for_*", "lean-pr-testing-*", "nightly-testing*",
)


@dataclass
class Window:
    kind: str            # bump | dep-adaptation | lean-pr-testing | ...
    key: str             # v4.33.0-rc1 | batteries#1921 | 4123
    source: str          # squash | branch | merge
    ref: str             # branch name or commit subject
    base: str            # sha to diff *from* (exclusive)
    tip: str             # sha to diff *to* (inclusive)
    date: str = ""
    pr: str | None = None
    toolchain_before: str = ""
    toolchain_after: str = ""

    def to_json(self) -> dict:
        return asdict(self)


_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def _toolchain(git: Git, sha: str) -> str:
    txt = git.file_at(sha, TOOLCHAIN_FILE)
    return (txt or "").strip()


def _match_message(subject: str) -> tuple[str, str] | None:
    for kind, pat in MESSAGE_PATTERNS:
        m = pat.match(subject.strip())
        if m:
            key = (m.group("key") or "").strip().strip("`")
            if key:
                return kind, key
    return None


def from_squash_commits(git: Git, mainline: str = "master", *,
                        limit: int | None = None,
                        include_dep_adaptations: bool = True,
                        resolve_toolchain: bool = True) -> list[Window]:
    """Primary source: squashed bump/adaptation commits on the mainline."""
    windows: list[Window] = []
    for c in git.log(mainline, no_merges=False, limit=limit):
        hit = _match_message(c.subject)
        if not hit:
            continue
        kind, key = hit
        if kind == "dep-adaptation" and not include_dep_adaptations:
            continue
        if not c.parents:
            continue
        pr = m.group(1) if (m := _PR_RE.search(c.subject)) else None
        w = Window(kind=kind, key=key, source="squash", ref=c.subject,
                   base=c.parents[0], tip=c.sha, date=c.author_date[:10], pr=pr)
        if resolve_toolchain:
            w.toolchain_before = _toolchain(git, w.base)
            w.toolchain_after = _toolchain(git, w.tip)
        windows.append(w)
    return windows


def from_toolchain_changes(git: Git, mainline: str = "master",
                           limit: int | None = None) -> list[Window]:
    """Message-independent fallback: any commit that edits `lean-toolchain`.

    Requires trees to be local. On a `--filter=tree:0` clone this is
    pathologically slow; see `Git.check_clone_health`.
    """
    windows = []
    for c in git.log(mainline, paths=[TOOLCHAIN_FILE], no_merges=False, limit=limit):
        if not c.parents:
            continue
        before, after = _toolchain(git, c.parents[0]), _toolchain(git, c.sha)
        if before == after:
            continue
        windows.append(Window(
            kind="bump", key=after or c.sha[:8], source="squash", ref=c.subject,
            base=c.parents[0], tip=c.sha, date=c.author_date[:10],
            pr=(m.group(1) if (m := _PR_RE.search(c.subject)) else None),
            toolchain_before=before, toolchain_after=after,
        ))
    return windows


def from_branches(git: Git, mainline: str = "master",
                  heads: dict[str, str] | None = None) -> list[Window]:
    """Secondary source: live ephemeral branches, if any still exist.

    Uses `tip ^mainline`, so master-merged-into-branch commits drop out and
    only genuine adaptation commits remain.
    """
    heads = heads if heads is not None else git.local_heads()
    base_sha = git.rev_parse(mainline)
    windows = []
    for name, sha in heads.items():
        short = name.split("/", 1)[1] if name.startswith(("origin/", "upstream/")) else name
        for kind, pat in BRANCH_PATTERNS:
            m = pat.match(short)
            if not m:
                continue
            windows.append(Window(
                kind=kind, key=(m.groupdict().get("key") or short), source="branch",
                ref=short, base=base_sha, tip=sha,
                toolchain_before=_toolchain(git, base_sha),
                toolchain_after=_toolchain(git, sha),
            ))
            break
    return windows


def from_merges(git: Git, mainline: str = "master") -> list[Window]:
    """Fallback for repos that really do merge bump branches (not mathlib4).

    For merge M, adaptation commits are exactly `M^2 ^M^1`, because repeated
    master->branch merges are ancestors of M^1 and drop out.
    """
    windows = []
    for c in git.log(mainline, no_merges=False):
        if not c.is_merge or len(c.parents) < 2:
            continue
        name = None
        for pat in (r"Merge branch '(?P<n>[^']+)'",
                    r"Merge pull request #\d+ from [\w-]+/(?P<n>\S+)",
                    r"Merge remote-tracking branch '[^/]+/(?P<n>[^']+)'"):
            if m := re.search(pat, c.subject):
                name = m.group("n")
                break
        kind = key = None
        if name:
            for k, pat in BRANCH_PATTERNS:
                if m := pat.match(name):
                    kind, key = k, (m.groupdict().get("key") or name)
                    break
        before, after = _toolchain(git, c.parents[0]), _toolchain(git, c.sha)
        if kind is None and before != after:
            kind, key = "bump", after or c.sha[:8]
        if kind is None:
            continue
        windows.append(Window(
            kind=kind, key=key or "", source="merge", ref=name or c.subject,
            base=c.parents[0], tip=c.parents[1], date=c.author_date[:10],
            toolchain_before=before, toolchain_after=after,
        ))
    return windows


def discover(git: Git, mainline: str = "master", *, modes=("squash", "branch"),
             limit: int | None = None,
             include_dep_adaptations: bool = True) -> list[Window]:
    seen: set[tuple[str, str]] = set()
    out: list[Window] = []
    dispatch = {
        "squash": lambda: from_squash_commits(
            git, mainline, limit=limit,
            include_dep_adaptations=include_dep_adaptations),
        "toolchain": lambda: from_toolchain_changes(git, mainline, limit=limit),
        "branch": lambda: from_branches(git, mainline),
        "merge": lambda: from_merges(git, mainline),
    }
    for mode in modes:
        if mode not in dispatch:
            raise ValueError(f"unknown discovery mode: {mode}")
        for w in dispatch[mode]():
            k = (w.base, w.tip)
            if k in seen:
                continue
            seen.add(k)
            out.append(w)
    out.sort(key=lambda w: w.date, reverse=True)
    return out

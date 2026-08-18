"""Window -> labelled (broken, fixed) repair pairs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict

from . import leanlex, taxonomy
from .diffparse import parse_diff, Hunk
from .discover import Window
from .gitio import Git

#: Directory prefixes, NOT globs. Git's default pathspec matcher treats
#: `Mathlib/**/*.lean` as *excluding* top-level `Mathlib/Foo.lean` (only
#: `:(glob)` magic gives wildmatch semantics), which silently drops a large
#: slice of the corpus. Prefixes + an extension filter avoid the trap.
DEFAULT_PATHS = ["Mathlib/", "Archive/", "Counterexamples/",
                 "MathlibTest/", "test/"]
LEAN_SUFFIXES = (".lean",)

#: Files whose churn is not a *repair* signal.
EXCLUDE_SUBSTRINGS = ("Mathlib.lean", "/Deprecated/", "scripts/", ".github/")


@dataclass
class RepairPair:
    sample_id: str
    #: pre-adaptation code: compiles under `toolchain_before`, FAILS under
    #: `toolchain_after`. This is the model input.
    broken: str
    #: post-adaptation code: compiles under `toolchain_after`. The target.
    fixed: str
    label: str
    labels: list[str]
    confidence: float
    expected_errors: list[str]
    path: str
    decl: str | None
    decl_kind: str | None
    namespace: str
    #: contiguous file slices, so the pair can be replayed in situ
    broken_window: str
    fixed_window: str
    ctx_before: list[str]
    ctx_after: list[str]
    line_new: int
    line_old: int
    toolchain_before: str
    toolchain_after: str
    window_kind: str
    window_key: str
    commit: str
    pr: str | None
    date: str
    evidence: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def _sid(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def _excluded(path: str) -> bool:
    return any(s in path for s in EXCLUDE_SUBSTRINGS)


def mine_window(git: Git, w: Window, *, paths: list[str] | None = None,
                context: int = 6, gap: int = 0, keep_noise: bool = False,
                min_confidence: float = 0.0,
                max_hunk_lines: int = 60) -> list[RepairPair]:
    paths = paths or DEFAULT_PATHS
    diff_text = git.diff(w.base, w.tip, paths=paths, context=context)
    files = parse_diff(diff_text, gap=gap)

    out: list[RepairPair] = []
    for fd in files:
        if fd.is_binary or _excluded(fd.path):
            continue
        if not fd.path.endswith(LEAN_SUFFIXES):
            continue
        # A new file has no "broken" prior state; a deleted one has no target.
        if fd.is_new or fd.is_deleted:
            continue
        pre_file = git.file_at(w.base, fd.old_path or fd.path) or ""
        post_file = git.file_at(w.tip, fd.new_path or fd.path) or ""

        for h in fd.hunks:
            if len(h.old_lines) + len(h.new_lines) > max_hunk_lines:
                continue
            verdict = taxonomy.classify(h)
            if verdict.noise and not keep_noise:
                continue
            if verdict.confidence < min_confidence:
                continue

            ctx = leanlex.scan_context(post_file, h.change_new_line) if post_file \
                else leanlex.scan_context(pre_file, h.change_old_line)

            notes = list(verdict.evidence.get("notes", []))
            pair = RepairPair(
                sample_id=_sid(w.tip, fd.path, str(h.change_new_line), h.old_text),
                broken=h.old_text,
                fixed=h.new_text,
                label=verdict.primary,
                labels=verdict.labels,
                confidence=round(verdict.confidence, 3),
                expected_errors=verdict.expected_errors,
                path=fd.path,
                decl=ctx.name,
                decl_kind=ctx.kind,
                namespace=ctx.namespace,
                broken_window=h.window("old"),
                fixed_window=h.window("new"),
                ctx_before=h.ctx_before,
                ctx_after=h.ctx_after,
                line_new=h.change_new_line,
                line_old=h.change_old_line,
                toolchain_before=w.toolchain_before,
                toolchain_after=w.toolchain_after,
                window_kind=w.kind,
                window_key=w.key,
                commit=w.tip,
                pr=w.pr,
                date=w.date,
                evidence=verdict.evidence,
                notes=notes,
            )
            out.append(pair)
    return out


def mine(git: Git, windows: list[Window], *, progress=None, **kw) -> list[RepairPair]:
    pairs: list[RepairPair] = []
    for i, w in enumerate(windows, 1):
        try:
            got = mine_window(git, w, **kw)
        except Exception as exc:
            if progress:
                progress(f"  !! {w.key}: {type(exc).__name__}: {exc}")
            continue
        pairs.extend(got)
        if progress:
            progress(f"  [{i}/{len(windows)}] {w.kind} {w.key} -> {len(got)} pairs")
    return pairs

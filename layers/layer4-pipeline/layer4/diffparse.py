"""Unified-diff parser. Stdlib only, no `unidiff` dependency.

Produces `FileDiff` / `Hunk` records that keep both sides plus surrounding
context, which is what the reversal step needs (the post side must be
independently applicable to a clean checkout).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(
    r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@(?P<sec>.*)$"
)
_DIFF_RE = re.compile(r'^diff --git "?a/(?P<a>.+?)"? "?b/(?P<b>.+?)"?$')


@dataclass
class Hunk:
    path: str
    old_start: int
    new_start: int
    section: str
    #: lines exactly as they appear, without the leading +/-/space marker
    #: pure context lines, for human-readable output
    ctx_before: list[str] = field(default_factory=list)
    #: interleaved (kind, text) context, so each side's window is derivable
    ctx_before_entries: list[tuple[str, str]] = field(default_factory=list)
    ctx_after_entries: list[tuple[str, str]] = field(default_factory=list)
    #: changed lines only -- what the taxonomy and rule induction look at
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    ctx_after: list[str] = field(default_factory=list)
    #: full interleaved (kind, text) body incl. interior context, so that
    #: pre/post windows reconstruct byte-exactly for reversal
    body: list[tuple[str, str]] = field(default_factory=list)
    #: 1-based line number in the *new* file where the changed block begins
    change_new_line: int = 0
    change_old_line: int = 0

    @property
    def is_pure_addition(self) -> bool:
        return not self.old_lines and bool(self.new_lines)

    @property
    def is_pure_deletion(self) -> bool:
        return bool(self.old_lines) and not self.new_lines

    @property
    def old_text(self) -> str:
        return "\n".join(self.old_lines)

    @property
    def new_text(self) -> str:
        return "\n".join(self.new_lines)

    def window(self, side: str = "new", context: bool = True) -> str:
        """Full applicable window: context + one side. Used for reversal.

        Includes interior context lines, so the returned text is a contiguous
        slice of the real file and can be string-matched against a checkout.
        """
        keep = " +" if side == "new" else " -"
        core = [t for k, t in self.body if k in keep]
        if not context:
            return "\n".join(core)
        # context must be filtered per side too: a neighbouring run's `-` line
        # is not present in the post-adaptation file, and vice versa.
        before = [t for k, t in self.ctx_before_entries if k in keep]
        after = [t for k, t in self.ctx_after_entries if k in keep]
        return "\n".join([*before, *core, *after])


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    is_binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "<unknown>"


def _split_hunk(raw: list[str], path: str, old_start: int, new_start: int,
                section: str, gap: int = 0) -> list[Hunk]:
    """Split one @@ block into maximal contiguous change runs.

    A single @@ block can contain several unrelated edits separated by context
    when -U is large; each run becomes its own Hunk so that labels and induced
    rules stay attributable to one edit.
    """
    # Classify each raw line and track line numbers on both sides.
    entries: list[tuple[str, str, int, int]] = []  # (kind, text, oldno, newno)
    o, n = old_start, new_start
    for line in raw:
        if not line:
            kind, text = " ", ""
        else:
            kind, text = line[0], line[1:]
        if kind == "\\":  # "\ No newline at end of file"
            continue
        if kind == " ":
            entries.append((" ", text, o, n)); o += 1; n += 1
        elif kind == "-":
            entries.append(("-", text, o, n)); o += 1
        elif kind == "+":
            entries.append(("+", text, o, n)); n += 1
        else:  # tolerate malformed context lines emitted as bare text
            entries.append((" ", line, o, n)); o += 1; n += 1

    change_idx = [i for i, e in enumerate(entries) if e[0] in "+-"]
    if not change_idx:
        return []

    # Group into runs. `gap` is how many context lines may sit *inside* a run.
    # 0 is the right default: git emits all `-` then all `+` for one edit, so
    # any intervening context means two independent edits, and merging them
    # would give a hunk with two unrelated labels.
    runs: list[list[int]] = [[change_idx[0]]]
    for i in change_idx[1:]:
        if i - runs[-1][-1] <= gap + 1:
            runs[-1].append(i)
        else:
            runs.append([i])

    hunks: list[Hunk] = []
    for run in runs:
        lo, hi = run[0], run[-1]
        # widen to include interior context that got absorbed by GAP
        body = entries[lo:hi + 1]
        before_e = [(e[0], e[1]) for e in entries[max(0, lo - 6):lo]]
        after_e = [(e[0], e[1]) for e in entries[hi + 1:hi + 7]]
        hunks.append(Hunk(
            path=path,
            old_start=old_start,
            new_start=new_start,
            section=section.strip(),
            ctx_before=[t for k, t in before_e if k == " "],
            ctx_before_entries=before_e,
            ctx_after_entries=after_e,
            old_lines=[e[1] for e in body if e[0] == "-"],
            new_lines=[e[1] for e in body if e[0] == "+"],
            ctx_after=[t for k, t in after_e if k == " "],
            body=[(e[0], e[1]) for e in body],
            change_old_line=entries[lo][2],
            change_new_line=entries[lo][3],
        ))
    return hunks


def parse_diff(text: str, gap: int = 0) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    pending: list[str] | None = None
    p_old = p_new = 0
    p_sec = ""

    def flush() -> None:
        nonlocal pending
        if cur is not None and pending is not None:
            cur.hunks.extend(_split_hunk(pending, cur.path, p_old, p_new, p_sec, gap))
        pending = None

    for line in text.split("\n"):
        m = _DIFF_RE.match(line)
        if m:
            flush()
            cur = FileDiff(old_path=m.group("a"), new_path=m.group("b"))
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            flush(); cur.is_binary = True; continue
        if line.startswith("new file mode"):
            cur.is_new = True; continue
        if line.startswith("deleted file mode"):
            cur.is_deleted = True; continue
        if line.startswith("rename from") or line.startswith("rename to"):
            cur.is_rename = True; continue
        if line.startswith("--- "):
            flush()
            if line[4:].strip() == "/dev/null":
                cur.old_path = None; cur.is_new = True
            continue
        if line.startswith("+++ "):
            flush()
            if line[4:].strip() == "/dev/null":
                cur.new_path = None; cur.is_deleted = True
            continue
        hm = _HUNK_RE.match(line)
        if hm:
            flush()
            p_old = int(hm.group("os"))
            p_new = int(hm.group("ns"))
            p_sec = hm.group("sec")
            pending = []
            continue
        if pending is not None:
            pending.append(line)

    flush()
    return files

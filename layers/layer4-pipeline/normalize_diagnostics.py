#!/usr/bin/env python3
"""Layer 3: normalize Lean 4 compiler diagnostics and group them into templates.

The paper describes Layer 3 as:

    "Extract diagnostics from real compilations. Remove unstable details such
    as absolute paths, metavariable identifiers and hygienic macro scopes.
    Split messages into main text, formatted payload values and additional
    notes, then group them into parameterized templates via alignment
    inspired by Drain [He et al. 2017]."

This module implements exactly those three steps over the per-diagnostic JSON
files produced by the build pipeline (``parsed-errors/``):

1. :func:`normalize_message` removes unstable details (paths, metavariable
   ids, hygienic macro scopes, inaccessible-name daggers, unique ids, line
   endings).  It is deliberately conservative: Lean identifiers, numerals in
   Lean code and goal structure are left untouched.
2. :func:`split_message` splits a (normalized) message into ``main_text``,
   ``payloads`` (pretty-printed terms / types / goals) and ``notes``.
3. :class:`Drain` groups the ``main_text`` of each diagnostic (payload blocks
   collapsed into a ``<PAYLOAD>`` token) into parameterized templates using
   the fixed-depth parse tree and token alignment of Drain.

Only the Python standard library is used (Python 3.11+).

CLI
---
    python normalize_diagnostics.py --errors-root parsed-errors --out-dir layer3-out
                                    [--depth 4] [--sim-threshold 0.4] [--max-children 100]

Outputs (all UTF-8):

* ``normalized-diagnostics.jsonl`` -- one row per input file: all original
  fields plus ``source_file``, ``normalized_message``, ``main_text``,
  ``payloads``, ``notes``, ``template_id``, ``template``.
* ``templates.jsonl`` -- one row per template: ``template_id``, ``template``,
  ``count``, ``n_pairs``, ``n_repos``, ``n_transitions``, ``example_message``,
  ``example_file``.
* ``summary.json`` -- ``counts`` (diagnostics / templates / singletons),
  ``top_templates`` (top 30), ``per_transition`` template counts and the
  parameters used.

Inputs are processed in sorted-filename order so the output is reproducible.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Placeholder tokens
# ---------------------------------------------------------------------------

PATH_TOKEN = "<PATH>"
MVAR_TOKEN = "<MVAR>"
UNIQ_TOKEN = "<UNIQ>"
PAYLOAD_TOKEN = "<PAYLOAD>"
WILDCARD = "<*>"
EMPTY_TOKEN = "<EMPTY>"

# ---------------------------------------------------------------------------
# 1. Normalization of unstable details
# ---------------------------------------------------------------------------

# Characters that terminate a path token (whitespace, quotes, brackets used by
# Lean messages around file names).
_PATH_END = r"""\s'"`<>|)\]"""

# Windows absolute path: drive letter, ``:``, then ``\`` or ``/`` and a run of
# non-terminating characters.  Case-insensitive drive letter (Lean prints
# ``c:\Users\...``).
_WIN_PATH_RE = re.compile(rf"(?<![\w])[A-Za-z]:[\\/][^{_PATH_END}]+")

# POSIX absolute path.  Conservative: must start at a non-word boundary, have at
# least two segments, and either start from a well-known root directory, pass
# through a ``.lake`` directory, or end with a build/source extension.  This
# keeps ``a / b`` (Lean division, always spaced by the pretty printer) and
# anonymous-constructor syntax intact.
_POSIX_ROOTS = (
    "home|Users|tmp|root|mnt|opt|var|usr|private|nix|workspace|workspaces|github|"
    "runner|builds|srv|data|etc|proc|lake|c|d"
)
_POSIX_EXTS = r"lean|olean|ilean|c|o|h|json|toml|trace|hash|log|txt|md|out|err"
_POSIX_PATH_RE = re.compile(
    rf"(?<![\w./\\])/(?:(?:{_POSIX_ROOTS})/[^{_PATH_END}]*"
    rf"|(?:[^{_PATH_END}/]+/)*\.lake/[^{_PATH_END}]*"
    rf"|(?:[^{_PATH_END}/]+/)+[^{_PATH_END}/]+\.(?:{_POSIX_EXTS})(?![\w.]))"
)

# Relative ``.lake/packages/...`` (or ``.lake\packages\...``) paths that Lake
# prints for dependency sources, with an optional ``./`` / ``../`` prefix.
_LAKE_REL_PATH_RE = re.compile(
    rf"(?<![\w/\\])(?:\.{{1,2}}[\\/])*\.lake[\\/][^{_PATH_END}]+"
)

# Metavariables.  ``?_uniq.N`` must be handled before ``_uniq.N``.
_MVAR_RE = re.compile(r"\?(?:m|u|_uniq)\.\d+")
# Bare unique ids (``_uniq.1234``) that leak from the kernel / elaborator.
_UNIQ_RE = re.compile(r"(?<![\w?])_uniq\.\d+")
# Hygienic macro scopes: ``name._@.Module.Path._hyg.123`` (optionally several
# numeric scopes) -> ``name``.
_HYG_FULL_RE = re.compile(r"\._@\.(?:[^\s.]+\.)*?_hyg(?:\.\d+)+")
_HYG_TAIL_RE = re.compile(r"\._hyg(?:\.\d+)+")
# Inaccessible-name marker ``✝`` plus optional superscript index: ``inst✝¹`` -> ``inst``.
_DAGGER_RE = re.compile("\u271d[\u2070\u00b9\u00b2\u00b3\u2074-\u2079]*")


def normalize_message(message: str) -> str:
    """Remove unstable details from a Lean diagnostic message.

    Normalized classes (in this order):

    * line endings: ``\\r\\n`` / ``\\r`` -> ``\\n``;
    * absolute paths (Windows ``C:\\...`` or ``C:/...``, POSIX ``/home/...``,
      ``/.../Foo.lean``) and relative ``.lake/packages/...`` paths -> ``<PATH>``;
    * metavariables ``?m.N``, ``?u.N`` (universe metavariables) and
      ``?_uniq.N`` -> ``<MVAR>``;
    * bare unique ids ``_uniq.N`` -> ``<UNIQ>``;
    * hygienic macro scopes ``x._@.Mod.Name._hyg.N`` -> ``x`` (base name kept);
    * inaccessible-name markers ``x✝``, ``inst✝¹`` -> ``x``, ``inst``.

    Everything else (identifiers, numerals, goal structure, indentation) is
    kept verbatim.  Named metavariables such as ``?f`` or ``?_eq_none`` are
    *not* normalized: their names come from binders and are stable.
    """
    text = message.replace("\r\n", "\n").replace("\r", "\n")
    text = _WIN_PATH_RE.sub(PATH_TOKEN, text)
    text = _POSIX_PATH_RE.sub(PATH_TOKEN, text)
    text = _LAKE_REL_PATH_RE.sub(PATH_TOKEN, text)
    text = _MVAR_RE.sub(MVAR_TOKEN, text)
    text = _UNIQ_RE.sub(UNIQ_TOKEN, text)
    text = _HYG_FULL_RE.sub("", text)
    text = _HYG_TAIL_RE.sub("", text)
    text = _DAGGER_RE.sub("", text)
    return text


# ---------------------------------------------------------------------------
# 2. Splitting into main text / payloads / notes
# ---------------------------------------------------------------------------

# Prefixes that start an "additional note" block even without a preceding
# blank line.  The first five come from the Layer 3 description; the lowercase
# ``note:`` is the linter spelling, and "Additional diagnostic information"
# is the pre-4.9 wording of what newer Lean prints as "Hint: Additional ...".
NOTE_PREFIXES = (
    "Note:",
    "note:",
    "Hint:",
    "Suggestion",
    "Explanation",
    "All Messages",
    "Additional diagnostic information",
)

_INDENTED_RE = re.compile(r"^(?: {2,}|\t)")
_GOAL_RE = re.compile(r"^\u22a2(?:\s|$)")  # ⊢
_CASE_RE = re.compile(r"^case \S+$")
# Hypothesis line of a goal context: one or more identifier-like names, then
# `` : `` and a type (``inst : Group G``, ``A B : Subgroup G``), or a bare
# ``names :`` whose (long) type Lean wrapped onto the following indented line.
# The names may not contain whitespace or ``:``; prose lines virtually never
# contain a spaced colon after a run of bare tokens.
_HYP_RE = re.compile(r"^(?:[^\s:]+ )*[^\s:]+ :(?: \S.*)?$")


def _is_payload_line(line: str) -> bool:
    return bool(
        _INDENTED_RE.match(line)
        or _GOAL_RE.match(line)
        or _CASE_RE.match(line)
        or _HYP_RE.match(line)
    )


def _is_note_start(line: str) -> bool:
    return line.startswith(NOTE_PREFIXES)


def split_message(message: str) -> dict:
    """Split a Lean diagnostic into ``main_text``, ``payloads`` and ``notes``.

    The message is processed line by line (it should already be normalized).
    Rules, applied in order to every line after the head line:

    1. A blank line is a separator: it closes the current payload block or
       note and marks that the next line follows a blank line.
    2. While inside a note, every non-blank line (indented or not) is appended
       to that note.  A note therefore runs from its first line to the next
       blank line, including indented bullet lists under ``Hint:`` and the
       argument block under ``Note: Expected a function because ...``.
    3. A line starting with one of :data:`NOTE_PREFIXES` starts a new note.
    4. A *payload* line is one that is indented by two or more spaces (or a
       tab), starts with ``⊢``, is a ``case foo`` label, or has the shape of
       a goal-context hypothesis ``names : type``.  Consecutive payload lines
       form one payload block (kept verbatim, including indentation).
    5. Any other non-indented line that directly follows a blank line starts
       a note (a trailing sentence after a blank line is a note).
    6. Any other non-indented line is main text (e.g. ``has type``, ``but is
       expected to have type``, ``argument``, bullet lists of reasons).

    The first non-empty line is always the head of the main text.  The
    ``main_text`` is the head and all main-text lines joined with single
    spaces, where every maximal run of payload blocks (blank-line separated
    runs included, with no main-text line in between) is replaced by one
    ``<PAYLOAD>`` token.  Internal whitespace is collapsed.  ``payloads`` is
    the list of payload blocks (strings, lines joined with ``\\n``) and
    ``notes`` is the list of note blocks, both in order of appearance.
    """
    lines = [ln.rstrip() for ln in message.split("\n")]
    # Drop leading blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return {"main_text": "", "payloads": [], "notes": []}

    head, rest = lines[0].strip(), lines[1:]
    main_parts: list[str] = [head]
    payloads: list[str] = []
    notes: list[str] = []

    cur_payload: list[str] = []
    cur_note: list[str] = []
    after_blank = False
    last_main_was_payload = False  # whether the last main_parts item is <PAYLOAD>

    def flush_payload() -> None:
        nonlocal cur_payload, last_main_was_payload
        if cur_payload:
            payloads.append("\n".join(cur_payload))
            if not last_main_was_payload:
                main_parts.append(PAYLOAD_TOKEN)
                last_main_was_payload = True
            cur_payload = []

    def flush_note() -> None:
        nonlocal cur_note
        if cur_note:
            notes.append("\n".join(cur_note))
            cur_note = []

    for line in rest:
        if not line.strip():
            flush_payload()
            flush_note()
            after_blank = True
            continue
        if cur_note:
            cur_note.append(line)
            continue
        if _is_note_start(line):
            flush_payload()
            cur_note = [line]
        elif _is_payload_line(line):
            cur_payload.append(line)
        elif after_blank:
            flush_payload()
            cur_note = [line]
        else:
            flush_payload()
            main_parts.append(line.strip())
            last_main_was_payload = False
        after_blank = False

    flush_payload()
    flush_note()

    main_text = " ".join(" ".join(main_parts).split())
    return {"main_text": main_text, "payloads": payloads, "notes": notes}


# ---------------------------------------------------------------------------
# 3. Drain-style templating
# ---------------------------------------------------------------------------

_DIGIT_CATEGORIES = {"Nd", "No", "Nl"}
_SYMBOL_CATEGORIES = {"Sm", "So", "Ps", "Pe"}


def is_wildcard_token(token: str) -> bool:
    """Tokens treated as parameters at the prefix levels of the parse tree.

    Drain uses "contains digits".  Following the Layer 3 description we also
    treat tokens containing backticks (quoted Lean code) or non-ASCII math
    symbols (``⊢``, ``↦``, ``∈``, ``⟨``, ``✝``, ...) as wildcards.  Non-ASCII
    *letters* (``α``, ``ℕ``) do not make a token a wildcard.
    """
    for ch in token:
        if ch == "`":
            return True
        cat = unicodedata.category(ch)
        if cat in _DIGIT_CATEGORIES:
            return True
        if ord(ch) > 127 and cat in _SYMBOL_CATEGORIES:
            return True
    return False


def tokenize(text: str) -> list[str]:
    tokens = text.split()
    return tokens if tokens else [EMPTY_TOKEN]


@dataclass
class LogGroup:
    """A leaf log group: a template plus the ids of the diagnostics in it."""

    template: list[str]
    ids: list[int] = field(default_factory=list)


@dataclass
class _Node:
    children: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)


class Drain:
    """Fixed-depth parse tree log parser (He et al., ICWS 2017).

    ``depth`` counts the root, the token-count layer, the token-prefix layers
    and the leaf layer, as in the paper; depth 4 therefore means two prefix
    token layers.  ``sim_threshold`` is the paper's ``st``.  ``max_children``
    is the paper's ``maxChild`` safeguard against branch explosion.  The paper
    reports depth = 4 and st = 0.4 as a robust default across data sets, so
    they are the defaults here.
    """

    def __init__(self, depth: int = 4, sim_threshold: float = 0.4, max_children: int = 100):
        if depth < 3:
            raise ValueError("depth must be >= 3 (root, length layer, leaf)")
        if not 0.0 <= sim_threshold <= 1.0:
            raise ValueError("sim_threshold must be in [0, 1]")
        self.depth = depth
        self.sim_threshold = sim_threshold
        self.max_children = max_children
        self.root = _Node()
        self.groups: list[LogGroup] = []

    # -- tree traversal ---------------------------------------------------

    def _leaf(self, tokens: list[str], create: bool) -> _Node | None:
        length = len(tokens)
        node = self.root.children.get(length)
        if node is None:
            if not create:
                return None
            node = self.root.children[length] = _Node()
        prefix_layers = self.depth - 2
        for i in range(min(prefix_layers, length)):
            token = tokens[i]
            key = WILDCARD if is_wildcard_token(token) else token
            child = node.children.get(key)
            if child is None:
                if not create:
                    child = node.children.get(WILDCARD)
                    if child is None:
                        return None
                elif key == WILDCARD:
                    child = node.children[WILDCARD] = _Node()
                elif WILDCARD in node.children:
                    if len(node.children) < self.max_children:
                        child = node.children[key] = _Node()
                    else:
                        child = node.children[WILDCARD]
                else:
                    if len(node.children) + 1 < self.max_children:
                        child = node.children[key] = _Node()
                    else:
                        child = node.children[WILDCARD] = _Node()
            node = child
        return node

    # -- similarity -------------------------------------------------------

    @staticmethod
    def seq_similarity(template: list[str], tokens: list[str]) -> tuple[float, int]:
        """Fraction of positions with identical tokens; wildcards never match.

        Returns ``(similarity, number_of_wildcards)`` like Drain's ``SeqDist``.
        """
        assert len(template) == len(tokens)
        if not template:
            return 1.0, 0
        same = 0
        params = 0
        for a, b in zip(template, tokens):
            if a == WILDCARD:
                params += 1
            elif a == b:
                same += 1
        return same / len(template), params

    def _best_group(self, groups: list[LogGroup], tokens: list[str]) -> LogGroup | None:
        best: LogGroup | None = None
        best_sim = -1.0
        best_params = -1
        for group in groups:
            sim, params = self.seq_similarity(group.template, tokens)
            if sim > best_sim or (sim == best_sim and params > best_params):
                best, best_sim, best_params = group, sim, params
        if best is not None and best_sim >= self.sim_threshold:
            return best
        return None

    @staticmethod
    def merge_template(template: list[str], tokens: list[str]) -> list[str]:
        return [a if a == b else WILDCARD for a, b in zip(template, tokens)]

    # -- public API -------------------------------------------------------

    def add(self, text: str, item_id: int) -> LogGroup:
        """Insert one message; returns the group it was assigned to."""
        tokens = tokenize(text)
        leaf = self._leaf(tokens, create=False)
        group = self._best_group(leaf.groups, tokens) if leaf is not None else None
        if group is None:
            leaf = self._leaf(tokens, create=True)
            assert leaf is not None
            group = LogGroup(template=list(tokens))
            leaf.groups.append(group)
            self.groups.append(group)
        else:
            group.template = self.merge_template(group.template, tokens)
        group.ids.append(item_id)
        return group

    def match(self, text: str) -> LogGroup | None:
        """Find the group a message would join, without inserting it."""
        tokens = tokenize(text)
        leaf = self._leaf(tokens, create=False)
        if leaf is None:
            return None
        return self._best_group(leaf.groups, tokens)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

ORIGINAL_FIELDS = (
    "file",
    "line",
    "column",
    "message",
    "source_found",
    "error_line",
    "code",
    "context_start_line",
    "context_end_line",
    "repo",
    "old_version",
    "new_version",
    "old_hash",
    "new_hash",
    "pair_index",
    "pair_id",
)


def load_diagnostics(errors_root: Path) -> list[dict]:
    """Load all ``*.json`` diagnostics under ``errors_root`` in sorted order."""
    rows = []
    for path in sorted(errors_root.glob("*.json"), key=lambda p: p.name):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            continue
        data["source_file"] = path.name
        rows.append(data)
    return rows


def transition_key(row: dict) -> str:
    return f"{row.get('old_version', '?')} -> {row.get('new_version', '?')}"


def process(rows: Iterable[dict], depth: int = 4, sim_threshold: float = 0.4,
            max_children: int = 100) -> tuple[list[dict], list[dict], dict]:
    """Run Layer 3 over ``rows``; returns (diagnostic rows, template rows, summary)."""
    rows = list(rows)
    drain = Drain(depth=depth, sim_threshold=sim_threshold, max_children=max_children)

    out_rows: list[dict] = []
    for idx, row in enumerate(rows):
        message = row.get("message") or ""
        normalized = normalize_message(message)
        parts = split_message(normalized)
        drain.add(parts["main_text"], idx)
        out = {k: row[k] for k in ORIGINAL_FIELDS if k in row}
        out["source_file"] = row.get("source_file")
        out["normalized_message"] = normalized
        out["main_text"] = parts["main_text"]
        out["payloads"] = parts["payloads"]
        out["notes"] = parts["notes"]
        out_rows.append(out)

    # Deterministic template ids: by descending count, then template text.
    ordered = sorted(drain.groups, key=lambda g: (-len(g.ids), " ".join(g.template)))
    width = max(4, len(str(len(ordered))))
    template_rows: list[dict] = []
    for n, group in enumerate(ordered, start=1):
        template_id = f"T{n:0{width}d}"
        template = " ".join(group.template)
        pairs, repos, transitions = set(), set(), set()
        for i in group.ids:
            out_rows[i]["template_id"] = template_id
            out_rows[i]["template"] = template
            pairs.add(rows[i].get("pair_id"))
            repos.add(rows[i].get("repo"))
            transitions.add(transition_key(rows[i]))
        first = rows[group.ids[0]]
        template_rows.append(
            {
                "template_id": template_id,
                "template": template,
                "count": len(group.ids),
                "n_pairs": len(pairs),
                "n_repos": len(repos),
                "n_transitions": len(transitions),
                "example_message": first.get("message", ""),
                "example_file": first.get("source_file"),
            }
        )

    per_transition: dict[str, dict] = OrderedDict()
    by_transition: dict[str, Counter] = defaultdict(Counter)
    for out in out_rows:
        by_transition[transition_key(out)][out["template_id"]] += 1
    for key in sorted(by_transition):
        counter = by_transition[key]
        per_transition[key] = {
            "diagnostics": sum(counter.values()),
            "templates": len(counter),
            "template_counts": dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        }

    summary = {
        "parameters": {"depth": depth, "sim_threshold": sim_threshold, "max_children": max_children},
        "counts": {
            "diagnostics": len(out_rows),
            "templates": len(template_rows),
            "singletons": sum(1 for t in template_rows if t["count"] == 1),
            "transitions": len(per_transition),
        },
        "top_templates": [
            {"template_id": t["template_id"], "template": t["template"], "count": t["count"]}
            for t in template_rows[:30]
        ],
        "per_transition": per_transition,
    }
    return out_rows, template_rows, summary


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def format_summary(summary: dict, top: int = 15) -> str:
    c = summary["counts"]
    lines = [
        f"diagnostics: {c['diagnostics']}  templates: {c['templates']}  "
        f"singletons: {c['singletons']}  transitions: {c['transitions']}",
        f"drain: depth={summary['parameters']['depth']} "
        f"st={summary['parameters']['sim_threshold']}",
        f"top {top} templates:",
    ]
    for t in summary["top_templates"][:top]:
        template = t["template"]
        if len(template) > 110:
            template = template[:107] + "..."
        lines.append(f"  {t['count']:5d}  {t['template_id']}  {template}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--errors-root", type=Path, default=Path("parsed-errors"),
                        help="directory with one JSON diagnostic per file")
    parser.add_argument("--out-dir", type=Path, default=Path("layer3-out"),
                        help="output directory (created if missing)")
    parser.add_argument("--depth", type=int, default=4,
                        help="Drain parse-tree depth (paper default 4)")
    parser.add_argument("--sim-threshold", type=float, default=0.4,
                        help="Drain similarity threshold st (paper default 0.4)")
    parser.add_argument("--max-children", type=int, default=100,
                        help="Drain maxChild per internal node (default 100)")
    args = parser.parse_args(argv)

    if not args.errors_root.is_dir():
        print(f"error: {args.errors_root} is not a directory", file=sys.stderr)
        return 2
    rows = load_diagnostics(args.errors_root)
    out_rows, template_rows, summary = process(
        rows, depth=args.depth, sim_threshold=args.sim_threshold, max_children=args.max_children
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "normalized-diagnostics.jsonl", out_rows)
    write_jsonl(args.out_dir / "templates.jsonl", template_rows)
    with (args.out_dir / "summary.json").open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(format_summary(summary))
    print(f"wrote {args.out_dir / 'normalized-diagnostics.jsonl'}, "
          f"{args.out_dir / 'templates.jsonl'}, {args.out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

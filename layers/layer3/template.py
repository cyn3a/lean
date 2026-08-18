"""Template induction: many concrete messages -> one parameterized realization.

Why not just cluster on edit distance: Lean prose is short and highly templated, but
the *arguments* embedded in it (identifiers, types) are unbounded. Pure string
clustering either merges genuinely different messages or splits one message into
hundreds. Alignment gives you the holes explicitly, which is what Layer 2 needs to
join against -- a bag of similar strings is not a realization.

Algorithm (a positional variant of Drain, tuned for prose rather than log lines):

  1. Bucket by (severity, error_name, arity, first two literal tokens). Anything with
     a compiler-supplied error_name goes straight to its own bucket -- no induction
     needed, and these buckets double as a labelled validation set for the rest.
  2. Within a bucket, greedily merge each message into the first compatible template
     via difflib alignment. Compatible = literal tokens agree on >= THETA of aligned
     positions and neither side introduces a hole spanning a sentence boundary.
  3. Type each hole from the values that landed in it.

Step 1's labelled subset is the reason to bother: you can measure induction accuracy
against the ~8% of messages Lean already tags, rather than eyeballing it.
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .normalize import payload_shape, scrub, split_message
from .schema import Realization, RawDiagnostic

THETA = 0.72          # min literal agreement to merge a message into a template
MAX_HOLE_TOKENS = 8   # a hole wider than this means we aligned two different messages

_TOKEN = re.compile(r"`[^`]*`|\u2039[a-z]+\u203a|[A-Za-z_][A-Za-z0-9_.'!?]*|\d+|\S")


def tokenize(prose: str) -> list[str]:
    """Backquoted spans are one token: Lean uses them to delimit arguments, which
    makes them the highest-signal hole boundary available."""
    return _TOKEN.findall(prose)


def _hole_kind(values: Iterable[str]) -> str:
    vals = [v.strip() for v in values if v.strip()]
    if not vals:
        return "opaque"
    stripped = [v.strip("`") for v in vals]
    if all(re.fullmatch(r"-?\d+", v) for v in stripped):
        return "numeral"
    if all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.'!?\u2039\u203a]*", v) for v in stripped):
        # Heuristic: capitalized dotted names are usually types/namespaces.
        if all(v[:1].isupper() for v in stripped):
            return "type"
        return "ident"
    if all(v.endswith(".lean") or "/" in v for v in stripped):
        return "path"
    if any(len(v) > 60 or "\n" in v for v in stripped):
        return "term"
    return "opaque"


@dataclass
class _Template:
    tokens: list[str]                                   # literal token or HOLE sentinel
    hole_values: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    count: int = 0

    HOLE = "\u0000HOLE"

    def render(self) -> tuple[str, list[str]]:
        out: list[str] = []
        kinds: list[str] = []
        idx = 0
        for i, t in enumerate(self.tokens):
            if t == self.HOLE:
                out.append("{%d}" % idx)
                kinds.append(_hole_kind(self.hole_values.get(i, [])))
                idx += 1
            else:
                out.append(t)
        return (" ".join(out), kinds)


def _merge(tpl: _Template, toks: list[str]) -> bool:
    """Try to fold `toks` into `tpl`. Mutates and returns True on success."""
    sm = difflib.SequenceMatcher(
        a=[t for t in tpl.tokens], b=toks, autojunk=False)
    blocks = sm.get_matching_blocks()

    matched = sum(b.size for b in blocks)
    literal_positions = sum(1 for t in tpl.tokens if t != _Template.HOLE)
    if literal_positions and matched / max(literal_positions, 1) < THETA:
        return False

    new_tokens: list[str] = []
    new_values: dict[int, list[str]] = defaultdict(list)
    ai = bi = 0
    for blk in blocks:
        a_gap = tpl.tokens[ai:blk.a]
        b_gap = toks[bi:blk.b]
        if a_gap or b_gap:
            if max(len(a_gap), len(b_gap)) > MAX_HOLE_TOKENS:
                return False
            # Preserve any values already collected for holes inside the gap.
            carried: list[str] = []
            for off, t in enumerate(a_gap):
                if t == _Template.HOLE:
                    carried.extend(tpl.hole_values.get(ai + off, []))
            new_values[len(new_tokens)].extend(carried)
            if b_gap:
                new_values[len(new_tokens)].append(" ".join(b_gap))
            new_tokens.append(_Template.HOLE)
        for k in range(blk.size):
            t = tpl.tokens[blk.a + k]
            if t == _Template.HOLE:
                new_values[len(new_tokens)].extend(tpl.hole_values.get(blk.a + k, []))
                new_values[len(new_tokens)].append(toks[blk.b + k])
            new_tokens.append(t)
        ai, bi = blk.a + blk.size, blk.b + blk.size

    if all(t == _Template.HOLE for t in new_tokens):
        return False                                   # degenerate merge

    tpl.tokens = new_tokens
    tpl.hole_values = defaultdict(list, new_values)
    tpl.count += 1
    return True


def _bucket_key(d: RawDiagnostic, prose: str) -> tuple:
    if d.error_name:
        return (d.severity, d.error_name)
    toks = tokenize(prose)
    return (d.severity, None, len(toks) // 4, tuple(toks[:2]))


class TemplateIndex:
    """Fit on a corpus, then assign realizations to new diagnostics."""

    def __init__(self) -> None:
        self._buckets: dict[tuple, list[_Template]] = defaultdict(list)
        self._meta: dict[int, tuple] = {}

    def fit(self, diags: Iterable[RawDiagnostic]) -> "TemplateIndex":
        # Longest-first: a long message merged into a short template loses tokens
        # permanently, so seed each bucket with its most specific member.
        items = []
        for d in diags:
            head, payload, trailers = split_message(scrub(d.text))
            items.append((d, head, payload, trailers))
        items.sort(key=lambda it: -len(it[1]))
        for d, head, payload, trailers in items:
            key = _bucket_key(d, head)
            toks = tokenize(head)
            for tpl in self._buckets[key]:
                if _merge(tpl, toks):
                    break
            else:
                t = _Template(tokens=list(toks), count=1)
                self._buckets[key].append(t)
                self._meta[id(t)] = (d.severity, d.error_name)
        return self

    def assign(self, d: RawDiagnostic) -> Realization:
        head, payload, trailers = split_message(scrub(d.text))
        key = _bucket_key(d, head)
        toks = tokenize(head)
        best: _Template | None = None
        best_ratio = 0.0
        for tpl in self._buckets.get(key, []):
            lits = [t for t in tpl.tokens if t != _Template.HOLE]
            r = difflib.SequenceMatcher(a=lits, b=toks, autojunk=False).ratio()
            if r > best_ratio:
                best, best_ratio = tpl, r
        if best is None or best_ratio < THETA:
            # Unseen shape. Emit a singleton realization rather than forcing a bad
            # match -- a wrong fingerprint is worse than a rare one.
            template, kinds = " ".join(toks), []
        else:
            template, kinds = best.render()
        return Realization(
            template=template,
            hole_kinds=kinds,
            severity=d.severity,
            error_name=d.error_name,
            trailer_kinds=sorted(set(trailers)),
            payload_shape=payload_shape(payload),
        )

    def templates(self) -> list[dict]:
        out = []
        for key, tpls in self._buckets.items():
            for t in tpls:
                template, kinds = t.render()
                sev, name = self._meta.get(id(t), (key[0], None))
                out.append({"template": template, "hole_kinds": kinds,
                            "severity": sev, "error_name": name, "count": t.count})
        return sorted(out, key=lambda r: -r["count"])

    # --- self-evaluation ---------------------------------------------------

    def purity_against_error_names(self, diags: Iterable[RawDiagnostic]) -> dict:
        """Induction accuracy measured on the compiler-labelled subset.

        Drop the error_name, induce templates blind, then check whether messages
        sharing a name landed on one template and messages with different names
        stayed apart. This is the only honest accuracy number available, and it is
        why the labelled subset matters even though it is small.
        """
        labelled = [d for d in diags if d.error_name]
        blind = TemplateIndex().fit(
            [RawDiagnostic(severity=d.severity, text=d.text, file=d.file,
                           error_name=None) for d in labelled]
        )
        by_name: dict[str, set[str]] = defaultdict(set)
        by_tpl: dict[str, set[str]] = defaultdict(set)
        for d in labelled:
            probe = RawDiagnostic(severity=d.severity, text=d.text,
                                  file=d.file, error_name=None)
            fp = blind.assign(probe).fingerprint
            by_name[d.error_name].add(fp)
            by_tpl[fp].add(d.error_name)
        return {
            "labelled_messages": len(labelled),
            "distinct_names": len(by_name),
            "names_split_across_templates": sum(1 for v in by_name.values() if len(v) > 1),
            "templates_mixing_names": sum(1 for v in by_tpl.values() if len(v) > 1),
        }

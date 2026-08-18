"""Reversal: turn mined hunks into reusable breakage rules.

The premise of layer 4 is that a forward adaptation diff, read backwards, is a
generator: `post -> pre` rewrites *working* code into code that is known to
fail under the new toolchain, with the repair already known. One mined hunk
with support N therefore yields not N training pairs but an unbounded supply,
since the rule can be applied at any matching site in a current checkout.

Two rule strengths:

* ``substitution`` -- a consistent 1-for-1 token mapping. Safely reversible and
  applicable anywhere the token occurs. High yield.
* ``window``       -- a literal pre/post text window. Only applicable where the
  exact post text occurs. Low yield, high fidelity.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import leanlex
from .taxonomy import RepairLabel

#: Renames that are unsafe to reverse blindly: reversing them would rewrite
#: tokens that legitimately still exist under the new toolchain.
_UNSAFE_REVERSE = {"rfl", "trivial", "simp", "exact", "sorry", "this", "_"}


def _boundary(name: str) -> re.Pattern:
    """Match `name` as a whole dotted identifier.

    The trailing `(?!\.[A-Z])` is load-bearing. Without it a rule for
    `Mathlib.Tactic` fires inside `Mathlib.Tactic.Common` and silently
    corrupts an unrelated import. A *lowercase* next component is kept
    matchable, because `eqRec_heq_iff_heq.mp` really should be rewritten by a
    rule targeting `eqRec_heq_iff_heq` -- `.mp` is a projection, not a
    namespace. Lean's UpperCamel-namespace convention separates the two.
    """
    return re.compile(rf"(?<![\w.'!?]){re.escape(name)}(?![\w'!?])(?!\.[A-Z])")


@dataclass
class RepairRule:
    rule_id: str
    kind: str                      # "substitution" | "window"
    label: str
    #: forward = repair (broken -> fixed); reverse = breakage synthesis
    forward: dict
    support: int = 0
    files: int = 0
    windows: list[str] = field(default_factory=list)
    toolchains: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reversible: bool = True

    def to_json(self) -> dict:
        d = asdict(self)
        d["windows"] = self.windows[:3]
        d["provenance"] = self.provenance[:5]
        return d

    # ---------------------------------------------------------- application

    def apply_reverse(self, text: str, limit: int | None = None) -> tuple[str, int]:
        """Rewrite *working* text into *broken* text. Returns (text, n_sites)."""
        if not self.reversible:
            return text, 0
        if self.kind == "substitution":
            new, old = self.forward["from"], self.forward["to"]
            pat = _boundary(old)
            if limit is None:
                out, n = pat.subn(new, text)
            else:
                out, n = pat.subn(new, text, count=limit)
            return out, n
        pre, post = self.forward["pre"], self.forward["post"]
        if post not in text:
            return text, 0
        n = text.count(post) if limit is None else min(limit, text.count(post))
        return text.replace(post, pre, n), n

    def apply_forward(self, text: str) -> tuple[str, int]:
        """The repair itself, for round-trip validation."""
        if self.kind == "substitution":
            old, new = self.forward["from"], self.forward["to"]
            return _boundary(old).subn(new, text)
        pre, post = self.forward["pre"], self.forward["post"]
        return text.replace(pre, post), text.count(pre)


def _rid(kind: str, key: str) -> str:
    return f"{kind[:4]}-{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def induce(pairs: list[dict], min_support: int = 1) -> list[RepairRule]:
    """Build rules from mined pairs (as emitted by `mine.py`)."""
    subs: dict[tuple[str, str], RepairRule] = {}
    wins: dict[tuple[str, str], RepairRule] = {}
    sub_files: dict[tuple[str, str], set[str]] = defaultdict(set)
    win_files: dict[tuple[str, str], set[str]] = defaultdict(set)

    for p in pairs:
        label = p.get("label", RepairLabel.UNKNOWN)
        toolchain = p.get("toolchain_after", "")
        prov = p.get("commit", "")
        notes = p.get("evidence", {}).get("notes", [])
        pairs_subs = p.get("evidence", {}).get("substitutions", [])

        for a, b in pairs_subs:
            if a in _UNSAFE_REVERSE or b in _UNSAFE_REVERSE:
                continue
            if not (leanlex.is_identifier(a) and leanlex.is_identifier(b)):
                continue
            key = (a, b)
            r = subs.get(key)
            if r is None:
                r = subs[key] = RepairRule(
                    rule_id=_rid("substitution", f"{a}=>{b}"),
                    kind="substitution", label=label,
                    forward={"from": a, "to": b},
                )
            r.support += 1
            sub_files[key].add(p.get("path", ""))
            if toolchain and toolchain not in r.toolchains:
                r.toolchains.append(toolchain)
            if prov and prov not in r.provenance:
                r.provenance.append(prov)
            for n in notes:
                if n not in r.notes:
                    r.notes.append(n)
            if len(r.windows) < 3:
                r.windows.append(p.get("fixed", "")[:400])

        pre = leanlex.normalize(p.get("broken", ""), drop_comments=False)
        post = leanlex.normalize(p.get("fixed", ""), drop_comments=False)
        if pre and post and pre != post and len(post) < 600:
            key = (p.get("broken", ""), p.get("fixed", ""))
            r = wins.get(key)
            if r is None:
                r = wins[key] = RepairRule(
                    rule_id=_rid("window", pre + "|" + post),
                    kind="window", label=label,
                    forward={"pre": key[0], "post": key[1]},
                    reversible=bool(key[0].strip()),
                )
            r.support += 1
            win_files[key].add(p.get("path", ""))
            if toolchain and toolchain not in r.toolchains:
                r.toolchains.append(toolchain)
            if prov and prov not in r.provenance:
                r.provenance.append(prov)

    for key, r in subs.items():
        r.files = len(sub_files[key])
    for key, r in wins.items():
        r.files = len(win_files[key])

    out = [r for r in (*subs.values(), *wins.values()) if r.support >= min_support]
    out.sort(key=lambda r: (-r.support, r.rule_id))
    return out


def load_rules(path: str | Path) -> list[RepairRule]:
    rules = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rules.append(RepairRule(**json.loads(line)))
    return rules


def save_rules(rules: list[RepairRule], path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8") as fh:
        for r in rules:
            fh.write(json.dumps(r.to_json(), ensure_ascii=False) + "\n")


# --------------------------------------------------------------- synthesis

@dataclass
class SyntheticBreak:
    sample_id: str
    rule_id: str
    label: str
    path: str
    broken: str
    fixed: str
    sites: int
    toolchain: str
    origin: str = "synthetic"


def synthesize(rules: list[RepairRule], files: dict[str, str], *,
               n: int = 500, max_per_rule: int = 20, sites_per_file: int = 1,
               seed: int = 0, toolchain: str = "") -> list[SyntheticBreak]:
    """Apply reverse rules to clean files to manufacture broken/fixed pairs.

    `files` maps path -> current (compiling) contents. Only substitution rules
    generalise across files; window rules are applied where they match.
    """
    rng = random.Random(seed)
    paths = list(files)
    rng.shuffle(paths)
    out: list[SyntheticBreak] = []
    per_rule: Counter = Counter()

    ordered = sorted(rules, key=lambda r: -r.support)
    for path in paths:
        text = files[path]
        for r in ordered:
            if len(out) >= n:
                return out
            if per_rule[r.rule_id] >= max_per_rule or not r.reversible:
                continue
            broken, sites = r.apply_reverse(text, limit=sites_per_file)
            if sites == 0 or broken == text:
                continue
            # round-trip check: the forward repair must restore the original
            restored, _ = r.apply_forward(broken)
            if restored != text:
                continue
            per_rule[r.rule_id] += 1
            out.append(SyntheticBreak(
                sample_id=_rid("syn", f"{path}|{r.rule_id}|{sites}"),
                rule_id=r.rule_id, label=r.label, path=path,
                broken=broken, fixed=text, sites=sites,
                toolchain=toolchain or (r.toolchains[0] if r.toolchains else ""),
            ))
    return out

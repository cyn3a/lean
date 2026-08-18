"""Canonical records for Layer 3.

Three record types, in pipeline order:

    RawDiagnostic   what a driver observed, verbatim
    Realization     a normalized + templated surface form, with a stable fingerprint
    Observation     one (Realization, provenance, stratum) triple -- the unit of counting

Everything is JSON-serializable. Nothing here imports a driver or a corpus, so this
module is safe to depend on from Layer 1/2 code.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Severity = Literal["error", "warning", "information", "trace", "unknown"]

# Which corpus a datum came from. This distinction is load-bearing: see README
# section "Corpus roles". Strata differ in *what they are evidence for*.
Stratum = Literal[
    "test_expected",       # lean4 tests/**/*.out.expected -- realization evidence only
    "test_drift",          # same, diffed across toolchain tags -- drift evidence only
    "reservoir_head",      # Reservoir package built at its pinned toolchain
    "reservoir_bump",      # Reservoir package built at a *newer* toolchain
    "mathlib_head",        # mathlib at its pinned toolchain
    "mathlib_bump",        # mathlib commit built at a non-pinned toolchain
]

FREQUENCY_STRATA: tuple[Stratum, ...] = (
    "reservoir_head",
    "reservoir_bump",
    "mathlib_head",
    "mathlib_bump",
)


@dataclass(frozen=True)
class Position:
    line: int          # 0-based, LSP convention
    column: int        # 0-based, UTF-16 code units in LSP; codepoints in `lean --json`

    def as_tuple(self) -> tuple[int, int]:
        return (self.line, self.column)


@dataclass
class RawDiagnostic:
    """Exactly what came off the wire, before any normalization."""
    severity: Severity
    text: str                              # full rendered message body
    file: str
    start: Optional[Position] = None
    end: Optional[Position] = None
    # Lean tags a growing subset of messages with a stable identifier, rendered as
    # `error(lean.unknownIdentifier):`. When present this is ground truth and beats
    # anything template induction can infer. Coverage is currently partial -- see
    # data/observed_error_names.json.
    error_name: Optional[str] = None
    source: str = ""                       # "lean --json" | "lsp" | "out.expected"
    toolchain: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Realization:
    """A normalized surface form: prose template + typed holes.

    `fingerprint` is the join key against Layer 2. Two diagnostics share a
    fingerprint iff they are the same message constructor as far as we can tell
    from outside the compiler.
    """
    template: str                          # prose with {0}, {1}, ... hole markers
    hole_kinds: list[str]                  # per-hole: ident|term|type|numeral|path|opaque
    severity: Severity
    error_name: Optional[str]
    # Structured trailers Lean appends, e.g. "Hint:" / "Note:" blocks. Kept separate
    # because they vary independently of the head message and would otherwise
    # shatter the template space.
    trailer_kinds: list[str] = field(default_factory=list)
    payload_shape: str = ""                # coarse shape of the indented term block

    @property
    def fingerprint(self) -> str:
        if self.error_name:
            # Trust the compiler-supplied identifier; still salt with severity so a
            # message promoted warning->error is visibly a different realization.
            basis = f"name:{self.error_name}|{self.severity}"
        else:
            basis = "tpl:" + json.dumps(
                [self.template, self.hole_kinds, self.severity,
                 self.trailer_kinds, self.payload_shape],
                sort_keys=True, ensure_ascii=False,
            )
        return hashlib.blake2b(basis.encode("utf-8"), digest_size=10).hexdigest()

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["fingerprint"] = self.fingerprint
        return d


@dataclass
class Observation:
    realization: Realization
    stratum: Stratum
    toolchain: str
    # Clustering unit for the bootstrap. Diagnostics inside one file are heavily
    # correlated (one bad import yields fifty errors), so the resampling unit is the
    # file, never the individual diagnostic.
    cluster: str                           # usually "<package>:<relative path>"
    package: str = ""
    is_root: bool = True                   # False if collapsed as a cascade descendant
    start: Optional[Position] = None
    end: Optional[Position] = None         # required for cascade containment
    raw_text: str = ""

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["realization"]["fingerprint"] = self.realization.fingerprint
        return d


@dataclass
class DriftEvent:
    """One expected-output message changing between two toolchain tags."""
    path: str
    from_tag: str
    to_tag: str
    kind: Literal["added", "removed", "retext", "retag", "reseverity", "moved"]
    before: Optional[str] = None
    after: Optional[str] = None
    before_name: Optional[str] = None
    after_name: Optional[str] = None
    similarity: float = 0.0


def dumps(records: list[Any]) -> str:
    """NDJSON. Every stage of the pipeline writes this and only this."""
    out = []
    for r in records:
        if hasattr(r, "to_json"):
            out.append(json.dumps(r.to_json(), ensure_ascii=False, sort_keys=True))
        else:
            out.append(json.dumps(dataclasses.asdict(r), ensure_ascii=False, sort_keys=True))
    return "\n".join(out) + "\n"

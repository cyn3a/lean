"""The seam between Layer 3 and Layers 1-2.

I do not have your Layer 1/2 schemas, so this is a declared contract rather than an
implementation. Layer 3 produces fingerprinted realizations with frequency weights;
Layers 1-2 presumably produce a taxonomy of causes and the emission sites that
realize them. The join is many-to-many in both directions and pretending otherwise
is where this kind of pipeline usually goes wrong:

  * one Layer-2 node emits several realizations (a message that varies its prose by
    the shape of its argument, or that gained a `Hint:` trailer in some release)
  * one realization is emitted by several Layer-2 nodes (`unknown identifier` is
    raised from at least the elaborator, the delaborator, and dot-notation
    resolution -- same text, different cause)

So the binding is an explicit edge list with a confidence, not a dict lookup. Fill in
`resolve` for your schema; everything downstream consumes `BindingEdge`.

Three sources of edges, in decreasing order of trust:

  1. error_name. Where Lean tags the message, the tag *is* the join key. Measured
     coverage on lean4 master: 8.1% of test-suite messages, 10 distinct names. Small,
     but exact, and growing with each release -- the drift log will tell you how fast.
  2. Declared mapping. A hand-written table from Layer-2 node to template. Expensive,
     accurate, goes stale; version it alongside the toolchain matrix.
  3. Source-position attribution. If you can build a toolchain with message
     construction sites instrumented, the emitting site is recoverable and the join
     becomes exact. This is the only route to full coverage.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol

from .schema import Realization


@dataclass
class BindingEdge:
    fingerprint: str
    layer2_id: str
    confidence: float          # 1.0 for error_name, lower for declared/inferred
    basis: str                 # "error_name" | "declared" | "instrumented" | "manual"
    note: str = ""


class Layer2Index(Protocol):
    """Whatever your Layer 2 exposes. Only two operations are needed."""

    def by_error_name(self, name: str) -> list[str]: ...
    def all_ids(self) -> Iterable[str]: ...


class DeclaredMapping:
    """A versioned, hand-maintained fingerprint -> layer2_id table.

    Stored as NDJSON so it diffs cleanly in review, keyed by fingerprint so it
    survives template churn only as far as the fingerprint does -- when a message is
    rewritten upstream the fingerprint changes and the entry goes stale *loudly*
    rather than silently mapping to the wrong node. Cross-reference the drift log to
    find which entries a toolchain bump invalidated.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self._map: dict[str, list[BindingEdge]] = {}
        if path and path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    d = json.loads(line)
                    self._map.setdefault(d["fingerprint"], []).append(BindingEdge(**d))

    def get(self, fingerprint: str) -> list[BindingEdge]:
        return list(self._map.get(fingerprint, []))

    def stale_against(self, live_fingerprints: set[str]) -> list[str]:
        """Entries whose fingerprint no longer occurs. Run this after every bump."""
        return sorted(fp for fp in self._map if fp not in live_fingerprints)


def resolve(realizations: Iterable[Realization],
            layer2: Optional[Layer2Index] = None,
            declared: Optional[DeclaredMapping] = None,
            instrumented: Optional[Callable[[Realization], list[str]]] = None,
            ) -> list[BindingEdge]:
    """Build the edge list. Every realization gets zero or more edges.

    Realizations with zero edges are the interesting output: they are either a Layer-2
    gap or a Layer-3 induction artefact, and the ones with high frequency weight are
    worth reading by hand before anything else in this pipeline.
    """
    edges: list[BindingEdge] = []
    for r in realizations:
        fp = r.fingerprint
        if instrumented is not None:
            for nid in instrumented(r):
                edges.append(BindingEdge(fp, nid, 1.0, "instrumented"))
                continue
        if r.error_name and layer2 is not None:
            for nid in layer2.by_error_name(r.error_name):
                edges.append(BindingEdge(fp, nid, 1.0, "error_name",
                                         note=r.error_name))
        if declared is not None:
            edges.extend(declared.get(fp))
    return edges


def unbound(realizations: Iterable[Realization],
            edges: Iterable[BindingEdge]) -> list[Realization]:
    bound = {e.fingerprint for e in edges}
    return [r for r in realizations if r.fingerprint not in bound]

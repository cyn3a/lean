"""Turning observations into frequency weights, without lying about what they mean.

Three problems stand between a pile of diagnostics and a defensible weight.

CASCADE. One root error yields many downstream diagnostics: a failed import poisons
every declaration below it, a type error inside a tactic block produces the error
plus an unsolved-goals plus a `declaration uses sorry`. Counting raw diagnostics
measures the compiler's error-recovery behaviour, not the frequency of causes. We
collapse to roots and report both numbers, because which one you want depends on the
question -- "what does a user see" wants raw, "what went wrong" wants roots.

CLUSTERING. Diagnostics within a file are not independent draws. Any interval
computed as if they were will be far too narrow. The resampling unit is the file.

STRATUM MIXING. The corpora are not samples from one population and must not be
pooled by concatenation. The test suite in particular is *adversarially enriched*:
it exists to cover rare paths, so its message distribution is close to the inverse of
the field distribution. Its weight in the frequency estimate is zero by default.
Mixing weights are analyst-declared priors, stated in the output, not inferred from
sample sizes.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .schema import FREQUENCY_STRATA, Observation, Stratum

# Messages that are structurally consequences of another diagnostic in the same file.
# Matched against the induced template, not raw text.
_CASCADE_PRONE = (
    "declaration uses `sorry`",
    "unsolved goals",
    "unknown identifier",      # only when downstream of a failed import/def
)


@dataclass
class StratumPrior:
    """Analyst-declared share of the target population.

    There is no way to estimate these from the corpora themselves -- the corpora are
    the sample, not the population. State them, vary them, and report sensitivity.
    """
    weights: dict[Stratum, float]

    def __post_init__(self) -> None:
        active = {k: v for k, v in self.weights.items() if v > 0}
        total = sum(active.values())
        if not math.isclose(total, 1.0, rel_tol=1e-6):
            raise ValueError(f"stratum priors must sum to 1, got {total:.6f}")
        for k in active:
            if k not in FREQUENCY_STRATA:
                raise ValueError(
                    f"{k!r} carries no frequency evidence; see README 'Corpus roles'")

    @classmethod
    def default(cls) -> "StratumPrior":
        # A defensible starting point, not a measurement. mathlib_head and
        # reservoir_head dominate because they represent code as it is written;
        # the _bump strata represent migration pressure, which is real but bursty.
        return cls({
            "reservoir_head": 0.40,
            "mathlib_head": 0.30,
            "reservoir_bump": 0.20,
            "mathlib_bump": 0.10,
        })


# --- cascade collapse ------------------------------------------------------

def collapse_cascades(obs: Sequence[Observation]) -> list[Observation]:
    """Mark non-root diagnostics in place and return the same list.

    Rules, in order of confidence:
      1. A diagnostic whose range is strictly contained in an earlier *error* in the
         same file is a descendant.
      2. A warning from `_CASCADE_PRONE` positioned after the first error in its file
         is a descendant.
      3. Repeated identical fingerprints at the same start position collapse to one.
    """
    by_file: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_file[o.cluster].append(o)

    for _, group in by_file.items():
        group.sort(key=lambda o: (o.start.as_tuple() if o.start else (0, 0)))
        first_error_at: tuple[int, int] | None = None
        error_spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
        seen: set[tuple[str, tuple[int, int]]] = set()

        for o in group:
            pos = o.start.as_tuple() if o.start else (0, 0)
            key = (o.realization.fingerprint, pos)
            if key in seen:
                o.is_root = False
                continue
            seen.add(key)

            for (s, e) in error_spans:
                if s < pos < e:
                    o.is_root = False
                    break
            if not o.is_root:
                continue

            tpl = o.realization.template
            if (first_error_at is not None and pos > first_error_at
                    and o.realization.severity == "warning"
                    and any(p in tpl for p in _CASCADE_PRONE)):
                o.is_root = False
                continue

            if o.realization.severity == "error":
                if first_error_at is None:
                    first_error_at = pos
                if o.start and o.end:
                    error_spans.append((o.start.as_tuple(), o.end.as_tuple()))
    return list(obs)


# --- estimation ------------------------------------------------------------

@dataclass
class WeightRow:
    fingerprint: str
    template: str
    severity: str
    error_name: str | None
    weight: float                       # stratified share, sums to 1 across rows
    ci_low: float
    ci_high: float
    raw_count: int
    root_count: int
    clusters: int                       # files it appeared in -- the real sample size
    per_stratum: dict[str, float] = field(default_factory=dict)


def _stratum_shares(obs: Iterable[Observation], roots_only: bool) -> tuple[
        dict[Stratum, Counter], dict[Stratum, int]]:
    counts: dict[Stratum, Counter] = defaultdict(Counter)
    totals: dict[Stratum, int] = defaultdict(int)
    for o in obs:
        if roots_only and not o.is_root:
            continue
        if o.stratum not in FREQUENCY_STRATA:
            continue
        counts[o.stratum][o.realization.fingerprint] += 1
        totals[o.stratum] += 1
    return counts, totals


def _point_estimate(counts, totals, prior: StratumPrior) -> dict[str, float]:
    """Horvitz-Thompson style: within-stratum share, mixed by declared prior.

    Strata with no observations have their mass redistributed proportionally over the
    strata that do, so a missing corpus shrinks the evidence base rather than
    silently zeroing part of the distribution.
    """
    live = {s: w for s, w in prior.weights.items() if w > 0 and totals.get(s, 0) > 0}
    if not live:
        return {}
    norm = sum(live.values())
    out: Counter = Counter()
    for s, w in live.items():
        share = w / norm
        n = totals[s]
        for fp, c in counts[s].items():
            out[fp] += share * (c / n)
    return dict(out)


def estimate(obs: Sequence[Observation], prior: StratumPrior | None = None,
             roots_only: bool = True, bootstrap: int = 400,
             seed: int = 0) -> list[WeightRow]:
    prior = prior or StratumPrior.default()
    counts, totals = _stratum_shares(obs, roots_only)
    point = _point_estimate(counts, totals, prior)

    # Clustered bootstrap: resample whole files within each stratum.
    clusters_by_stratum: dict[Stratum, list[list[Observation]]] = defaultdict(list)
    grouped: dict[tuple[Stratum, str], list[Observation]] = defaultdict(list)
    for o in obs:
        if roots_only and not o.is_root:
            continue
        if o.stratum in FREQUENCY_STRATA:
            grouped[(o.stratum, o.cluster)].append(o)
    for (s, _), members in grouped.items():
        clusters_by_stratum[s].append(members)

    rng = random.Random(seed)
    draws: dict[str, list[float]] = defaultdict(list)
    for _ in range(max(bootstrap, 0)):
        bc: dict[Stratum, Counter] = defaultdict(Counter)
        bt: dict[Stratum, int] = defaultdict(int)
        for s, cl in clusters_by_stratum.items():
            if not cl:
                continue
            for _ in range(len(cl)):
                for o in cl[rng.randrange(len(cl))]:
                    bc[s][o.realization.fingerprint] += 1
                    bt[s] += 1
        est = _point_estimate(bc, bt, prior)
        for fp in point:
            draws[fp].append(est.get(fp, 0.0))

    meta: dict[str, Observation] = {}
    raw: Counter = Counter()
    root: Counter = Counter()
    cl_count: dict[str, set] = defaultdict(set)
    for o in obs:
        fp = o.realization.fingerprint
        meta.setdefault(fp, o)
        raw[fp] += 1
        if o.is_root:
            root[fp] += 1
        cl_count[fp].add((o.stratum, o.cluster))

    rows: list[WeightRow] = []
    for fp, w in sorted(point.items(), key=lambda kv: -kv[1]):
        d = sorted(draws.get(fp, []))
        lo = d[int(0.025 * len(d))] if d else w
        hi = d[int(0.975 * (len(d) - 1))] if d else w
        r = meta[fp].realization
        rows.append(WeightRow(
            fingerprint=fp, template=r.template, severity=r.severity,
            error_name=r.error_name, weight=w, ci_low=lo, ci_high=hi,
            raw_count=raw[fp], root_count=root[fp], clusters=len(cl_count[fp]),
            per_stratum={s: (counts[s][fp] / totals[s]) if totals.get(s) else 0.0
                         for s in FREQUENCY_STRATA},
        ))
    return rows


def sensitivity(obs: Sequence[Observation], priors: dict[str, StratumPrior],
                top: int = 25) -> dict:
    """How much does the ranking move if the declared priors were wrong?

    Report this next to the weights. A weight that flips rank under a plausible
    reweighting is not a finding.
    """
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    for label, p in priors.items():
        for i, row in enumerate(estimate(obs, p, bootstrap=0)[:top]):
            ranks[row.fingerprint][label] = i
    return {
        fp: {"ranks": r,
             "spread": (max(r.values()) - min(r.values())) if len(r) > 1 else 0}
        for fp, r in ranks.items()
    }

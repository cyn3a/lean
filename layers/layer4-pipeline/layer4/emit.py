"""Dedup, rebalance, split, write.

Two hazards this module exists to handle, both measured on the real
v4.33.0-rc1 bump commit (2433 files, 7347 hunks):

1. **Class collapse.** 6052 of 8246 added lines in that single bump were the
   same mechanical `set_option backward.isDefEq.respectTransparency[.types]
   false in` insertion, and 807 more were `@[implicit_reducible]` ->
   `@[instance_reducible]`. Emitted raw, ~75% of the dataset is one label and
   a model trained on it learns to insert a `set_option`. `cap_per_label` /
   `cap_per_signature` fix this.

2. **Leakage.** The same rename appears in hundreds of hunks across hundreds
   of files. A random split puts near-duplicates on both sides. Default split
   is by toolchain window, which also matches deployment: train on past
   bumps, evaluate on the newest one.
"""

from __future__ import annotations

import json
import hashlib
import random
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import leanlex


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "to_json"):
        return obj.to_json()
    raise TypeError(type(obj))


def signature(p: dict) -> str:
    """Edit-shape identity: what makes two pairs redundant for training."""
    key = "\x00".join([
        p.get("label", ""),
        leanlex.normalize(p.get("broken", "")),
        leanlex.normalize(p.get("fixed", "")),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def dedup(pairs: list[dict], keep_per_signature: int = 1) -> tuple[list[dict], Counter]:
    """Collapse identical edit shapes.

    `keep_per_signature > 1` retains that many *contextually distinct* copies
    (different declaration, then different file). Worth raising when the
    downstream model conditions on surrounding code: the 6362 identical
    `set_option backward.isDefEq...` insertions in the v4.33.0-rc1 bump are one
    edit but 6362 different proof contexts.
    """
    kept: dict[str, list[dict]] = defaultdict(list)
    counts: Counter = Counter()
    ctx_seen: dict[str, set] = defaultdict(set)
    for p in pairs:
        sig = signature(p)
        counts[sig] += 1
        if len(kept[sig]) >= keep_per_signature:
            continue
        ctx = p.get("decl") or p.get("path") or ""
        if ctx in ctx_seen[sig] and keep_per_signature > 1:
            continue
        ctx_seen[sig].add(ctx)
        row = dict(p)
        row["signature"] = sig
        kept[sig].append(row)
    out = [r for rows in kept.values() for r in rows]
    for r in out:
        r["duplicate_count"] = counts[r["signature"]]
    return out, counts


def rebalance(pairs: list[dict], *, cap_per_label: int | None = None,
              cap_per_signature: int | None = None, seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    out = list(pairs)
    if cap_per_signature:
        by_sig: dict[str, list[dict]] = defaultdict(list)
        for p in out:
            by_sig[p.get("signature") or signature(p)].append(p)
        out = []
        for group in by_sig.values():
            rng.shuffle(group)
            out.extend(group[:cap_per_signature])
    if cap_per_label:
        by_label: dict[str, list[dict]] = defaultdict(list)
        for p in out:
            by_label[p.get("label", "")].append(p)
        out = []
        for group in by_label.values():
            rng.shuffle(group)
            # prefer high-confidence, distinct declarations
            group.sort(key=lambda q: (-q.get("confidence", 0), q.get("decl") or ""))
            out.extend(group[:cap_per_label])
    rng.shuffle(out)
    return out


SPLIT_KEYS = {
    "toolchain": lambda p: p.get("toolchain_after") or p.get("window_key", ""),
    "window": lambda p: p.get("window_key", ""),
    "commit": lambda p: p.get("commit", ""),
    "file": lambda p: p.get("path", ""),
    "decl": lambda p: p.get("decl") or p.get("path", ""),
    "signature": lambda p: p.get("signature") or signature(p),
}


#: Coarse -> fine. If the requested key yields too few groups to fill three
#: buckets, `split` walks down this chain rather than silently emitting empty
#: val/test files.
SPLIT_FALLBACK = ["toolchain", "window", "commit", "decl", "file", "signature"]


def split(pairs: list[dict], *, by: str = "toolchain",
          ratios=(0.8, 0.1, 0.1), seed: int = 0,
          allow_fallback: bool = True) -> tuple[dict[str, list[dict]], str]:
    """Group-disjoint split. Returns (buckets, key_actually_used).

    `toolchain` additionally orders groups by date so the test set is the
    *newest* window -- a forward-in-time evaluation matching deployment.
    Mining a single bump gives one toolchain group, which cannot fill three
    buckets; rather than write empty splits, fall back to a finer key.
    """
    if by not in SPLIT_KEYS:
        raise ValueError(f"unknown split key: {by}")
    if allow_fallback:
        chain = SPLIT_FALLBACK[SPLIT_FALLBACK.index(by):] if by in SPLIT_FALLBACK else [by]
        for candidate in chain:
            if len({SPLIT_KEYS[candidate](p) for p in pairs}) >= 3:
                by = candidate
                break
    keyfn = SPLIT_KEYS[by]
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        groups[keyfn(p)].append(p)

    names = list(groups)
    if by in ("toolchain", "window", "commit"):
        names.sort(key=lambda g: max((p.get("date", "") for p in groups[g]), default=""))
    else:
        random.Random(seed).shuffle(names)

    total = sum(len(groups[g]) for g in names)
    want = [r * total for r in ratios]
    buckets = {"train": [], "val": [], "test": []}
    # fill train first (oldest), then val, then test (newest)
    order = ["train", "val", "test"]
    bi, acc = 0, 0.0
    for g in names:
        if bi < 2 and acc >= want[bi]:
            acc = 0.0
            bi += 1
        buckets[order[bi]].extend(groups[g])
        acc += len(groups[g])
    return buckets, by


def label_report(pairs: list[dict]) -> dict:
    labels = Counter(p.get("label", "") for p in pairs)
    total = sum(labels.values()) or 1
    return {
        "total": total,
        "distinct_labels": len(labels),
        "by_label": [
            {"label": lb, "n": n, "pct": round(100 * n / total, 2)}
            for lb, n in labels.most_common()
        ],
        "top_label_share": round(100 * labels.most_common(1)[0][1] / total, 2)
        if labels else 0.0,
        "by_window": dict(Counter(p.get("window_key", "") for p in pairs).most_common(20)),
        "distinct_files": len({p.get("path", "") for p in pairs}),
        "distinct_decls": len({p.get("decl") for p in pairs if p.get("decl")}),
    }


def write_jsonl(rows, path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(_as_dict(r), ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def write_manifest(path: str | Path, **sections) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(sections, indent=2, ensure_ascii=False), encoding="utf-8")

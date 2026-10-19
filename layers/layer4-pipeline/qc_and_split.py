#!/usr/bin/env python3
"""Section 5.2 of "Lean Proof Repair Assistant": quality control and data splits.

Input: a chat-JSONL corpus (diagnostic corpus from build_finetuning_dataset.py or the
repair corpus from build_repair_corpus.py).  Every row's user message is a JSON request
with at least: repo, old_version, new_version, old_hash, new_hash, pair_id, file, line,
error_line, code, context_start_line, source_found.

Quality control (paper, Section 5.2), applied to every example in order:
  1. toolchain-concrete   old/new versions are concrete, immutable toolchains
                          (``leanprover/lean4:[v]X.Y.Z[-rcN]`` or a *dated* nightly), not
                          moving aliases such as ``stable`` or an undated ``nightly``.
  2. native-old / native-new
                          both commits compile in their own environments (``native_checks``
                          rows with status ``passed`` in state/progress.sqlite3, read-only).
  3. lean-diagnostic      the adapted failure contains at least one Lean diagnostic with a
                          non-empty message (not only an infrastructure failure).
  4. context-matches      the code context really contains the compiler-reported line:
                          ``code`` split at ``context_start_line`` yields ``error_line`` at
                          ``line``.
  5. repair-verified      (repair corpus only) the minimized repair compiled in the target
                          environment (``verified`` is true in the request metadata).

Duplicates and near-identical variants are grouped so they never cross the train /
validation / test boundary: examples are linked when they share a repository, when two
repositories share the same (old_hash, new_hash) commit pair (forks/mirrors), or when they
have identical normalized content (file basename, stripped error line, stripped answer).
Connected components of that graph are the split groups.

Splits:
  * repo-grouped train/validation/test (groups never split);
  * an additional held-out test split by *version transition* (old->new toolchain) so
    generalization to an unseen toolchain transition can be measured.

The split fractions and the seed are parameters (the paper does not fix them).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "state" / "progress.sqlite3"

CONCRETE_RE = re.compile(
    r"^leanprover/lean4:(v?\d+\.\d+\.\d+(-rc\d+)?|nightly-\d{4}-\d{2}-\d{2})$"
)


# ----------------------------------------------------------------------------- loading


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            msgs = row["messages"]
            if [m["role"] for m in msgs] != ["system", "user", "assistant"]:
                raise ValueError(f"{path}:{line_no}: expected system/user/assistant")
            request = json.loads(msgs[1]["content"])
            try:
                answer = json.loads(msgs[2]["content"])
            except json.JSONDecodeError:
                answer = {"raw": msgs[2]["content"]}
            rows.append({"idx": len(rows), "row": row, "request": request, "answer": answer})
    return rows


def load_native_passed(db: Path) -> set[tuple[str, str]] | None:
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {
            (repo, commit)
            for repo, commit in con.execute(
                "SELECT repo, commit_hash FROM native_checks WHERE status='passed'"
            )
        }
    finally:
        con.close()


# ----------------------------------------------------------------------------- QC


def context_matches(request: dict) -> bool:
    if not request.get("source_found", True):
        return True  # nothing to check against; the compiler location is still recorded
    code = request.get("code")
    start = request.get("context_start_line")
    line = request.get("line")
    error_line = request.get("error_line")
    if code is None or start is None or line is None or error_line is None:
        return False
    lines = code.split("\n")
    offset = line - start
    return 0 <= offset < len(lines) and lines[offset] == error_line


def lean_diagnostics(example: dict) -> list[dict]:
    answer = example["answer"]
    if isinstance(answer, dict) and isinstance(answer.get("diagnostics"), list):
        return [
            d
            for d in answer["diagnostics"]
            if isinstance(d, dict) and str(d.get("message", "")).strip()
        ]
    request = example["request"]
    if isinstance(request.get("diagnostics"), list):  # repair corpus: diagnostics are input
        return [
            d
            for d in request["diagnostics"]
            if isinstance(d, dict) and str(d.get("message", "")).strip()
        ]
    return []


def qc_reasons(example: dict, native_passed: set | None, corpus: str) -> list[str]:
    request = example["request"]
    reasons = []
    for key in ("old_version", "new_version"):
        if not CONCRETE_RE.match(str(request.get(key, ""))):
            reasons.append(f"toolchain-not-concrete:{key}")
    if native_passed is not None:
        for key, name in (("old_hash", "native-old"), ("new_hash", "native-new")):
            if (request.get("repo"), request.get(key)) not in native_passed:
                reasons.append(f"{name}-not-passed")
    if not lean_diagnostics(example):
        reasons.append("no-lean-diagnostic")
    if not context_matches(request):
        reasons.append("context-mismatch")
    if corpus == "repair" and request.get("verified") is not True:
        reasons.append("repair-not-verified")
    return reasons


# ----------------------------------------------------------------------------- grouping


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def content_key(example: dict) -> str:
    request = example["request"]
    answer = json.dumps(example["answer"], sort_keys=True, ensure_ascii=False)
    answer = re.sub(r"\?m\.\d+|\?u\.\d+|_hyg\.\d+|_uniq\.\d+", "<N>", answer)
    material = "\n".join(
        [
            Path(str(request.get("file", ""))).name,
            str(request.get("error_line", "")).strip(),
            answer.strip(),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def transition_of(example: dict) -> str:
    req = example["request"]
    return f'{req["old_version"]} -> {req["new_version"]}'


def build_groups(examples: list[dict]) -> tuple[dict[int, str], dict[str, dict]]:
    """Return example idx -> group id and group id -> description."""
    uf = UnionFind()
    by_commit_pair: dict[tuple, str] = {}
    by_content: dict[str, str] = {}
    link_reasons: collections.Counter = collections.Counter()
    for ex in examples:
        req = ex["request"]
        repo = req["repo"]
        uf.find(("repo", repo))
        pair_key = (req["old_hash"], req["new_hash"])
        if pair_key in by_commit_pair and by_commit_pair[pair_key] != repo:
            uf.union(("repo", repo), ("repo", by_commit_pair[pair_key]))
            link_reasons["fork-same-commit-pair"] += 1
        by_commit_pair.setdefault(pair_key, repo)
        ck = content_key(ex)
        if ck in by_content and by_content[ck] != repo:
            uf.union(("repo", repo), ("repo", by_content[ck]))
            link_reasons["identical-content"] += 1
        by_content.setdefault(ck, repo)
    groups: dict[str, dict] = {}
    assignment: dict[int, str] = {}
    for ex in examples:
        root = uf.find(("repo", ex["request"]["repo"]))
        gid = root[1]
        g = groups.setdefault(
            gid, {"repos": set(), "examples": 0, "pairs": set(), "transitions": set()}
        )
        g["repos"].add(ex["request"]["repo"])
        g["examples"] += 1
        g["pairs"].add(ex["request"]["pair_id"])
        g["transitions"].add(transition_of(ex))
        assignment[ex["idx"]] = gid
    for g in groups.values():
        g["repos"] = sorted(g["repos"])
        g["pairs"] = len(g["pairs"])
        g["transitions"] = sorted(g["transitions"])
    groups["_link_reasons"] = dict(link_reasons)
    return assignment, groups


# ----------------------------------------------------------------------------- splitting


def greedy_split(
    unit_sizes: dict[str, int],
    fractions: dict[str, float],
    seed: int,
    max_group_frac: float,
) -> dict[str, str]:
    """Assign whole units (groups or transitions) to splits, approaching target fractions.

    Units are visited in seeded random order; a unit goes to the split that is currently
    furthest below its target share, unless the unit alone exceeds ``max_group_frac`` of
    all examples, in which case it is kept in train so one giant group cannot swallow the
    held-out splits.
    """
    total = sum(unit_sizes.values())
    rng = random.Random(seed)
    units = sorted(unit_sizes)
    rng.shuffle(units)
    targets = {name: frac * total for name, frac in fractions.items()}
    targets["train"] = total - sum(targets.values())
    filled = {name: 0 for name in targets}
    assignment = {}
    for unit in units:
        size = unit_sizes[unit]
        if size > max_group_frac * total:
            choice = "train"
        else:
            deficits = {
                name: (targets[name] - filled[name]) / max(targets[name], 1)
                for name in targets
            }
            choice = max(deficits, key=lambda n: (deficits[n], n == "train"))
            if (
                choice != "train"
                and filled[choice] > 0
                and filled[choice] + size > targets[choice] * 1.25
            ):
                choice = "train"
        assignment[unit] = choice
        filled[choice] += size
    return assignment


# ----------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--corpus", choices=["diagnostic", "repair"], required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--test-frac", type=float, default=0.15)
    parser.add_argument("--val-frac", type=float, default=0.05)
    parser.add_argument("--transition-test-frac", type=float, default=0.15)
    parser.add_argument("--max-group-frac", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()

    examples = load_rows(args.input)
    native_passed = load_native_passed(args.db)
    if native_passed is None:
        print(f"warning: {args.db} not found; native-check QC skipped", file=sys.stderr)

    accepted, rejected = [], []
    reason_counts: collections.Counter = collections.Counter()
    for ex in examples:
        reasons = qc_reasons(ex, native_passed, args.corpus)
        if reasons:
            reason_counts.update(reasons)
            rejected.append(
                {
                    "idx": ex["idx"],
                    "pair_id": ex["request"].get("pair_id"),
                    "repo": ex["request"].get("repo"),
                    "reasons": reasons,
                }
            )
        else:
            accepted.append(ex)

    assignment, groups = build_groups(accepted)
    group_sizes = {gid: g["examples"] for gid, g in groups.items() if not gid.startswith("_")}
    group_split = greedy_split(
        group_sizes, {"test": args.test_frac, "val": args.val_frac}, args.seed, args.max_group_frac
    )

    transition_sizes = collections.Counter(transition_of(ex) for ex in accepted)
    transition_split = greedy_split(
        dict(transition_sizes), {"test": args.transition_test_frac}, args.seed, args.max_group_frac
    )

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    names = ("train", "val", "test", "transition-train", "transition-test")
    handles = {n: (out / f"{n}.jsonl").open("w", encoding="utf-8", newline="\n") for n in names}
    split_counts: collections.Counter = collections.Counter()
    for ex in accepted:
        gid = assignment[ex["idx"]]
        split = group_split[gid]
        tsplit = transition_split[transition_of(ex)]
        row = dict(ex["row"])
        row["split"] = {
            "group": gid,
            "repo_split": split,
            "transition": transition_of(ex),
            "transition_split": tsplit,
        }
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        handles[split].write(line)
        handles[f"transition-{tsplit}"].write(line)
        split_counts[split] += 1
        split_counts[f"transition-{tsplit}"] += 1
    for h in handles.values():
        h.close()

    with (out / "rejected.jsonl").open("w", encoding="utf-8", newline="\n") as h:
        for r in rejected:
            h.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (out / "groups.json").open("w", encoding="utf-8") as h:
        json.dump(
            {
                gid: ({**g, "split": group_split.get(gid)} if not gid.startswith("_") else g)
                for gid, g in groups.items()
            },
            h,
            ensure_ascii=False,
            indent=1,
        )

    repos_per_split = collections.defaultdict(set)
    for ex in accepted:
        repos_per_split[group_split[assignment[ex["idx"]]]].add(ex["request"]["repo"])
    report = {
        "input": str(args.input),
        "corpus": args.corpus,
        "examples_total": len(examples),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "rejection_reasons": dict(reason_counts),
        "groups": len(group_sizes),
        "group_links": groups.get("_link_reasons", {}),
        "repo_split_examples": {k: split_counts[k] for k in ("train", "val", "test")},
        "repo_split_repos": {k: len(v) for k, v in repos_per_split.items()},
        "transition_split_examples": {
            k: split_counts[f"transition-{k}"] for k in ("train", "test")
        },
        "held_out_transitions": sorted(t for t, s in transition_split.items() if s == "test"),
        "parameters": {
            "test_frac": args.test_frac,
            "val_frac": args.val_frac,
            "transition_test_frac": args.transition_test_frac,
            "max_group_frac": args.max_group_frac,
            "seed": args.seed,
        },
    }
    with (out / "qc-report.json").open("w", encoding="utf-8") as h:
        json.dump(report, h, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

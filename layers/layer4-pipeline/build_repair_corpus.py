#!/usr/bin/env python3
"""Section 5.1 of "Lean Proof Repair Assistant": from the diagnostic corpus to a repair corpus.

For every pair whose adapted build produced Lean diagnostics (``completed / build-errors``,
both endpoints verified natively), the old->new commit diff is only a *candidate* ground
truth: it may contain unrelated functional changes, formatting, or new features.  This
script filters and minimizes the diff hunks, applies them to the old code inside the exactly
reconstructed target environment, and accepts a label only when ``lake build`` passes and
the edit does not touch configuration outside the allowed protocol.

Procedure per pair
------------------
1. Reconstruct the adapted target exactly as the pipeline did: pristine ``old``/``new``
   worktrees carrying the native-build manifest snapshots, a ``target`` worktree of the old
   commit, ``lakeva adapt OLD NEW TARGET``, optional submodules, ``lake exe cache get`` when
   Mathlib is in the manifest.  When the pipeline recorded the target's ``lean-toolchain`` /
   ``lake-manifest.json`` for this pair, they are compared with the fresh adaptation and, if
   different, the recorded files win so the environment is the one the diagnostics came from.
2. Baseline ``lake build`` without any repair hunk must reproduce Lean diagnostics (the
   overlap with the recorded diagnostics is stored).
3. Candidate hunks = hunks of ``.lean`` files in the old->new diff.  Hunks of
   ``lean-toolchain``, ``lake-manifest.json``, ``lakefile.lean``, ``lakefile.toml`` are
   excluded: those files are governed by the environment-reconstruction protocol.  Non-Lean
   files and binary patches are excluded as well and counted.
4. Oracle: apply a hunk subset with ``git apply`` to the pristine old sources, run
   ``lake build`` (incremental; ``.lake`` is kept between trials) and classify the log with
   the pipeline's own rules.  A subset *passes* iff the result is ``completed /
   build-succeeded`` (positive build evidence, zero Lean diagnostics, toolchain unchanged).
5. Minimization: hunks in the files that carry diagnostics are tried first; if they pass,
   ddmin runs inside that set, otherwise the full candidate set is tried and minimized.  A
   per-pair build budget bounds the search; when it is exhausted the smallest passing set is
   kept and marked ``minimal: false``.
6. Output: ``repair-corpus/records/<pair_id>.json`` (full provenance, every build log under
   ``repair-corpus/logs``), and ``repair-corpus.jsonl`` with one chat example per verified
   pair:  x = (C_old, E_target, D, M) in the user message, y = the minimized unified diff.

The script never writes to the pipeline database and uses its own worktree root, so it can
run while the pipeline is active.  It skips repositories that currently have a running pair.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_pipeline as rp  # noqa: E402
from build_finetuning_dataset import training_message  # noqa: E402
from commit_to_version import IGNORED_ROOT_FILES, git  # noqa: E402
from extract_lean_errors import parse_log  # noqa: E402
from resource_management import elan_home, elan_toolchain_dirname  # noqa: E402
from result_validation import classify_adapted_build, infrastructure_failure  # noqa: E402

DEFAULT_OUT = HERE / "repair-corpus"
DEFAULT_JSONL = HERE / "repair-corpus.jsonl"
SYSTEM_PROMPT = (
    "You repair Lean 4 code that stopped compiling after the project was migrated to a "
    "newer Lean toolchain. You receive the migration metadata, the exactly reconstructed "
    "target environment, the compiler diagnostics, and the local source context of each "
    "diagnostic. Return one compact JSON object with exactly one field named patch. Its "
    "value is a minimal unified diff against the old source files that makes the project "
    "build in the target environment."
)

ERROR_PAIRS_SQL = """
SELECT p.pair_id, p.repo, r.path AS clone_path, r.clone_deleted, p.pair_index,
       p.old_hash, p.new_hash, p.old_version, p.new_version
FROM pairs p
JOIN repos r ON r.repo = p.repo
JOIN native_checks no ON no.repo=p.repo AND no.commit_hash=p.old_hash
     AND no.version=p.old_version AND no.status='passed'
JOIN native_checks nn ON nn.repo=p.repo AND nn.commit_hash=p.new_hash
     AND nn.version=p.new_version AND nn.status='passed'
WHERE p.active=1 AND p.status='completed' AND p.outcome='build-errors'
ORDER BY p.repo COLLATE NOCASE, p.pair_index
"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# ----------------------------------------------------------------------------- diff parsing


class Hunk:
    __slots__ = ("file", "index", "text", "added", "removed")

    def __init__(self, file: str, index: int, text: str):
        self.file = file
        self.index = index
        self.text = text
        self.added = sum(1 for l in text.split("\n") if l.startswith("+") and not l.startswith("+++"))
        self.removed = sum(1 for l in text.split("\n") if l.startswith("-") and not l.startswith("---"))

    @property
    def key(self) -> str:
        return f"{self.file}#{self.index}"


class FilePatch:
    def __init__(self, header: str):
        self.header = header
        self.hunks: list[Hunk] = []
        m = re.search(r"^diff --git a/(.*?) b/(.*)$", header, re.M)
        self.old_path = m.group(1) if m else ""
        self.new_path = m.group(2) if m else self.old_path
        self.is_new = "\nnew file mode" in header
        self.is_deleted = "\ndeleted file mode" in header
        self.is_binary = "GIT binary patch" in header or "\nBinary files " in header

    @property
    def path(self) -> str:
        return self.new_path if self.is_new else self.old_path

    @property
    def whole_file_unit(self) -> bool:
        return self.is_new or self.is_deleted


def parse_diff(text: str) -> list[FilePatch]:
    lines = [l for l in text.split("\n") if not l.startswith("# ")] if text.startswith("# ") else text.split("\n")
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    header: list[str] = []
    hunk: list[str] | None = None

    def close_hunk():
        nonlocal hunk
        if current is not None and hunk is not None:
            current.hunks.append(Hunk(current.path, len(current.hunks), "\n".join(hunk)))
        hunk = None

    for line in lines:
        if line.startswith("diff --git "):
            close_hunk()
            if current is not None:
                patches.append(current)
            header = [line]
            current = None
            continue
        if current is None:
            if line.startswith("@@"):
                current = FilePatch("\n".join(header))
                hunk = [line]
            else:
                header.append(line)
                if "GIT binary patch" in line or line.startswith("Binary files "):
                    current = FilePatch("\n".join(header))
            continue
        if line.startswith("@@"):
            close_hunk()
            hunk = [line]
        elif hunk is not None:
            hunk.append(line)
    close_hunk()
    if current is not None:
        patches.append(current)
    elif header and header[0].startswith("diff --git "):
        patches.append(FilePatch("\n".join(header)))
    return patches


def candidate_units(patches: list[FilePatch]) -> tuple[dict[str, list[Hunk]], dict[str, int]]:
    """Return unit_id -> hunks (a unit is one hunk, or a whole added/deleted file)."""
    units: dict[str, list[Hunk]] = {}
    excluded = collections.Counter()
    for fp in patches:
        name = Path(fp.path).name
        if fp.is_binary:
            excluded["binary"] += len(fp.hunks) or 1
            continue
        if name in IGNORED_ROOT_FILES or name in ("lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain"):
            excluded["protocol-config"] += len(fp.hunks) or 1
            continue
        if fp.path.startswith(".lake/") or "/.lake/" in fp.path:
            excluded["lake-dir"] += len(fp.hunks) or 1
            continue
        if not fp.path.endswith(".lean"):
            excluded["non-lean"] += len(fp.hunks) or 1
            continue
        if not fp.hunks:
            excluded["empty"] += 1
            continue
        if fp.whole_file_unit:
            units[f"{fp.path}#file"] = list(fp.hunks)
        else:
            for h in fp.hunks:
                units[h.key] = [h]
    return units, dict(excluded)


def render_patch(patches: list[FilePatch], selected: set[str]) -> str:
    parts = []
    for fp in patches:
        chosen = []
        for h in fp.hunks:
            if h.key in selected or (fp.whole_file_unit and f"{fp.path}#file" in selected):
                chosen.append(h.text)
        if chosen:
            header = fp.header if fp.header.endswith("\n") else fp.header
            parts.append(header + "\n" + "\n".join(chosen))
    text = "\n".join(parts)
    return text if text.endswith("\n") else text + "\n"


# ----------------------------------------------------------------------------- build oracle


class ToolchainHold:
    """Hold a handle on the toolchain binary so the pipeline's janitor cannot rename it."""

    def __init__(self, version: str):
        self.version = version
        self.handle = None

    def __enter__(self):
        path = elan_home() / "toolchains" / elan_toolchain_dirname(self.version) / "bin" / "lean.exe"
        if not path.is_file():
            path = path.with_suffix("")
        with contextlib.suppress(OSError):
            self.handle = open(path, "rb")
        return self

    def __exit__(self, *_exc):
        if self.handle is not None:
            self.handle.close()


class PairWork:
    def __init__(self, args, pair: dict, log_dir: Path, worktrees_root: Path):
        self.args = args
        self.pair = pair
        self.log_dir = log_dir
        self.worktrees_root = worktrees_root
        self.repo_path = Path(pair["clone_path"])
        root = worktrees_root / pair["pair_id"]
        self.old_tree = root / "old"
        self.new_tree = root / "new"
        self.target = root / "target"
        self.lake = rp.command_prefix(args.lake)
        self.lakeva = rp.command_prefix(args.lakeva)
        self.builds = 0
        self.build_results: list[dict] = []
        self.touched_files: set[str] = set()
        self.added_files: set[str] = set()

    # -- environment -----------------------------------------------------------------

    def reconstruct_environment(self) -> dict:
        pair, args = self.pair, self.args
        info: dict = {"environment_source": "lakeva"}
        manifests = rp.prepare_adaptation_sources(
            args, pair, self.repo_path, self.old_tree, self.new_tree, self.worktrees_root
        )
        if manifests is None:
            raise RuntimeError("missing lake-manifest.json for old or new commit")
        rp.add_worktree(self.repo_path, self.target, pair["old_hash"], self.worktrees_root)
        if (self.old_tree / rp.LAKE_MANIFEST).is_file():
            shutil.copy2(self.old_tree / rp.LAKE_MANIFEST, self.target / rp.LAKE_MANIFEST)
        rc = rp.run_logged(
            [*self.lakeva, "adapt", str(self.old_tree), str(self.new_tree), str(self.target)],
            HERE,
            self.log_dir / "lakeva-adapt.log",
            args.adapt_timeout,
        )
        if rc != 0:
            text = (self.log_dir / "lakeva-adapt.log").read_text(encoding="utf-8", errors="replace")
            kind = "network-failure" if rp.network_failure_text(text) else "lakeva-adapt-failed"
            raise EnvironmentError(kind)
        if rp.committed_toolchain(self.target) != pair["new_version"]:
            raise RuntimeError("lakeva did not set the target toolchain")

        # Prefer the exact files the diagnostics were recorded with.
        run_dir = args.state_root / "runs" / rp.slug(pair["repo"]) / pair["pair_id"]
        recorded = {}
        for name in ("lean-toolchain", rp.LAKE_MANIFEST):
            if (run_dir / name).is_file():
                recorded[name] = (run_dir / name).read_bytes()
        info["recorded_environment_available"] = bool(recorded)
        differing = [n for n, b in recorded.items() if (self.target / n).read_bytes() != b]
        info["recorded_environment_differs"] = differing
        for name in differing:
            (self.target / name).write_bytes(recorded[name])
            info["environment_source"] = "recorded"
        if (run_dir / ".lakeva-plan.json").is_file():
            info["recorded_plan"] = str(run_dir / ".lakeva-plan.json")
        if (self.target / ".lakeva-plan.json").is_file():
            shutil.copy2(self.target / ".lakeva-plan.json", self.log_dir / ".lakeva-plan.json")
        for name in ("lean-toolchain", rp.LAKE_MANIFEST):
            shutil.copy2(self.target / name, self.log_dir / name)

        if (self.target / ".gitmodules").is_file():
            rc = rp.run_logged(
                ["git", "-c", "core.longpaths=true", "submodule", "update", "--init", "--recursive"],
                self.target,
                self.log_dir / "submodules.log",
                args.cache_timeout,
            )
            if rc != 0:
                raise EnvironmentError("adapted-submodule-setup-failed")
        # Installs the toolchain if needed (lake --help probe) before we take the hold.
        rp.lake_supports_keep_toolchain(self.lake, self.target)
        cache_rc = rp.run_cache_get(args, self.lake, self.target, self.log_dir / "cache.log", pair["new_version"])
        info["cache_exit_code"] = cache_rc
        info["target_environment"] = describe_environment(self.target)
        return info

    # -- oracle -------------------------------------------------------------------------

    def reset_sources(self) -> None:
        if self.touched_files:
            git(self.target, "checkout", "--", *sorted(self.touched_files), check=False)
        for added in self.added_files:
            with contextlib.suppress(OSError):
                (self.target / added).unlink()
        self.touched_files.clear()
        self.added_files.clear()

    def apply(self, patch_text: str, patches: list[FilePatch], selected: set[str]) -> bool:
        patch_file = self.log_dir / f"trial-{self.builds + 1:02d}.patch"
        patch_file.write_text(patch_text, encoding="utf-8", newline="\n")
        for fp in patches:
            unit_keys = {h.key for h in fp.hunks} | {f"{fp.path}#file"}
            if unit_keys & selected:
                if fp.is_new:
                    self.added_files.add(fp.path)
                else:
                    self.touched_files.add(fp.path)
        result = git(self.target, "apply", "--whitespace=nowarn", str(patch_file), check=False)
        if result.returncode != 0:
            (self.log_dir / f"trial-{self.builds + 1:02d}.apply.log").write_text(
                (result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8", errors="replace"
            )
            return False
        return True

    def build(self, label: str) -> tuple[tuple[str, str], list[dict], int]:
        self.builds += 1
        build_log = self.log_dir / f"build-{self.builds:02d}-{label}.log"
        rc = rp.run_logged(
            rp.lake_command_for(self.lake, self.target, ["build"]),
            self.target,
            build_log,
            self.args.build_timeout,
            toolchain=self.pair["new_version"],
        )
        errors = parse_log(build_log)
        status = classify_adapted_build(
            rc,
            errors,
            build_log,
            infrastructure_failure=infrastructure_failure(build_log),
            expected_toolchain=self.pair["new_version"],
            cache_log=self.log_dir / "cache.log",
        )
        lean_errors = [e for e in errors if e.get("kind") == "lean-diagnostic"]
        self.build_results.append(
            {"build": self.builds, "label": label, "exit_code": rc, "status": list(status),
             "lean_diagnostics": len(lean_errors), "log": build_log.name}
        )
        return status, lean_errors, rc

    def passes(self, patches: list[FilePatch], selected: set[str]) -> tuple[bool, dict]:
        self.reset_sources()
        patch_text = render_patch(patches, selected)
        label = f"n{len(selected)}"
        if not self.apply(patch_text, patches, selected):
            self.build_results.append({"build": None, "label": label, "status": ["apply-failed", None]})
            return False, {"apply_failed": True}
        status, lean_errors, rc = self.build(label)
        ok = status == ("completed", "build-succeeded")
        return ok, {"status": status, "lean_diagnostics": len(lean_errors), "exit_code": rc}


def describe_environment(target: Path) -> dict:
    env = {"lean_toolchain": rp.committed_toolchain(target)}
    manifest = target / rp.LAKE_MANIFEST
    deps = []
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for pkg in data.get("packages", []):
                if pkg.get("inherited"):
                    continue
                deps.append({k: pkg.get(k) for k in ("name", "type", "url", "scope", "rev", "inputRev", "subDir") if pkg.get(k) is not None})
        except (json.JSONDecodeError, OSError):
            pass
    env["direct_dependencies"] = deps
    return env


# ----------------------------------------------------------------------------- ddmin


def ddmin(items: list[str], test, budget_left) -> tuple[list[str], bool]:
    """Zeller's ddmin: smallest subset (1-minimal) for which ``test`` still passes.

    Returns (subset, completed).  ``completed`` is False when the build budget ran out.
    """
    current = list(items)
    n = 2
    while len(current) >= 2:
        chunk = max(1, len(current) // n)
        subsets = [current[i:i + chunk] for i in range(0, len(current), chunk)]
        reduced = False
        for subset in subsets:
            if budget_left() <= 0:
                return current, False
            if test(subset):
                current, n, reduced = subset, 2, True
                break
        if not reduced:
            for subset in subsets:
                complement = [x for x in current if x not in subset]
                if not complement:
                    continue
                if budget_left() <= 0:
                    return current, False
                if test(complement):
                    current, n, reduced = complement, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current, True


# ----------------------------------------------------------------------------- per pair


def load_expected_diagnostics(errors_root: Path, pair_id: str) -> list[dict]:
    rows = []
    for path in sorted(errors_root.glob(f"*__{pair_id}__error-*.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def process_pair(args, pair: dict, out_dir: Path, worktrees_root: Path) -> dict:
    pair_id = pair["pair_id"]
    log_dir = out_dir / "logs" / rp.slug(pair["repo"]) / pair_id
    log_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    record = {
        "pair_id": pair_id, "repo": pair["repo"], "pair_index": pair["pair_index"],
        "old_hash": pair["old_hash"], "new_hash": pair["new_hash"],
        "old_version": pair["old_version"], "new_version": pair["new_version"],
        "started_at": utcnow(), "verified": False, "rejection": None,
    }
    diff_path = HERE / "repair-diffs" / f"{rp.slug(pair['repo'])}__{pair_id}.diff.gz"
    import gzip
    diff_text = gzip.open(diff_path, "rt", encoding="utf-8", errors="replace").read()
    patches = parse_diff(diff_text)
    units, excluded = candidate_units(patches)
    expected = load_expected_diagnostics(args.errors_root, pair_id)
    error_files = {e["file"].replace("\\", "/") for e in expected}
    record.update({
        "candidate_hunks": len(units), "excluded_hunks": excluded,
        "expected_diagnostics": len(expected), "error_files": sorted(error_files),
    })
    if not units:
        record["rejection"] = "no-lean-hunks-in-diff"
        return finish(record, started)

    work = PairWork(args, pair, log_dir, worktrees_root)
    try:
        with ToolchainHold(pair["new_version"]):
            try:
                record["environment"] = work.reconstruct_environment()
            except EnvironmentError as exc:
                record["rejection"] = f"environment:{exc}"
                return finish(record, started)

            status, baseline_errors, rc = work.build("baseline")
            record["baseline"] = {"status": list(status), "exit_code": rc, "lean_diagnostics": len(baseline_errors)}
            if status != ("completed", "build-errors"):
                record["rejection"] = "baseline-did-not-reproduce" if status == ("completed", "build-succeeded") else f"baseline-inconclusive:{status[1]}"
                return finish(record, started, work)
            expected_locs = {(e["file"].replace("\\", "/"), e["line"]) for e in expected}
            baseline_locs = set()
            for e in baseline_errors:
                name, _ = rp._relative_source_path(e["file"], work.target)
                baseline_locs.add((str(name).replace("\\", "/"), e.get("line")))
            overlap = len(expected_locs & baseline_locs)
            record["baseline"]["expected_locations_reproduced"] = f"{overlap}/{len(expected_locs)}"
            record["baseline"]["diagnostics"] = [
                {"file": e["file"], "line": e.get("line"), "column": e.get("column"), "message": e.get("message", "")}
                for e in baseline_errors
            ]

            budget_left = lambda: args.max_builds - work.builds  # noqa: E731
            tested: dict[frozenset, bool] = {}

            def test(subset) -> bool:
                key = frozenset(subset)
                if key in tested:
                    return tested[key]
                ok, _ = work.passes(patches, set(subset))
                tested[key] = ok
                return ok

            all_units = sorted(units)
            in_error_files = [u for u in all_units if u.rsplit("#", 1)[0] in error_files]
            record["hunks_in_error_files"] = len(in_error_files)
            start_set = None
            if in_error_files and test(in_error_files):
                start_set = in_error_files
                record["search_start"] = "error-file-hunks"
            elif budget_left() > 0 and len(in_error_files) != len(all_units) and test(all_units):
                start_set = all_units
                record["search_start"] = "all-lean-hunks"
            if start_set is None:
                record["rejection"] = "lean-diff-does-not-repair" if budget_left() > 0 else "build-budget-exhausted"
                last = work.build_results[-1] if work.build_results else {}
                record["last_trial"] = last
                return finish(record, started, work)

            minimal, completed = ddmin(start_set, test, budget_left)
            record["minimal"] = completed
            record["minimal_hunks"] = len(minimal)
            final_patch = render_patch(patches, set(minimal))
            record["patch"] = final_patch
            record["patch_files"] = sorted({u.rsplit("#", 1)[0] for u in minimal})
            record["patch_lines_added"] = sum(h.added for u in minimal for h in units[u])
            record["patch_lines_removed"] = sum(h.removed for u in minimal for h in units[u])
            (log_dir / "minimal.patch").write_text(final_patch, encoding="utf-8", newline="\n")
            record["verified"] = True
            return finish(record, started, work)
    finally:
        if not args.keep_worktrees:
            for tree in (work.target, work.new_tree, work.old_tree):
                with contextlib.suppress(Exception):
                    rp.remove_worktree(work.repo_path, tree, worktrees_root)
            with contextlib.suppress(Exception):
                git(work.repo_path, "worktree", "prune", check=False)
            with contextlib.suppress(OSError):
                (worktrees_root / pair_id).rmdir()  # only succeeds when empty


def finish(record: dict, started: float, work: PairWork | None = None) -> dict:
    record["finished_at"] = utcnow()
    record["seconds"] = round(time.time() - started, 1)
    if work is not None:
        record["builds"] = work.builds
        record["trials"] = work.build_results
    return record


# ----------------------------------------------------------------------------- corpus export


def example_from_record(record: dict, expected: list[dict]) -> dict | None:
    if not record.get("verified"):
        return None
    diagnostics = []
    contexts = []
    for e in sorted(expected, key=lambda e: (e["file"], e["line"], e["column"])):
        message, _ = training_message(e["message"])
        diagnostics.append({"file": e["file"], "line": e["line"], "column": e["column"], "message": message})
        contexts.append({
            "file": e["file"], "line": e["line"], "error_line": e["error_line"], "code": e["code"],
            "context_start_line": e["context_start_line"], "context_end_line": e["context_end_line"],
            "source_found": e["source_found"],
        })
    first = contexts[0] if contexts else {}
    request = {
        "task": "Repair this Lean project so that it builds in the target environment.",
        "repo": record["repo"], "old_version": record["old_version"], "new_version": record["new_version"],
        "old_hash": record["old_hash"], "new_hash": record["new_hash"], "pair_id": record["pair_id"],
        "target_environment": record.get("environment", {}).get("target_environment", {}),
        "diagnostics": diagnostics,
        "contexts": contexts,
        # First diagnostic flattened for tooling that expects one location per example.
        "file": first.get("file"), "line": first.get("line"), "error_line": first.get("error_line"),
        "code": first.get("code"), "context_start_line": first.get("context_start_line"),
        "context_end_line": first.get("context_end_line"), "source_found": first.get("source_found", False),
        "verified": True,
        "minimal": record.get("minimal", False),
        "patch_files": record.get("patch_files", []),
    }
    answer = {"patch": record["patch"]}
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False, separators=(",", ":"))},
            {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False, separators=(",", ":"))},
        ]
    }


def export_corpus(args, out_dir: Path) -> dict:
    records_dir = out_dir / "records"
    summary = collections.Counter()
    examples = []
    for path in sorted(records_dir.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        summary["records"] += 1
        if record.get("verified"):
            summary["verified"] += 1
            summary["minimal" if record.get("minimal") else "budget-limited"] += 1
            expected = load_expected_diagnostics(args.errors_root, record["pair_id"])
            example = example_from_record(record, expected)
            if example:
                examples.append(example)
        else:
            summary[f"rejected:{(record.get('rejection') or 'unknown').split(':')[0]}"] += 1
    tmp = args.output.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for ex in examples:
            handle.write(json.dumps(ex, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(args.output)
    (out_dir / "summary.json").write_text(json.dumps(dict(summary), indent=1), encoding="utf-8")
    return dict(summary)


# ----------------------------------------------------------------------------- scheduling


def load_pairs(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(ERROR_PAIRS_SQL)]
    finally:
        con.close()


def repos_with_running_pairs(db: Path) -> set[str]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT repo FROM pairs WHERE status='running'")}
    finally:
        con.close()


def plan(args, pairs: list[dict], out_dir: Path) -> list[tuple[dict, int]]:
    import gzip
    done = {p.stem for p in (out_dir / "records").glob("*.json")} if not args.redo else set()
    scheduled = []
    for pair in pairs:
        if pair["pair_id"] in done:
            continue
        if args.pair_id and pair["pair_id"] not in args.pair_id:
            continue
        if args.repo and pair["repo"] not in args.repo:
            continue
        clone_missing = pair["clone_deleted"] or not (Path(pair["clone_path"]) / ".git").exists()
        if clone_missing and not args.restore_clones:
            continue
        pair["clone_missing"] = bool(clone_missing)
        diff_path = HERE / "repair-diffs" / f"{rp.slug(pair['repo'])}__{pair['pair_id']}.diff.gz"
        if not diff_path.is_file():
            continue
        text = gzip.open(diff_path, "rt", encoding="utf-8", errors="replace").read()
        units, _ = candidate_units(parse_diff(text))
        scheduled.append((pair, len(units)))
    scheduled.sort(key=lambda t: (t[0].get("clone_missing", False), t[1], t[0]["repo"].casefold(), t[0]["pair_index"]))
    if args.max_pairs:
        scheduled = scheduled[: args.max_pairs]
    return scheduled


def restore_clone(args, pair: dict) -> None:
    """Re-clone a deleted repository the way the collection cloned it (README: deleted clones)."""
    import csv
    import subprocess

    manifest = HERE.parent / "lean-multi-toolchain-repos" / "qualifying-repositories.csv"
    with manifest.open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["name"]: row for row in csv.DictReader(handle)}
    row = rows.get(pair["repo"])
    if row is None:
        raise RuntimeError(f"{pair['repo']} is not in {manifest}")
    path = Path(pair["clone_path"])
    if (path / ".git").exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"  re-cloning {pair['repo']} ({row.get('clone_mode')}) -> {path}")
    command = rp.clone_command(row["url"], path, row.get("clone_mode") or "")
    result = subprocess.run(command, env=rp.child_process_environment(), capture_output=True, text=True,
                            timeout=args.cache_timeout)
    if result.returncode != 0:
        raise RuntimeError(f"clone failed: {(result.stderr or '')[-1500:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-root", type=Path, default=HERE / "state")
    parser.add_argument("--errors-root", type=Path, default=HERE / "parsed-errors")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--worktrees-root", type=Path, default=None,
                        help="Default: %%TEMP%%\\lvp-repair\\<state id>; must differ from the pipeline's root")
    parser.add_argument("--lake", default="lake")
    parser.add_argument("--lakeva", default="lakeva")
    parser.add_argument("--cache-timeout", type=int, default=1800)
    parser.add_argument("--adapt-timeout", type=int, default=3600)
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--max-builds", type=int, default=40, help="Build budget per pair, baseline included")
    parser.add_argument("--max-pairs", type=int, default=None)
    parser.add_argument("--pair-id", action="append")
    parser.add_argument("--repo", action="append")
    parser.add_argument("--skip-cache", action="store_true")
    parser.add_argument("--keep-worktrees", action="store_true")
    parser.add_argument("--redo", action="store_true", help="Reprocess pairs that already have a record")
    parser.add_argument("--include-busy-repos", action="store_true",
                        help="Also process repos that currently have a running pipeline pair")
    parser.add_argument("--restore-clones", action="store_true",
                        help="Re-clone deleted repository clones (after all pairs with clones) and process their pairs too")
    parser.add_argument("--dry-run", action="store_true", help="Print the schedule and exit")
    parser.add_argument("--export-only", action="store_true", help="Only regenerate the JSONL from records")
    parser.add_argument("--acknowledge-untrusted-builds", action="store_true")
    args = parser.parse_args()
    args.state_root = args.state_root.resolve()
    args.errors_root = args.errors_root.resolve()
    args.output = args.output.resolve()
    out_dir = args.out_dir.resolve()
    (out_dir / "records").mkdir(parents=True, exist_ok=True)

    if args.export_only:
        print(json.dumps(export_corpus(args, out_dir), indent=1))
        return 0

    if args.worktrees_root is None:
        state_id = hashlib.sha256(str(args.state_root).casefold().encode()).hexdigest()[:10]
        args.worktrees_root = Path(os.environ.get("TEMP", str(Path.home() / "AppData/Local/Temp"))) / "lvp-repair" / state_id
    args.worktrees_root = args.worktrees_root.resolve()
    args.worktrees_root.mkdir(parents=True, exist_ok=True)

    db = args.state_root / "progress.sqlite3"
    pairs = load_pairs(db)
    schedule = plan(args, pairs, out_dir)
    log(f"{len(pairs)} verified error pairs; {len(schedule)} scheduled (records exist for the rest or clone missing)")
    if args.dry_run:
        for pair, n in schedule:
            print(f"  {n:4d} hunks  {pair['repo']}  {pair['old_version']} -> {pair['new_version']}  {pair['pair_id']}")
        missing = [p for p in pairs if p["clone_deleted"] or not (Path(p["clone_path"]) / ".git").exists()]
        print(f"pairs whose clone is missing: {len(missing)}" + ("" if args.restore_clones else " (use --restore-clones to include them)"))
        return 0
    if not args.acknowledge_untrusted_builds:
        print("Refusing to run Lake builds of untrusted repositories without --acknowledge-untrusted-builds", file=sys.stderr)
        return 2

    processed = 0
    try:
        for pair, n_units in schedule:
            if not args.include_busy_repos and pair["repo"] in repos_with_running_pairs(db):
                log(f"skip (pipeline is working on this repo now): {pair['repo']}")
                continue
            log(f"pair {pair['repo']} {pair['old_version']} -> {pair['new_version']} [{pair['pair_id']}] {n_units} candidate hunks")
            try:
                if pair.get("clone_missing"):
                    restore_clone(args, pair)
                record = process_pair(args, pair, out_dir, args.worktrees_root)
            except (KeyboardInterrupt, rp.PipelineInterrupted):
                raise
            except Exception as exc:  # noqa: BLE001
                record = {"pair_id": pair["pair_id"], "repo": pair["repo"], "verified": False,
                          "rejection": f"exception:{type(exc).__name__}", "error": str(exc)[-2000:],
                          "old_version": pair["old_version"], "new_version": pair["new_version"],
                          "old_hash": pair["old_hash"], "new_hash": pair["new_hash"], "pair_index": pair["pair_index"]}
            (out_dir / "records" / f"{pair['pair_id']}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            verdict = "VERIFIED" if record.get("verified") else f"rejected: {record.get('rejection')}"
            log(f"  -> {verdict}; builds={record.get('builds')} hunks {record.get('candidate_hunks')} -> {record.get('minimal_hunks')} in {record.get('seconds')}s")
            processed += 1
            export_corpus(args, out_dir)
    except KeyboardInterrupt:
        log("interrupted")
    summary = export_corpus(args, out_dir)
    log(f"processed {processed} pairs this run; corpus summary: {json.dumps(summary)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

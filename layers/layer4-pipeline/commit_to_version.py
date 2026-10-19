#!/usr/bin/env python3
"""Find useful Lean-version commit pairs in one Git repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Iterable


IGNORED_ROOT_FILES = {
    "lake-manifest.json",
    "lakefile.toml",
    "lakefile.lean",
    "lean-toolchain",
}


class GitCommandError(RuntimeError):
    pass


def git(repo: Path, *args: str, timeout: int = 1800, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["git", "-c", "core.longpaths=true", *args]
    result = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitCommandError(f"{' '.join(command)} failed in {repo}: {detail}")
    return result


def normalize_toolchain(text: str) -> str | None:
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines() if line.strip()]
    return "\n".join(lines) or None


def toolchain_at(repo: Path, commit: str) -> str | None:
    result = git(repo, "show", f"{commit}:lean-toolchain", check=False)
    if result.returncode != 0:
        return None
    return normalize_toolchain(result.stdout)


def is_shallow(repo: Path) -> bool:
    result = git(repo, "rev-parse", "--is-shallow-repository")
    return result.stdout.strip().lower() == "true"


def ensure_full_history(repo: Path) -> None:
    """Deepen a shallow clone; blobless clones stay blobless."""
    if is_shallow(repo):
        git(repo, "fetch", "--unshallow", "--filter=blob:none", "origin", timeout=7200)


def version_change_commits(repo: Path) -> list[dict]:
    """Return actual toolchain transitions, newest first.

    Multiple consecutive commits that normalize to the same toolchain are
    collapsed to the oldest such commit, which is the closest boundary to the
    preceding Lean version.
    """
    result = git(repo, "log", "--format=%H", "--", "lean-toolchain")
    entries: list[dict] = []
    last_version: str | None = None
    for commit in result.stdout.split():
        version = toolchain_at(repo, commit)
        if version is None:
            last_version = None
            continue
        entry = {"commit": commit, "version": version}
        if entries and last_version == version:
            entries[-1] = entry
        else:
            entries.append(entry)
        last_version = version
    return entries


def changed_paths(repo: Path, newer: str, older: str) -> list[str]:
    result = git(repo, "diff", "--name-only", "--no-renames", newer, older, "--")
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def has_meaningful_diff(paths: Iterable[str]) -> bool:
    return any(path not in IGNORED_ROOT_FILES for path in paths)


def discover_pairs(repo: Path) -> list[dict]:
    """Choose the first older version boundary with a meaningful source diff."""
    transitions = version_change_commits(repo)
    pairs: list[dict] = []
    for newer_index in range(len(transitions) - 1):
        newer = transitions[newer_index]
        for older_index in range(newer_index + 1, len(transitions)):
            older = transitions[older_index]
            if older['version'] == newer['version']:
                continue
            paths = changed_paths(repo, newer["commit"], older["commit"])
            meaningful = [path for path in paths if path not in IGNORED_ROOT_FILES]
            if not meaningful:
                continue
            pairs.append(
                {
                    "pair_index": len(pairs),
                    "new_hash": newer["commit"],
                    "old_hash": older["commit"],
                    "new_version": newer["version"],
                    "old_version": older["version"],
                    "newer_transition_index": newer_index,
                    "older_transition_index": older_index,
                    "skipped_version_boundaries": older_index - newer_index - 1,
                    "meaningful_paths_sample": meaningful[:25],
                    "meaningful_path_count": len(meaningful),
                }
            )
            break
    return pairs


def write_jsonl_atomic(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--no-unshallow", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if not args.no_unshallow:
        ensure_full_history(repo)
    pairs = discover_pairs(repo)
    write_jsonl_atomic(args.output.resolve(), pairs)
    print(f"{repo}: wrote {len(pairs)} pair(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded disk usage and yield-ordered scheduling for long pipeline runs.

The pipeline touches thousands of historical Lean releases.  Left alone, elan
keeps every toolchain it ever installed (0.5-2.5 GB each) and Mathlib's
``cache get`` keeps every ``.ltar`` it ever downloaded.  Both are pure caches:
elan reinstalls a missing toolchain on demand and ``cache get`` re-downloads a
missing archive, so they can be trimmed safely between builds.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterable


# ---------------------------------------------------------------------------
# Toolchains


def elan_home() -> Path:
    value = os.environ.get("ELAN_HOME")
    return Path(value) if value else Path.home() / ".elan"


def canonical_toolchain(version: str) -> str:
    value = version.strip()
    if ":" not in value and "/" not in value:
        value = "leanprover/lean4:" + value
    return value


def elan_toolchain_dirname(version: str) -> str:
    """Directory name elan uses under ``toolchains/`` for a release string."""
    value = canonical_toolchain(version)
    if ":nightly-" in value and "/lean4:" in value:
        value = value.replace("/lean4:", "/lean4-nightly:")
    return value.replace("/", "--").replace(":", "---")


def protected_elan_toolchains(home: Path) -> set[str]:
    """The user's default toolchain and directory overrides are never evicted."""
    settings = home / "settings.toml"
    if not settings.is_file():
        return set()
    try:
        import tomllib

        data = tomllib.loads(settings.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names: set[str] = set()
    default = data.get("default_toolchain")
    if isinstance(default, str):
        names.add(elan_toolchain_dirname(default))
    overrides = data.get("overrides")
    if isinstance(overrides, dict):
        for value in overrides.values():
            if isinstance(value, str):
                names.add(elan_toolchain_dirname(value))
    return names


class ToolchainJanitor:
    """Keep at most ``max_toolchains`` pipeline toolchains installed (LRU).

    Only toolchains that occur in the pipeline's own pair table are candidates;
    the elan default, elan overrides and anything currently in use or needed
    by the next scheduled pairs are kept.  A toolchain is first renamed away
    (which fails on Windows while any process holds its files open) and only
    then deleted, so elan never sees a half-deleted installation.
    """

    def __init__(
        self,
        managed_versions: Iterable[str],
        max_toolchains: int,
        *,
        home: Path | None = None,
        protect: Iterable[str] = (),
        log: Callable[[str], None] = print,
    ) -> None:
        self.home = home or elan_home()
        self.root = self.home / "toolchains"
        self.max_toolchains = max_toolchains
        self.managed = {elan_toolchain_dirname(v) for v in managed_versions if v.strip()}
        self.protected = protected_elan_toolchains(self.home) | {
            elan_toolchain_dirname(v) for v in protect
        }
        self.last_used: dict[str, float] = {}
        self.log = log
        self.evicted = 0

    def touch(self, versions: Iterable[str]) -> None:
        stamp = time.time()
        for version in versions:
            self.last_used[elan_toolchain_dirname(version)] = stamp

    def _installed(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return [
            entry.name
            for entry in os.scandir(self.root)
            if entry.is_dir() and ".evicting-" not in entry.name
        ]

    def _age_key(self, name: str) -> float:
        if name in self.last_used:
            return self.last_used[name]
        try:
            return (self.root / name).stat().st_mtime - 10**9  # older than any use
        except OSError:
            return 0.0

    def _remove_leftovers(self) -> None:
        if not self.root.is_dir():
            return
        for entry in os.scandir(self.root):
            if ".evicting-" in entry.name:
                shutil.rmtree(entry.path, ignore_errors=True)

    def evict(self, keep_versions: Iterable[str]) -> int:
        if self.max_toolchains <= 0:
            return 0
        self._remove_leftovers()
        keep = {elan_toolchain_dirname(v) for v in keep_versions} | self.protected
        installed = self._installed()
        managed_installed = [
            name for name in installed if name in self.managed and name not in self.protected
        ]
        excess = len(managed_installed) - self.max_toolchains
        if excess <= 0:
            return 0
        candidates = sorted(
            (name for name in managed_installed if name not in keep),
            key=self._age_key,
        )
        removed = 0
        for name in candidates[:excess]:
            if self._remove(name):
                removed += 1
        if removed:
            self.evicted += removed
            self.log(f"  evicted {removed} unused Lean toolchain(s) to stay within --max-toolchains")
        return removed

    def _remove(self, name: str) -> bool:
        source = self.root / name
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        doomed = self.root / f"{name}.evicting-{stamp}"
        try:
            source.rename(doomed)
        except OSError:
            return False  # in use by some process; try again later
        (self.home / "update-hashes" / name).unlink(missing_ok=True)
        shutil.rmtree(doomed, ignore_errors=True)
        self.last_used.pop(name, None)
        return True


# ---------------------------------------------------------------------------
# Mathlib .ltar cache


def mathlib_cache_dirs() -> list[Path]:
    candidates = []
    if os.environ.get("MATHLIB_CACHE_DIR"):
        candidates.append(Path(os.environ["MATHLIB_CACHE_DIR"]))
    if os.environ.get("XDG_CACHE_HOME"):
        candidates.append(Path(os.environ["XDG_CACHE_HOME"]) / "mathlib")
    candidates.append(Path.home() / ".cache" / "mathlib")
    unique: list[Path] = []
    for path in candidates:
        if path.is_dir() and path.resolve() not in {p.resolve() for p in unique}:
            unique.append(path)
    return unique


def prune_ltar_cache(directory: Path, max_bytes: int, *, target_ratio: float = 0.8) -> tuple[int, int]:
    """Delete least-recently-touched ``.ltar`` files until under ``max_bytes``.

    Returns (files_removed, bytes_removed).
    """
    entries: list[tuple[float, int, str]] = []
    total = 0
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if not entry.name.endswith(".ltar"):
                continue
            try:
                info = entry.stat()
            except OSError:
                continue
            total += info.st_size
            entries.append((max(info.st_atime, info.st_mtime), info.st_size, entry.path))
    if total <= max_bytes:
        return 0, 0
    goal = int(max_bytes * target_ratio)
    entries.sort()
    removed_files = 0
    removed_bytes = 0
    for _stamp, size, path in entries:
        if total - removed_bytes <= goal:
            break
        try:
            os.unlink(path)
        except OSError:
            continue
        removed_files += 1
        removed_bytes += size
    return removed_files, removed_bytes


class CacheGate:
    """Many concurrent ``cache get`` users, or one exclusive pruner."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._users = 0
        self._pruning = False

    def __enter__(self):
        with self._condition:
            while self._pruning:
                self._condition.wait()
            self._users += 1
        return self

    def __exit__(self, *_exc) -> None:
        with self._condition:
            self._users -= 1
            self._condition.notify_all()

    def try_exclusive(self, operation: Callable[[], None]) -> bool:
        with self._condition:
            if self._users or self._pruning:
                return False
            self._pruning = True
        try:
            operation()
        finally:
            with self._condition:
                self._pruning = False
                self._condition.notify_all()
        return True


class MathlibCacheJanitor:
    def __init__(
        self,
        max_gb: float,
        gate: CacheGate,
        *,
        interval_seconds: float = 1800,
        directories: list[Path] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.max_bytes = int(max_gb * 2**30)
        self.gate = gate
        self.interval = interval_seconds
        self.directories = directories if directories is not None else mathlib_cache_dirs()
        self.next_run = 0.0
        self.log = log

    def maybe_prune(self, *, force: bool = False) -> None:
        if self.max_bytes <= 0 or not self.directories:
            return
        if not force and time.monotonic() < self.next_run:
            return

        def operation() -> None:
            for directory in self.directories:
                files, size = prune_ltar_cache(directory, self.max_bytes)
                if files:
                    self.log(
                        f"  pruned {files} Mathlib cache file(s) ({size / 2**30:.1f} GiB) from {directory}"
                    )

        if self.gate.try_exclusive(operation):
            self.next_run = time.monotonic() + self.interval


# ---------------------------------------------------------------------------
# Background worktree deletion


def directory_size(path: Path) -> int:
    """Bytes of regular files below ``path`` (symlinks are not followed)."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


class WorktreeTrash:
    """Delete discarded worktrees on a daemon thread instead of in the worker.

    Removing a multi-GB target worktree idles a worker for minutes.  ``discard``
    renames the directory into ``root`` (same volume, so it is instantaneous),
    measures it, and returns; the janitor thread deletes entries oldest first.
    ``wait_below`` lets a worker pause before it starts a new pair while more
    than ``max_bytes`` are still waiting for deletion, so the trash cannot
    outgrow the disk.  Entries left behind by an interrupted run are picked up
    by ``sweep`` at the next startup.
    """

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        *,
        onerror: Callable | None = None,
        log: Callable[[str], None] = print,
        stop: threading.Event | None = None,
    ) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.onerror = onerror
        self.log = log
        self.stop = stop
        self.condition = threading.Condition()
        self.queue: list[Path] = []
        self.sizes: dict[Path, int] = {}
        self.deleted = 0
        self.failed: list[Path] = []
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(
                target=self._run, name="lean-pipeline-trash", daemon=True
            )
            self.thread.start()

    def enqueue(self, path: Path) -> None:
        size = directory_size(path)
        with self.condition:
            if path not in self.sizes:
                self.queue.append(path)
            self.sizes[path] = size
            self.condition.notify_all()

    def discard(self, path: Path, label: str) -> bool:
        """Move ``path`` into the trash; False when it must be deleted inline."""
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        destination = self.root / f"{label}-{stamp}"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
        except OSError:
            return False
        self.enqueue(destination)
        return True

    def sweep(self) -> int:
        """Queue every entry an earlier run left in the trash directory."""
        if not self.root.is_dir():
            return 0
        entries = [entry for entry in self.root.iterdir() if entry.is_dir()]
        for entry in entries:
            self.enqueue(entry)
        return len(entries)

    def queued_bytes(self) -> int:
        with self.condition:
            return sum(self.sizes.values())

    def pending(self) -> int:
        with self.condition:
            return len(self.sizes)

    def wait_below(self, *, poll: float = 1.0, deadline: float | None = None) -> bool:
        """Block while more than ``max_bytes`` await deletion; False on timeout/stop."""
        end = None if deadline is None else time.monotonic() + deadline
        with self.condition:
            while self.sizes and sum(self.sizes.values()) > self.max_bytes:
                if self.stop is not None and self.stop.is_set():
                    return False
                if end is not None and time.monotonic() >= end:
                    return False
                self.condition.wait(poll)
            return True

    def drain(self, *, poll: float = 1.0, deadline: float | None = None) -> bool:
        """Wait until the queue is empty (normal completion); False on timeout/stop."""
        end = None if deadline is None else time.monotonic() + deadline
        with self.condition:
            while self.sizes:
                if self.stop is not None and self.stop.is_set():
                    return False
                if end is not None and time.monotonic() >= end:
                    return False
                self.condition.wait(poll)
            return True

    def _delete(self, path: Path) -> bool:
        for delay in (0, 2, 5):
            if delay:
                time.sleep(delay)
            try:
                shutil.rmtree(path, onerror=self.onerror)
            except FileNotFoundError:
                return True
            except OSError:
                continue
            if not path.exists():
                return True
        return not path.exists()

    def _run(self) -> None:
        while True:
            with self.condition:
                while not self.queue:
                    self.condition.wait()
                path = self.queue.pop(0)
            try:
                removed = self._delete(path)
            except Exception as exc:  # deletion must never stop the janitor
                removed = False
                self.log(f"  trash cleanup warning for {path}: {exc}")
            with self.condition:
                size = self.sizes.pop(path, 0)
                if removed:
                    self.deleted += 1
                else:
                    self.failed.append(path)
                    self.log(
                        f"  could not delete {path} ({size / 2**30:.1f} GiB); "
                        "it is retried at the next startup"
                    )
                self.condition.notify_all()


# ---------------------------------------------------------------------------
# Logs


def enable_ntfs_compression(directory: Path) -> bool:
    """Mark a directory tree NTFS-compressed; later files inherit it.

    Build logs are highly repetitive text and shrink several-fold, while every
    reader keeps using ordinary file reads.  No-op outside Windows.
    """
    if os.name != "nt":
        return False
    directory.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["compact", "/c", "/s", "/i", "/q"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Scheduling


MINOR = re.compile(r"(?:^|[:/])v?4\.(\d+)\.\d+")


def lean_minor(version: str) -> int | None:
    match = MINOR.search(version.strip())
    return int(match.group(1)) if match else None


def changes_lean_source(meta: dict | None) -> bool:
    """Whether discovery saw a changed .lean file between the two commits."""
    if not meta:
        return True  # unknown: do not penalize
    sample = meta.get("meaningful_paths_sample") or []
    if any(str(path).endswith(".lean") for path in sample):
        return True
    return int(meta.get("meaningful_path_count") or 0) > len(sample)


def pair_priority(pair: dict, meta: dict | None) -> int:
    """Lower runs first.  Buckets follow the yield observed in the first ~650 pairs.

    0: forward bump of 1-3 Lean minor releases (highest Lean-error rate)
    1: larger forward jumps and downgrades
    2: nightly / non-semver toolchains
    3: same-minor transitions (patch, rc -> release) or no .lean change;
       none of these produced a Lean error so far.
    """
    if not changes_lean_source(meta):
        return 3
    old, new = lean_minor(pair["old_version"]), lean_minor(pair["new_version"])
    if old is None or new is None:
        return 2
    if old == new:
        return 3
    if 1 <= new - old <= 3:
        return 0
    return 1


def load_pair_metadata(pairs_root: Path) -> dict[tuple[str, str], dict]:
    metadata: dict[tuple[str, str], dict] = {}
    if not pairs_root.is_dir():
        return metadata
    for path in pairs_root.glob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        row = json.loads(line)
                        metadata[(row["new_hash"], row["old_hash"])] = row
        except (OSError, ValueError, KeyError):
            continue
    return metadata


def pair_key(pair: dict) -> tuple[str, str, str, str]:
    """What makes two pairs of different repositories the same experiment."""
    return (pair["old_hash"], pair["new_hash"], pair["old_version"], pair["new_version"])


class PairScheduler:
    """Hand out pairs in priority order, never two from one repository at once.

    A pair whose (commits, toolchains) key is running in another worker is
    deferred: once the first finishes, the second is completed by copying its
    result instead of repeating the builds (see ``--no-fork-reuse``).
    """

    def __init__(
        self,
        pairs: list[dict],
        *,
        stop: threading.Event,
        should_drain: Callable[[], bool] | None = None,
        after_pair: Callable[["PairScheduler"], None] | None = None,
    ) -> None:
        self.pending = list(pairs)
        self.busy_repos: set[str] = set()
        self.busy_keys: set[tuple[str, str, str, str]] = set()
        self.active: dict[str, dict] = {}
        self.stop = stop
        self.should_drain = should_drain or (lambda: False)
        self.after_pair_hook = after_pair
        self.condition = threading.Condition()
        self.draining = False

    def next(self) -> dict | None:
        with self.condition:
            while True:
                if self.stop.is_set() or not self.pending:
                    return None
                if not self.draining and self.should_drain():
                    self.draining = True
                if self.draining:
                    return None
                for position, pair in enumerate(self.pending):
                    if pair["repo"] in self.busy_repos or pair_key(pair) in self.busy_keys:
                        continue
                    del self.pending[position]
                    self.busy_repos.add(pair["repo"])
                    self.busy_keys.add(pair_key(pair))
                    self.active[pair["pair_id"]] = pair
                    return pair
                self.condition.wait(timeout=1.0)

    def done(self, pair: dict) -> None:
        with self.condition:
            self.busy_repos.discard(pair["repo"])
            self.busy_keys.discard(pair_key(pair))
            self.active.pop(pair["pair_id"], None)
            if self.after_pair_hook is not None:
                self.after_pair_hook(self)
            self.condition.notify_all()

    def versions_in_use(self, lookahead: int) -> set[str]:
        """Toolchains of running pairs and of the next ``lookahead`` pairs."""
        versions: set[str] = set()
        for pair in [*self.active.values(), *self.pending[:lookahead]]:
            versions.add(pair["old_version"])
            versions.add(pair["new_version"])
        return versions

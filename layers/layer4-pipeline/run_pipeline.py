#!/usr/bin/env python3
"""Resumable Lean version-transition build/error pipeline."""

from __future__ import annotations

import argparse
from contextlib import closing
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import csv
import gzip
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, NamedTuple

from commit_to_version import discover_pairs, ensure_full_history, git, write_jsonl_atomic
from checkpoint_safety import state_lock, backup_results, archive_attempt_logs
from extract_lean_errors import enrich_lean_error, parse_log, write_report_atomic, _relative_source_path, required_build_output
from resource_management import (
    CacheGate,
    MathlibCacheJanitor,
    PairScheduler,
    ToolchainJanitor,
    WorktreeTrash,
    directory_size,
    enable_ntfs_compression,
    lean_minor,
    load_pair_metadata,
    pair_priority,
)
from result_validation import (
    PREDICTED_TIMEOUT_OUTCOME,
    RESULT_FORMAT_VERSION,
    classify_adapted_build,
    empty_build,
    has_build_evidence,
    has_toolchain_evidence,
    immutable_toolchain,
    infrastructure_failure,
    log_exit_code,
    log_source_root,
    toolchain_mismatch,
)


HERE = Path(__file__).resolve().parent
DEFAULT_COLLECTION = HERE.parent / "lean-multi-toolchain-repos"
DEFAULT_MANIFEST = DEFAULT_COLLECTION / "qualifying-repositories.csv"
DEFAULT_REPOS = DEFAULT_COLLECTION / "repos"
DEFAULT_STATE = HERE / "state"
DEFAULT_ERRORS = HERE / "parsed-errors"
DEFAULT_NO_LEAN_ERRORS = HERE / "pairs-without-lean-errors.jsonl"
DEFAULT_REPAIR_DIFFS = HERE / "repair-diffs"
DEFAULT_WORKTREES = Path(tempfile.gettempdir()) / "lvp"
LAKE_MANIFEST = "lake-manifest.json"
NATIVE_CHECK_PROTOCOL_VERSION = "5"
STOP_EVENT = threading.Event()
DB_WRITE_LOCK = threading.Lock()
EXPORT_LOCK = threading.Lock()
CACHE_GATE = CacheGate()
TIMEOUT_EXIT_CODE = 124
_KEEP_TOOLCHAIN_SUPPORT: dict[str, bool] = {}
_LAKE_HELP: dict[str, str] = {}
# Local bare mirrors of the dependencies nearly every Lean project re-clones
# from GitHub.  Default on the %TEMP% volume so clones can hardlink the packs.
DEFAULT_DEP_MIRROR_ROOT = (
    Path(os.environ.get("USERPROFILE") or Path.home()) / ".cache" / "lvp-dep-mirrors"
)
# Allowlist by normalized URL (lowercase, no trailing "/" or ".git").
# ProofWidgets is deliberately absent: its preferReleaseBuild download reads
# the remote URL, so it is always left to Lake.
DEPENDENCY_MIRROR_URLS = frozenset({
    "https://github.com/leanprover-community/mathlib4",
    "https://github.com/leanprover-community/batteries",
    "https://github.com/leanprover/std4",
    "https://github.com/leanprover-community/aesop",
    "https://github.com/leanprover-community/quote4",
    "https://github.com/leanprover-community/import-graph",
    "https://github.com/leanprover/lean4-cli",
    "https://github.com/leanprover-community/plausible",
    "https://github.com/leanprover-community/leansearchclient",
})
_MIRROR_LOCKS: dict[str, threading.Lock] = {}
_MIRROR_LOCKS_GUARD = threading.Lock()
# Set by run_pairs; None means worktrees are deleted inline (--no-async-cleanup).
WORKTREE_TRASH: WorktreeTrash | None = None
# Lake's exit code when --no-build finds a target out of date.
LAKE_NO_BUILD_EXIT_CODE = 3
# ProofWidgets rebuilds cascade into a full Mathlib rebuild on Lake < v4.16.
PROOFWIDGETS_CASCADE_FIRST_SAFE_MINOR = 16
PREDICTION_TIMEOUT = 300
REUSED_STAGE = "reused-from-fork"
DUPLICATE_TIMEOUT_STAGE = "skipped-duplicate-timeout"


# These failures say that the native check did not complete reliably; they do
# not say that the historical source is broken in its own Lean toolchain.
INCONCLUSIVE_NATIVE_EXIT_CODES = {
    124,  # timeout assigned by run_logged
    130,  # interrupted on POSIX / by this pipeline
    137,  # killed (commonly out of memory)
    143,  # terminated
    3221225786,  # Windows STATUS_CONTROL_C_EXIT (0xC000013A)
    -1073741510,  # the same Windows status represented as a signed integer
}
INCONCLUSIVE_NATIVE_PATTERNS = (
    "filename too long",
    "rpc failed",
    "early eof",
    "unexpected disconnect",
    "invalid index-pack output",
    "remote end hung up unexpectedly",
    "the remote end hung up unexpectedly",
    "could not resolve host",
    "couldn't resolve host",
    "failed to connect",
    "connection timed out",
    "connection reset",
    "connection was reset",
    "connection closed",
    "network is unreachable",
    "operation timed out",
    "tls connection",
    "ssl connection",
    "http/2 stream",
    "failed to receive http2 data",
    "unable to access '",
    "unable to access \"",
    "could not read from remote repository",
    "unable to checkout working tree",
    "failed to clone",
    "failed to fetch",
    "invalid path",
    "path too long",
    "failed to create file",
    "failed to write",
    "failed to open output file",
    "access is denied",
    "permission denied",
    "no space left on device",
    "disk full",
    "not enough space on the disk",
    "cannot allocate memory",
    "out of memory",
    "failed to download",
    "error downloading",
    "download failed",
    "checksum failed",
    "toolchain download",
    "couldn't find value of elan_home",
    "could not find value of elan_home",
    "error: command timed out",
    "error: pipeline interrupted",
)


class PipelineInterrupted(Exception):
    pass


# Failures that say this machine could not reach the network or the remote.
# They say nothing about the pair: the pair stays pending and is retried on
# the next run, and repeated failures pause the run instead of burning through
# the queue (the clones are blobless, so even `git worktree add` fetches).
NETWORK_FAILURE_PATTERNS = (
    "could not resolve host",
    "couldn't resolve host",
    "failed to connect",
    "could not connect to server",
    "connection timed out",
    "connection reset",
    "connection was reset",
    "connection closed",
    "network is unreachable",
    "operation timed out",
    "tls connection",
    "ssl connection",
    "http/2 stream",
    "failed to receive http2 data",
    "unable to access '",
    "unable to access \"",
    "could not read from remote repository",
    "from promisor remote",
    "remote end hung up unexpectedly",
    "rpc failed",
    "early eof",
    "unexpected disconnect",
    "invalid index-pack output",
    "failed to download",
    "error downloading",
    "download failed",
)
# Git refuses these paths on this platform (Windows device names such as
# `Aux.lean` or `Con.lean`, `:` or `?` in a name).  No retry here can succeed.
INVALID_PATH_PATTERNS = ("invalid path '", 'invalid path "')
NETWORK_FAILURE_OUTCOME = "network-failure"
INVALID_PATH_OUTCOME = "invalid-path-on-platform"
NETWORK_REPAIR_VERSION = "1"
NETWORK_PROBE_HOST = "github.com"
# A repository whose pairs are in one of these states may still need its clone.
UNFINISHED_PAIR_STATUSES = (
    "pending", "running", "native_inconclusive", "build_inconclusive", "pipeline_failed",
)


def network_failure_text(text: str | None) -> bool:
    folded = (text or "").casefold()
    return any(pattern in folded for pattern in NETWORK_FAILURE_PATTERNS)


def invalid_path_text(text: str | None) -> bool:
    folded = (text or "").casefold()
    return any(pattern in folded for pattern in INVALID_PATH_PATTERNS)


class NetworkWatchdog:
    """Pause the run when network failures repeat instead of failing every pair.

    Consecutive network failures are counted across workers and a success
    resets the count.  At the limit, the probe host is contacted: if it is
    reachable the failures were specific to one remote and the count restarts;
    otherwise the stop event is set, so active pairs return to ``pending``
    exactly as after an interruption.
    """

    def __init__(
        self,
        limit: int = 5,
        *,
        stop: threading.Event | None = None,
        probe: Callable[[], bool] | None = None,
    ) -> None:
        self.limit = max(0, int(limit))
        self.stop = stop if stop is not None else STOP_EVENT
        self.probe = probe or self.host_reachable
        self.consecutive = 0
        self.total = 0
        self.tripped = False
        self.lock = threading.Lock()

    def configure(self, limit: int) -> None:
        with self.lock:
            self.limit = max(0, int(limit))
            self.consecutive = 0
            self.total = 0
            self.tripped = False

    @staticmethod
    def host_reachable(host: str = NETWORK_PROBE_HOST, timeout: float = 10.0) -> bool:
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            return False

    def success(self) -> None:
        with self.lock:
            self.consecutive = 0

    def failure(self, detail: str = "") -> bool:
        """Record one network failure; return True when the run is pausing."""
        with self.lock:
            self.consecutive += 1
            self.total += 1
            if self.tripped:
                return True
            if not self.limit or self.consecutive < self.limit:
                return False
            if self.probe():
                self.consecutive = 0  # one remote is unreachable, not the network
                return False
            self.tripped = True
            count = self.consecutive
        print(
            f"Pausing: {count} consecutive network failures and {NETWORK_PROBE_HOST} "
            "is unreachable. Affected pairs stay pending; re-run the same command "
            "once connectivity is restored.",
            file=sys.stderr,
            flush=True,
        )
        self.stop.set()
        return True


NETWORK_WATCHDOG = NetworkWatchdog()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", value).strip("._-") or "item"


def version_slug(value: str, limit: int = 55) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "unknown"
    return cleaned[:limit]


def pair_identifier(repo: str, newer: str, older: str) -> str:
    digest = hashlib.sha256(f"{repo}\0{newer}\0{older}".encode("utf-8")).hexdigest()
    return digest[:20]


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def command_prefix(name: str) -> list[str]:
    resolved = shutil.which(name)
    if resolved is None:
        candidate = Path(name)
        if candidate.exists():
            resolved = str(candidate.resolve())
        else:
            raise FileNotFoundError(f"Required command not found: {name}")
    if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", resolved]
    return [resolved]


def append_git_config(environment: dict[str, str], key: str, value: str) -> None:
    """Append one Git config entry without clobbering inherited entries."""
    try:
        index = int(environment.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        index = 0
    while f"GIT_CONFIG_KEY_{index}" in environment:
        index += 1
    environment[f"GIT_CONFIG_KEY_{index}"] = key
    environment[f"GIT_CONFIG_VALUE_{index}"] = value
    environment["GIT_CONFIG_COUNT"] = str(index + 1)


def child_process_environment() -> dict[str, str]:
    """Environment shared by Lake, lakeva, and the Git processes they spawn."""
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # Never allow the caller's ambient toolchain or module search path to
    # override the committed lean-toolchain in a historical checkout.
    environment.pop("ELAN_TOOLCHAIN", None)
    environment.pop("LEAN_TOOLCHAIN", None)
    environment.pop("LEAN_PATH", None)
    environment.pop("LEAN_SYSROOT", None)
    environment.pop("LAKE_HOME", None)
    if not environment.get("ELAN_HOME"):
        elan_home = Path.home() / ".elan"
        if elan_home.is_dir():
            environment["ELAN_HOME"] = str(elan_home)
    # Our direct Git wrapper already uses -c core.longpaths=true.  Lake starts
    # its own Git processes for dependencies, so they need the setting through
    # the environment as well.  HTTP/1.1 avoids recurrent GitHub HTTP/2 stream
    # resets seen in the native-check logs.
    append_git_config(environment, "core.longpaths", "true")
    append_git_config(environment, "http.version", "HTTP/1.1")
    return environment


def committed_toolchain(worktree: Path, fallback: str | None = None) -> str:
    path = worktree / "lean-toolchain"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            value = line.strip()
            if value:
                return value
    return (fallback or "").strip()


def lake_supports_keep_toolchain(lake_command: list[str], worktree: Path) -> bool:
    """Probe each historical Lake release once; old releases lack the flag."""
    version = committed_toolchain(worktree)
    # Test doubles and callers that do not provide a historical checkout do
    # not have enough information to select a version-specific Lake option.
    if not version:
        return False
    key = "\0".join(lake_command) + "\0" + version
    cached = _KEEP_TOOLCHAIN_SUPPORT.get(key)
    if cached is not None:
        return cached
    try:
        environment = child_process_environment()
        environment['ELAN_TOOLCHAIN'] = version
        result = subprocess.run(
            [*lake_command, "--help"],
            cwd=worktree,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        supported = result.returncode == 0 and "--keep-toolchain" in result.stdout
    except (OSError, subprocess.SubprocessError):
        supported = False
    _KEEP_TOOLCHAIN_SUPPORT[key] = supported
    if 'result' in locals() and result.returncode == 0:
        _LAKE_HELP[key] = result.stdout
    return supported


def lake_command_for(
    lake_command: list[str], worktree: Path, args: list[str]
) -> list[str]:
    prefix = [*lake_command]
    if lake_supports_keep_toolchain(lake_command, worktree):
        prefix.append("--keep-toolchain")
    key = '\0'.join(lake_command) + '\0' + committed_toolchain(worktree)
    if args == ['build'] and '--verbose' in _LAKE_HELP.get(key, ''):
        prefix.append('--verbose')
    return [*prefix, *args]


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_logged(command: list[str], cwd: Path, log_path: Path, timeout: int, *, toolchain: str | None = None) -> int:
    if STOP_EVENT.is_set():
        raise PipelineInterrupted("pipeline interrupted")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = child_process_environment()
    if toolchain is not None:
        actual = committed_toolchain(cwd)
        if actual != toolchain:
            raise RuntimeError(f'Wrong toolchain in {cwd}: expected {toolchain!r}, got {actual!r}')
        environment['ELAN_TOOLCHAIN'] = toolchain
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with log_path.open("w", encoding="utf-8", errors="replace", newline="\n") as log:
        log.write(f"# started: {now()}\n")
        log.write(f"# cwd: {cwd}\n")
        log.write(f"# command: {subprocess.list2cmdline(command)}\n\n")
        if toolchain is not None:
            log.write(f'# toolchain: {toolchain}\n')
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        deadline = time.monotonic() + timeout
        interrupted = False
        while process.poll() is None:
            if STOP_EVENT.wait(0.5):
                terminate_process_tree(process)
                process.wait()
                log.write("\nerror: pipeline interrupted\n")
                return_code = 130
                interrupted = True
                break
            if time.monotonic() >= deadline:
                terminate_process_tree(process)
                process.wait()
                log.write(f"\nerror: command timed out after {timeout} seconds\n")
                return_code = 124
                break
        else:
            return_code = process.returncode
        if toolchain is not None:
            log.write(f'\n# toolchain_after: {committed_toolchain(cwd)}\n')
            if command[-1] == 'build':
                count = sum(1 for directory in (cwd / '.lake/build/lib', cwd / 'build/lib')
                            if directory.is_dir() for _ in directory.rglob('*.olean'))
                log.write(f'# root_olean_count: {count}\n')
        log.write(f"\n# finished: {now()}\n# exit_code: {return_code}\n")
    if interrupted:
        raise PipelineInterrupted("pipeline interrupted")
    return return_code


def normalize_dependency_url(url: str) -> str:
    value = url.strip().casefold().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value.rstrip("/")


def mirror_allowlisted(url: str) -> bool:
    return normalize_dependency_url(url) in DEPENDENCY_MIRROR_URLS


def dependency_mirror_root(args) -> Path | None:
    """The mirror directory, or None when mirroring is disabled."""
    if getattr(args, "no_dep_mirror", False):
        return None
    root = getattr(args, "dep_mirror_root", None)
    return Path(root) if root else None


def dependency_mirror_path(root: Path, url: str) -> Path:
    normalized = normalize_dependency_url(url)
    return root / (slug(normalized.split("://", 1)[-1]) + ".git")


def _mirror_lock(mirror: Path) -> threading.Lock:
    key = str(mirror).casefold()
    with _MIRROR_LOCKS_GUARD:
        lock = _MIRROR_LOCKS.get(key)
        if lock is None:
            lock = _MIRROR_LOCKS[key] = threading.Lock()
    return lock


def mirror_git(
    arguments: list[str], cwd: Path, timeout: int, *, network: bool = False
) -> subprocess.CompletedProcess:
    """Git for mirror maintenance: pipeline environment, no prompts, never raises."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.longpaths=true", *arguments],
            cwd=cwd,
            env=child_process_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result = subprocess.CompletedProcess(arguments, 1, "", str(exc))
    if network:
        if result.returncode == 0:
            NETWORK_WATCHDOG.success()
        elif network_failure_text(result.stderr + result.stdout):
            NETWORK_WATCHDOG.failure(result.stderr + result.stdout)
    return result


def _git_detail(result: subprocess.CompletedProcess) -> str:
    return ((result.stderr or result.stdout or "").strip() or "git failed")[-300:]


def mirror_has_rev(mirror: Path, rev: str) -> bool:
    return mirror_git(["cat-file", "-e", f"{rev}^{{commit}}"], mirror, 60).returncode == 0


def fetch_dependency_mirror(
    mirror: Path, timeout: int, *, rev: str | None = None, network: bool = True
) -> str | None:
    """Refresh one mirror (and fetch ``rev`` by hash if still absent); error text or None."""
    result = mirror_git(["fetch", "--prune", "--quiet", "origin"], mirror, timeout, network=network)
    if result.returncode != 0:
        return _git_detail(result)
    if rev is not None and not mirror_has_rev(mirror, rev):
        # A commit reachable only from an unlisted ref can still be fetched by hash.
        result = mirror_git(["fetch", "--quiet", "origin", rev], mirror, timeout, network=network)
        if result.returncode != 0:
            return _git_detail(result)
    return None


def ensure_dependency_mirror(
    root: Path, url: str, rev: str, timeout: int
) -> tuple[Path | None, str | None]:
    """Return (mirror, None) once the mirror holds ``rev``, else (None, reason).

    The mirror is created on first use and refreshed on demand; one lock per
    mirror keeps two workers from fetching the same mirror concurrently.
    """
    mirror = dependency_mirror_path(root, url)
    with _mirror_lock(mirror):
        if not (mirror / "HEAD").is_file():
            partial = mirror.with_name(mirror.name + ".partial")
            for leftover in (mirror, partial):
                if leftover.exists():
                    shutil.rmtree(leftover, onerror=_make_writable_and_retry)
            root.mkdir(parents=True, exist_ok=True)
            result = mirror_git(
                ["clone", "--bare", "--quiet", url, str(partial)],
                root, max(timeout, 3600), network=True,
            )
            if result.returncode != 0:
                shutil.rmtree(partial, ignore_errors=True)
                return None, _git_detail(result)
            # Track every branch and tag, but not GitHub's pull-request refs.
            mirror_git(["config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"], partial, 60)
            mirror_git(["config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*"], partial, 60)
            os.replace(partial, mirror)
        if mirror_has_rev(mirror, rev):
            return mirror, None
        error = fetch_dependency_mirror(mirror, timeout, rev=rev)
        if error:
            return None, error
        if mirror_has_rev(mirror, rev):
            return mirror, None
        return None, f"revision {rev} is not reachable from {url}"


def clone_from_mirror(mirror: Path, url: str, rev: str, directory: Path, timeout: int) -> str | None:
    """Clone from the local mirror (packs are hardlinked), then make it look like Lake's clone.

    Origin is set to the manifest's exact URL string so Lake's same-URL check
    passes, and HEAD is detached at ``rev`` so Lake's materialization sees an
    up-to-date checkout and does nothing.  Nothing is left behind on failure.
    """
    directory.parent.mkdir(parents=True, exist_ok=True)
    steps = (
        ["clone", "--quiet", "--no-checkout", str(mirror), str(directory)],
        ["-C", str(directory), "remote", "set-url", "origin", url],
        ["-C", str(directory), "checkout", "--quiet", "--detach", rev],
    )
    for arguments in steps:
        result = mirror_git(arguments, directory.parent, timeout)
        if result.returncode != 0:
            if directory.exists():
                shutil.rmtree(directory, onerror=_make_writable_and_retry)
            return _git_detail(result)
    return None


def manifest_git_packages(manifest: dict) -> list[tuple[str, str, str]]:
    """(name, url, rev) of every Git dependency, across the manifest schema families.

    Manifest versions 4-5 nest the record as ``{"git": {...}}``; versions 6-7
    and the "1.x.0" string versions use flat records with ``"type": "git"``.
    """
    packages: list[tuple[str, str, str]] = []
    for entry in manifest.get("packages") or []:
        if not isinstance(entry, dict):
            continue
        record = entry.get("git") if isinstance(entry.get("git"), dict) else entry
        if record is entry and (entry.get("type") not in (None, "git") or "url" not in entry):
            continue
        name, url, rev = record.get("name"), record.get("url"), record.get("rev")
        if all(isinstance(value, str) and value for value in (name, url, rev)):
            packages.append((name, url, rev))
    return packages


def manifest_package_names(manifest: dict) -> set[str]:
    names: set[str] = set()
    for entry in manifest.get("packages") or []:
        if not isinstance(entry, dict):
            continue
        for record in (entry, entry.get("git"), entry.get("path")):
            if isinstance(record, dict) and isinstance(record.get("name"), str):
                names.add(record["name"].casefold())
    return names


def read_manifest(worktree: Path) -> dict | None:
    path = worktree / LAKE_MANIFEST
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def manifest_packages_dir(worktree: Path, manifest: dict) -> Path | None:
    """Where Lake clones dependencies for this manifest (inside the worktree)."""
    value = manifest.get("packagesDir")
    if isinstance(value, str) and value.strip():
        candidate = Path(value.strip().replace("\\", "/"))
        directory = candidate if candidate.is_absolute() else worktree / candidate
    else:
        version = manifest.get("version")
        try:
            numeric = int(version)
        except (TypeError, ValueError):
            numeric = None
        # Manifests 4-6 (Lake < v4.3) used lake-packages; 7 and the string
        # versions use .lake/packages.
        directory = worktree / ("lake-packages" if numeric is not None and numeric <= 6 else ".lake/packages")
    return directory if within(directory, worktree) else None


def seed_dependency_clones(
    worktree: Path, log_path: Path, mirror_root: Path | None = None, *, timeout: int = 1800
) -> int:
    """Pre-clone allowlisted dependencies from the local mirrors before Lake runs.

    For every Git package in the worktree's manifest that is allowlisted and
    whose package directory does not exist yet, a clone is created from the
    mirror with the manifest's exact URL as origin and HEAD detached at the
    manifest's revision.  Lake then finds an up-to-date checkout and skips the
    download.  Any failure removes the partial directory and lets Lake clone
    as it would without the mirror.  Returns the number of seeded packages.
    """
    manifest = read_manifest(worktree)
    if mirror_root is None or manifest is None:
        return 0
    packages_dir = manifest_packages_dir(worktree, manifest)
    if packages_dir is None:
        return 0
    seeded = 0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        for name, url, rev in manifest_git_packages(manifest):
            if not mirror_allowlisted(url):
                continue
            directory = packages_dir / name
            if directory.exists():
                continue
            if STOP_EVENT.is_set():
                raise PipelineInterrupted("pipeline interrupted")
            mirror, reason = ensure_dependency_mirror(mirror_root, url, rev, timeout)
            if mirror is None:
                log.write(f"# not seeded: {name} {rev}: {reason}\n")
                continue
            error = clone_from_mirror(mirror, url, rev, directory, timeout)
            if error:
                log.write(f"# not seeded: {name} {rev}: {error}\n")
                continue
            log.write(f"# seeded: {name} {rev} from mirror {mirror}\n")
            seeded += 1
    return seeded


def refresh_dependency_mirrors(root: Path | None, timeout: int = 1800) -> int:
    """Fetch every existing mirror once (run start); failures only log."""
    if root is None or not root.is_dir():
        return 0
    refreshed = 0
    failures: list[str] = []
    for mirror in sorted(root.glob("*.git")):
        if not (mirror / "HEAD").is_file():
            continue
        with _mirror_lock(mirror):
            error = fetch_dependency_mirror(mirror, timeout, network=False)
        if error:
            failures.append(f"{mirror.name}: {error}")
        else:
            refreshed += 1
    message = f"Refreshed {refreshed} dependency mirror(s) in {root}"
    if failures:
        message += f"; {len(failures)} could not be refreshed ({failures[0]})"
    print(message, flush=True)
    return refreshed


def dependency_mirror_summary(root: Path | None) -> tuple[int, int]:
    """(mirror count, bytes) for the status output."""
    if root is None or not root.is_dir():
        return 0, 0
    mirrors = [path for path in root.glob("*.git") if (path / "HEAD").is_file()]
    return len(mirrors), sum(directory_size(path) for path in mirrors)


def manifest_mentions_mathlib(worktree: Path) -> bool:
    """Mathlib's cache executable exists only when Mathlib is in the graph.

    Without a manifest the dependency graph is unknown, so the cache step runs.
    """
    manifest = worktree / LAKE_MANIFEST
    if not manifest.is_file():
        return True
    return "mathlib" in manifest.read_text(encoding="utf-8", errors="replace").casefold()


def run_cache_get(args, lake_command: list[str], worktree: Path, log_path: Path, toolchain: str) -> int:
    """Run `lake exe cache get` unless it cannot help; -1 means skipped."""
    if args.skip_cache or not manifest_mentions_mathlib(worktree):
        return -1
    # Shared with the Mathlib-cache pruner so archives never vanish mid-unpack.
    with CACHE_GATE:
        return run_logged(
            lake_command_for(lake_command, worktree, ["exe", "cache", "get"]),
            worktree,
            log_path,
            args.cache_timeout,
            toolchain=toolchain,
        )


def lake_help_text(lake_command: list[str], worktree: Path) -> str:
    """The probed `lake --help` text for this checkout's Lake release ("" if unknown)."""
    lake_supports_keep_toolchain(lake_command, worktree)
    key = "\0".join(lake_command) + "\0" + committed_toolchain(worktree)
    return _LAKE_HELP.get(key, "")


def proofwidgets_cascade_predicted(
    args, lake_command: list[str], worktree: Path, version: str, log_path: Path, cache_rc: int
) -> bool:
    """Whether `lake build` is certain to rebuild all of Mathlib and time out.

    On Lake < v4.16 a ProofWidgets rebuild invalidates every cached Mathlib
    artifact.  After a successful `cache get`, `lake build +ProofWidgets.Compat
    --no-build` exits with Lake's no-build code when ProofWidgets is out of
    date; that is the cascade.  Prediction is skipped for nightlies, v4.16+,
    manifests without both mathlib and proofwidgets, when the cache step did
    not succeed, and for Lake releases whose help does not list --no-build.
    """
    if getattr(args, "no_timeout_prediction", False) or cache_rc != 0:
        return False
    minor = lean_minor(version)
    if minor is None or minor >= PROOFWIDGETS_CASCADE_FIRST_SAFE_MINOR:
        return False
    manifest = read_manifest(worktree)
    if manifest is None:
        return False
    names = manifest_package_names(manifest)
    if "mathlib" not in names or "proofwidgets" not in names:
        return False
    if "--no-build" not in lake_help_text(lake_command, worktree):
        return False
    rc = run_logged(
        lake_command_for(lake_command, worktree, ["build", "+ProofWidgets.Compat", "--no-build"]),
        worktree,
        log_path,
        PREDICTION_TIMEOUT,
        toolchain=version,
    )
    predicted = rc == LAKE_NO_BUILD_EXIT_CODE
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        if predicted:
            log.write("# predicted: ProofWidgets is out of date; Mathlib would be rebuilt from "
                      "source, which exceeds the build time limit. Build skipped.\n")
        else:
            log.write(f"# predicted: no cascade (exit {rc}); building normally.\n")
    return predicted


def native_failure_is_inconclusive(
    cache_log: Path,
    build_log: Path,
    cache_exit_code: int | None,
    build_exit_code: int | None,
    extra_logs: tuple[Path, ...] = (),
) -> bool:
    """Return whether a failed native run was environmental, not source proof."""
    if not build_log.is_file() or build_exit_code is None:
        return True
    if build_exit_code in INCONCLUSIVE_NATIVE_EXIT_CODES:
        return True

    build_content = ""
    if build_log.is_file():
        build_content = required_build_output(build_log.read_text(
            encoding="utf-8", errors="replace"
        )).casefold()
        # Infrastructure errors emitted by Lean itself can be location-shaped
        # diagnostics (for example, failure to create an .olean on a long
        # Windows path), so explicit build-side infrastructure wins first.
        if any(
            pattern in build_content for pattern in INCONCLUSIVE_NATIVE_PATTERNS
        ):
            return True
        if (
            "unknown short option '-v'" in build_content
            or 'unknown short option "-v"' in build_content
        ):
            return True

        # A real Lean diagnostic takes precedence over warnings or failures in
        # the earlier optional cache-download step.
        try:
            if any(
                error.get("kind") == "lean-diagnostic"
                for error in parse_log(build_log)
            ):
                return False
        except (OSError, UnicodeError):
            pass

    if cache_exit_code in INCONCLUSIVE_NATIVE_EXIT_CODES:
        return True

    pieces: list[str] = []
    for path in (cache_log, build_log, *extra_logs):
        if path.is_file():
            pieces.append(required_build_output(path.read_text(encoding="utf-8", errors="replace")))
    combined = "\n".join(pieces).casefold()
    if any(pattern in combined for pattern in INCONCLUSIVE_NATIVE_PATTERNS):
        return True

    # A Git child failure without a Lean diagnostic means dependency setup did
    # not complete.  It cannot establish that the source itself is broken.
    git_child_failed = (
        "external command 'git' exited" in combined
        or 'external command `git` exited' in combined
        or 'external command "git" exited' in combined
    )
    if git_child_failed:
        return True
    if cache_exit_code not in (None, -1, 0) and "fatal:" in combined:
        return True
    return False


def native_pass_is_valid(row, expected_version: str) -> bool:
    if not row or row['status'] != 'passed' or row['version'] != expected_version:
        return False
    directory = Path(row['log_dir'])
    build = directory / 'build.log'
    return (
        immutable_toolchain(expected_version)
        and log_exit_code(build) == 0
        and has_build_evidence(build)
        and has_toolchain_evidence(build, expected_version)
        and not empty_build(build)
        and not parse_log(build)
        and not toolchain_mismatch(directory / 'cache.log', expected_version)
    )


def ensure_native_manifest(args, repo_name, commit_hash, version, worktree, lake_command) -> bool:
    """Some dependency-free historical builds never create a manifest."""
    snapshot = native_manifest_snapshot(args, repo_name, commit_hash)
    if snapshot.is_file():
        return True
    manifest = worktree / LAKE_MANIFEST
    if not manifest.is_file():
        log = native_log_dir(args, repo_name, commit_hash) / 'manifest-recovery.log'
        rc = run_logged(lake_command_for(lake_command, worktree, ['update']),
                        worktree, log, args.cache_timeout, toolchain=version)
        if rc != 0 or toolchain_mismatch(log, version):
            return False
    if not manifest.is_file():
        return False
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest, snapshot)
    return True


def log_requests_lake_update(path: Path) -> bool:
    if not path.is_file():
        return False
    content = path.read_text(encoding="utf-8", errors="replace").casefold()
    return (
        "use `lake update" in content
        or "run `lake update" in content
        or "not in manifest" in content
        or "manifest out of date" in content
    )


def tail(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def connect_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = open_database(path, autocommit=False)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS repos (
            repo TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            pair_count INTEGER,
            error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairs (
            pair_id TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            pair_index INTEGER NOT NULL,
            old_hash TEXT NOT NULL,
            new_hash TEXT NOT NULL,
            old_version TEXT NOT NULL,
            new_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            outcome TEXT,
            last_stage TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            error_file TEXT,
            error TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            UNIQUE(repo, new_hash, old_hash)
        );
        CREATE INDEX IF NOT EXISTS pairs_status_idx ON pairs(status, active, repo, pair_index);
        CREATE TABLE IF NOT EXISTS native_checks (
            repo TEXT NOT NULL,
            commit_hash TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL,
            cache_exit_code INTEGER,
            build_exit_code INTEGER,
            log_dir TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(repo, commit_hash)
        );
        CREATE TABLE IF NOT EXISTS error_exports (
            pair_id TEXT PRIMARY KEY,
            format_version INTEGER NOT NULL,
            error_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pipeline_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    repo_columns = {row[1] for row in connection.execute("PRAGMA table_info(repos)")}
    if "clone_deleted" not in repo_columns:
        connection.execute("ALTER TABLE repos ADD COLUMN clone_deleted INTEGER NOT NULL DEFAULT 0")
    pair_columns = {row[1] for row in connection.execute("PRAGMA table_info(pairs)")}
    for column in ("build_exit_code", "cache_exit_code", "result_version"):
        if column not in pair_columns:
            connection.execute(f"ALTER TABLE pairs ADD COLUMN {column} INTEGER")
    connection.execute(
        "UPDATE pairs SET status='pending', last_stage='resuming-after-interruption', updated_at=? "
        "WHERE status='running'",
        (now(),),
    )
    connection.execute(
        "UPDATE repos SET status='pending', error='interrupted during discovery', updated_at=? "
        "WHERE status='discovering'",
        (now(),),
    )
    connection.commit()
    return connection


def migrate_native_check_protocol(
    connection: sqlite3.Connection,
    args=None,
) -> None:
    """Invalidate results made before strict toolchain/build validation."""
    row = connection.execute(
        "SELECT value FROM pipeline_meta WHERE key='native-check-protocol'"
    ).fetchone()
    if row and row["value"] == NATIVE_CHECK_PROTOCOL_VERSION:
        return

    rows = connection.execute(
        "SELECT repo,commit_hash,version,status,log_dir FROM native_checks"
    ).fetchall()
    invalidated_hashes: set[tuple[str, str]] = set()
    for row in rows:
        log_dir = Path(row['log_dir'])
        invalidate = False
        if row['status'] == 'failed':
            invalidate = native_failure_is_inconclusive(
                log_dir / 'cache.log', log_dir / 'build.log',
                log_exit_code(log_dir / 'cache.log'), log_exit_code(log_dir / 'build.log'),
            )
            invalidate = invalidate or (
                not immutable_toolchain(row['version'])
                or not has_toolchain_evidence(log_dir / 'build.log', row['version'])
                or toolchain_mismatch(log_dir / 'cache.log', row['version'])
                or empty_build(log_dir / 'build.log')
            )
        if row["status"] == "passed":
            # A prior pass is not trustworthy if Lake silently used another
            # toolchain or completed without compiling any target.
            invalidate = not native_pass_is_valid(row, row['version'])
        if invalidate:
            invalidated_hashes.add((row["repo"], row["commit_hash"]))

    for repo, commit_hash in invalidated_hashes:
        connection.execute(
            "DELETE FROM native_checks WHERE repo=? AND commit_hash=?",
            (repo, commit_hash),
        )

    pairs_to_requeue = connection.execute(
        "SELECT * FROM pairs WHERE status IN "
        "('discarded_native','native_inconclusive','completed','adapt_failed','pipeline_failed','build_inconclusive')"
    ).fetchall()
    requeued = 0
    for pair in pairs_to_requeue:
        if any(
            (pair["repo"], value) in invalidated_hashes
            for value in (pair["old_hash"], pair["new_hash"])
        ):
            if args is not None:
                clear_pair_error_files(args, pair)
            connection.execute(
                "UPDATE pairs SET status='pending',outcome=NULL,last_stage='native-evidence-recheck',"
                "error=NULL,error_file=NULL,result_version=NULL,updated_at=? WHERE pair_id=?",
                (now(), pair["pair_id"]),
            )
            requeued += 1
    # Same-version pairs and moving aliases cannot measure a historical
    # transition. Retain their records/logs but remove them from the dataset.
    for pair in connection.execute('SELECT * FROM pairs WHERE active=1').fetchall():
        if pair['old_version'] == pair['new_version'] or not all(
            immutable_toolchain(pair[key]) for key in ('old_version', 'new_version')
        ):
            if args is not None:
                clear_pair_error_files(args, pair)
            connection.execute(
                "UPDATE pairs SET status='ineligible_pair',outcome=?,error_file=NULL,updated_at=? WHERE pair_id=?",
                ('identical-toolchains' if pair['old_version'] == pair['new_version'] else 'moving-toolchain-alias', now(), pair['pair_id']),
            )
    connection.execute(
        """
        INSERT INTO pipeline_meta(key,value,updated_at)
        VALUES('native-check-protocol',?,?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (NATIVE_CHECK_PROTOCOL_VERSION, now()),
    )
    connection.commit()
    if invalidated_hashes or requeued:
        print(
            "Native-check fix: invalidated "
            f"{len(invalidated_hashes)} old native result(s) and requeued {requeued} pair(s).",
            flush=True,
        )


def open_database(path: Path, *, autocommit: bool = True) -> sqlite3.Connection:
    """Open SQLite with bounded lock waiting and no long-lived worker reads."""
    connection = sqlite3.connect(
        path,
        timeout=10,
        isolation_level=None if autocommit else "DEFERRED",
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def commit_database_write(
    connection: sqlite3.Connection,
    operation: Callable[[], None],
    *,
    attempts: int = 6,
) -> None:
    """Serialize worker writes and retry locks held by another process."""
    with DB_WRITE_LOCK:
        for attempt in range(attempts):
            try:
                operation()
                connection.commit()
                return
            except sqlite3.OperationalError as exc:
                connection.rollback()
                if "locked" not in str(exc).casefold():
                    raise
                if attempt + 1 == attempts:
                    raise RuntimeError(
                        "progress database remained locked after repeated retries; "
                        "close other programs using progress.sqlite3 and rerun"
                    ) from exc
                if STOP_EVENT.wait(min(0.25 * (2**attempt), 2.0)):
                    raise PipelineInterrupted("pipeline interrupted") from exc


def validate_errors_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for temporary in path.glob('*.json.tmp'):
        temporary.unlink()  # interrupted atomic export; the source log is kept
    unexpected = [entry for entry in path.iterdir() if not entry.is_file() or entry.suffix.lower() != ".json"]
    if unexpected:
        names = ", ".join(entry.name for entry in unexpected[:10])
        raise RuntimeError(
            f"Parsed-error directory contains non-JSON items ({names}). "
            "Move them elsewhere so this directory contains solely parsed error reports."
        )


def migrate_legacy_adapter_reports(args, connection: sqlite3.Connection) -> None:
    """Remove old adapter failures that were incorrectly filed as parsed Lean errors."""
    rows = connection.execute(
        "SELECT pair_id,error_file FROM pairs WHERE outcome='lakeva-adapt-errors'"
    ).fetchall()
    if not rows:
        return
    for row in rows:
        value = row["error_file"]
        if value:
            report = Path(value)
            if within(report, args.errors_root) and report.is_file():
                report.unlink()
    connection.execute(
        """
        UPDATE pairs
        SET status='pending', outcome=NULL, last_stage='retry-after-adapter-fix',
            error_file=NULL, error=NULL, updated_at=?
        WHERE outcome='lakeva-adapt-errors'
        """,
        (now(),),
    )
    connection.commit()
    print(
        f"Requeued {len(rows)} earlier Lake environment-adapter failure(s); "
        "they are not Lean compiler errors.",
        flush=True,
    )


def load_manifest(args, connection: sqlite3.Connection) -> list[dict]:
    manifest = args.manifest.resolve()
    collection_root = manifest.parent.resolve()
    filters = {name.lower() for name in (args.repo or [])}
    selected: list[dict] = []
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = row["name"]
            if filters and name.lower() not in filters:
                continue
            local_value = (row.get("local_path") or "").replace("\\", os.sep)
            repo_path = (collection_root / local_value).resolve() if local_value else (args.repos_root / name).resolve()
            if not within(repo_path, args.repos_root.resolve()):
                raise RuntimeError(f"Manifest path escapes repository root: {name}: {repo_path}")
            deleted = connection.execute(
                "SELECT clone_deleted FROM repos WHERE repo=?", (name,)
            ).fetchone()
            clone_deleted = bool(deleted and deleted["clone_deleted"])
            if not clone_deleted and not (repo_path / ".git").is_dir():
                raise FileNotFoundError(f"Clone is missing or invalid: {name}: {repo_path}")
            selected.append({
                "name": name,
                "path": repo_path,
                "clone_deleted": clone_deleted,
                "url": (row.get("url") or "").strip(),
                "clone_mode": (row.get("clone_mode") or "").strip(),
            })
            connection.execute(
                "INSERT INTO repos(repo,path,status,updated_at) VALUES(?,?,'pending',?) "
                "ON CONFLICT(repo) DO UPDATE SET path=excluded.path, updated_at=excluded.updated_at",
                (name, str(repo_path), now()),
            )
            if args.max_repos and len(selected) >= args.max_repos:
                break
    connection.commit()
    if filters:
        found = {item["name"].lower() for item in selected}
        missing = sorted(filters - found)
        if missing:
            raise ValueError(f"Requested repositories were not found in the manifest: {', '.join(missing)}")
    return selected


def migrate_legacy_error_reports(
    args,
    connection: sqlite3.Connection,
    repositories: list[dict],
) -> None:
    """Re-parse old build logs and write only validated, source-enriched errors."""
    repo_paths = {item["name"]: item["path"] for item in repositories}
    legacy_by_pair: dict[str, tuple[Path, list[dict]]] = {}
    for report_path in list(args.errors_root.glob("*.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        legacy_errors = report.get("errors") if isinstance(report, dict) else None
        pair_id = report.get("pair_id") if isinstance(report, dict) else None
        if isinstance(legacy_errors, list) and isinstance(pair_id, str):
            legacy_by_pair[pair_id] = (
                report_path,
                [item for item in legacy_errors if isinstance(item, dict)],
            )

    selected_repos = set(repo_paths)
    placeholders = ",".join("?" for _ in selected_repos)
    if not placeholders:
        return
    candidates = connection.execute(
        f"""
        SELECT p.*
        FROM pairs AS p
        LEFT JOIN error_exports AS e ON e.pair_id=p.pair_id
        WHERE p.repo IN ({placeholders})
          AND (
            p.pair_id IN ({','.join('?' for _ in legacy_by_pair) or "''"})
            OR (
              p.status IN ('completed','build_inconclusive')
              AND (
                p.result_version IS NULL OR p.result_version <> ?
                OR e.pair_id IS NULL OR e.format_version <> ?
              )
            )
          )
        ORDER BY p.repo,p.pair_index
        """,
        (*selected_repos, *legacy_by_pair.keys(), RESULT_FORMAT_VERSION, RESULT_FORMAT_VERSION),
    ).fetchall()
    converted_reports = 0
    written_errors = 0
    for pair in candidates:
        pair_id = pair["pair_id"]
        repo_name = pair["repo"]
        repo_path = repo_paths[repo_name]
        if not pair['active']:
            clear_pair_error_files(args, pair)
            continue
        native_rows = [connection.execute(
            'SELECT * FROM native_checks WHERE repo=? AND commit_hash=?',
            (repo_name, pair[side + '_hash']),
        ).fetchone() for side in ('old', 'new')]
        if not all(native_pass_is_valid(row, pair[side + '_version'])
                   for row, side in zip(native_rows, ('old', 'new'))):
            if pair['status'] == 'build_inconclusive':
                # Target-first runs never validate endpoints of inconclusive
                # adapted builds; there is nothing to export or requeue.
                continue
            clear_pair_error_files(args, pair)
            connection.execute(
                "UPDATE pairs SET status='pending',outcome=NULL,last_stage='native-evidence-recheck',"
                "error_file=NULL,result_version=NULL,updated_at=? WHERE pair_id=?", (now(), pair_id))
            connection.execute('DELETE FROM error_exports WHERE pair_id=?', (pair_id,))
            continue
        legacy = legacy_by_pair.get(pair_id)
        build_log = args.state_root / "runs" / slug(repo_name) / pair_id / "build.log"
        if pair["outcome"] == PREDICTED_TIMEOUT_OUTCOME and not build_log.is_file():
            # The build was never run (see predict.log); there is nothing to re-parse.
            clear_pair_error_files(args, pair)
            connection.execute(
                "UPDATE pairs SET result_version=?,updated_at=? WHERE pair_id=?",
                (RESULT_FORMAT_VERSION, now(), pair_id),
            )
            connection.execute(
                "INSERT INTO error_exports(pair_id,format_version,error_count,updated_at) "
                "VALUES(?,?,0,?) ON CONFLICT(pair_id) DO UPDATE SET "
                "format_version=excluded.format_version,error_count=0,updated_at=excluded.updated_at",
                (pair_id, RESULT_FORMAT_VERSION, now()),
            )
            continue
        if build_log.is_file():
            errors = parse_log(build_log)
            exit_code = pair["build_exit_code"]
            if exit_code is None:
                exit_code = log_exit_code(build_log)
            source_root = log_source_root(
                build_log,
                args.state_root / "worktrees" / slug(repo_name) / pair_id / "target",
            )
            build_infrastructure_failure = infrastructure_failure(build_log)
        elif legacy is not None:
            errors = [
                {**item, "kind": item.get("kind", "lean-diagnostic")}
                for item in legacy[1]
            ]
            exit_code = None
            source_root = args.state_root / "worktrees" / slug(repo_name) / pair_id / "target"
            build_infrastructure_failure = True
        else:
            clear_pair_error_files(args, pair)
            connection.execute(
                "UPDATE pairs SET status='build_inconclusive',outcome='missing-build-log',"
                "last_stage='result-repair',error_file=NULL,error=?,result_version=?,updated_at=? "
                "WHERE pair_id=?",
                (
                    "The adapted build log is missing; no result was exported.",
                    RESULT_FORMAT_VERSION,
                    now(),
                    pair_id,
                ),
            )
            connection.execute(
                "INSERT INTO error_exports(pair_id,format_version,error_count,updated_at) "
                "VALUES(?,?,0,?) ON CONFLICT(pair_id) DO UPDATE SET "
                "format_version=excluded.format_version,error_count=0,updated_at=excluded.updated_at",
                (pair_id, RESULT_FORMAT_VERSION, now()),
            )
            continue

        def source_from_old_commit(relative_path: str) -> str | None:
            normalized = relative_path.replace("\\", "/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            snapshot = build_log.parent / 'diagnostic-sources.json'
            if snapshot.is_file():
                saved = json.loads(snapshot.read_text(encoding='utf8'))
                if normalized in saved:
                    return saved[normalized]
            if not normalized or normalized.startswith(('.lake/', 'lake-packages/')) or normalized == 'lakefile.lean' or ".." in normalized.split("/"):
                return None
            if not repo_path.is_dir():
                return None  # clone deleted after its diagnostics were snapshotted
            result = git(
                repo_path,
                "show",
                f"{pair['old_hash']}:{normalized}",
                check=False,
            )
            return result.stdout if result.returncode == 0 else None

        if build_log.is_file():
            status, outcome = classify_adapted_build(
                exit_code,
                errors,
                build_log,
                infrastructure_failure=build_infrastructure_failure,
                expected_toolchain=pair['new_version'],
                cache_log=build_log.parent / 'cache.log',
            )
        else:
            status, outcome = "build_inconclusive", "legacy-report-without-build-log"
        if status == "completed":
            outputs = write_pair_error_files(
                args,
                pair,
                errors,
                source_root,
                source_fallback=source_from_old_commit,
            )
        else:
            clear_pair_error_files(args, pair)
            outputs = []
        if legacy is not None and legacy[0].is_file():
            legacy[0].unlink()
            converted_reports += 1
        database_error = None if status == "completed" else tail(build_log)
        connection.execute(
            "UPDATE pairs SET status=?,error_file=?,outcome=?,last_stage='result-repair',"
            "error=?,build_exit_code=?,result_version=?,updated_at=? WHERE pair_id=?",
            (
                status,
                str(outputs[0]) if outputs else None,
                outcome,
                database_error,
                exit_code,
                RESULT_FORMAT_VERSION,
                now(),
                pair_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO error_exports(pair_id,format_version,error_count,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(pair_id) DO UPDATE SET
                format_version=excluded.format_version,
                error_count=excluded.error_count,
                updated_at=excluded.updated_at
            """,
            (pair_id, RESULT_FORMAT_VERSION, len(outputs), now()),
        )
        written_errors += len(outputs)
    connection.execute(
        "UPDATE pairs SET outcome='pipeline-error' WHERE status='pipeline_failed'"
    )
    # Commit even when there were no export candidates. The UPDATE above opens
    # a write transaction on the main connection; leaving it open would block
    # every worker connection for the duration of the run.
    connection.commit()
    if candidates:
        print(
            f"Exported {written_errors} source-enriched Lean error file(s) "
            f"from {len(candidates)} stored build result(s)"
            + (f" and removed {converted_reports} legacy report(s)." if converted_reports else "."),
            flush=True,
        )
def discover_repositories(args, connection: sqlite3.Connection, repositories: list[dict]) -> None:
    pairs_root = args.state_root / "pairs"
    total = len(repositories)
    for index, item in enumerate(repositories, 1):
        name = item["name"]
        repo_path = item["path"]
        existing = connection.execute("SELECT status FROM repos WHERE repo=?", (name,)).fetchone()
        if existing and existing["status"] == "discovered" and not args.rediscover:
            continue
        if item.get("clone_deleted"):
            print(f"[discover {index}/{total}] {name}: clone was deleted; keeping saved pairs", flush=True)
            continue
        print(f"[discover {index}/{total}] {name}", flush=True)
        connection.execute(
            "UPDATE repos SET status='discovering', error=NULL, updated_at=? WHERE repo=?",
            (now(), name),
        )
        connection.commit()
        try:
            ensure_full_history(repo_path)
            discovered = discover_pairs(repo_path)
            pair_file = pairs_root / f"{slug(name)}.jsonl"
            write_jsonl_atomic(pair_file, discovered)
            connection.execute("UPDATE pairs SET active=0, updated_at=? WHERE repo=?", (now(), name))
            for pair in discovered:
                pair_id = pair_identifier(name, pair["new_hash"], pair["old_hash"])
                connection.execute(
                    """
                    INSERT INTO pairs(
                        pair_id,repo,pair_index,old_hash,new_hash,old_version,new_version,
                        status,active,updated_at
                    ) VALUES(?,?,?,?,?,?,?,'pending',1,?)
                    ON CONFLICT(pair_id) DO UPDATE SET
                        pair_index=excluded.pair_index,
                        old_version=excluded.old_version,
                        new_version=excluded.new_version,
                        active=1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        pair_id,
                        name,
                        int(pair["pair_index"]),
                        pair["old_hash"],
                        pair["new_hash"],
                        pair["old_version"],
                        pair["new_version"],
                        now(),
                    ),
                )
            connection.execute(
                "UPDATE repos SET status='discovered', pair_count=?, error=NULL, updated_at=? WHERE repo=?",
                (len(discovered), now(), name),
            )
            connection.commit()
        except KeyboardInterrupt:
            connection.execute(
                "UPDATE repos SET status='pending', error='interrupted', updated_at=? WHERE repo=?",
                (now(), name),
            )
            connection.commit()
            raise
        except Exception as exc:
            connection.execute(
                "UPDATE repos SET status='discovery_failed', error=?, updated_at=? WHERE repo=?",
                (str(exc), now(), name),
            )
            connection.commit()
            print(f"  discovery failed: {exc}", file=sys.stderr, flush=True)


def remove_worktree(repo: Path, worktree: Path, worktrees_root: Path) -> None:
    if not within(worktree, worktrees_root):
        raise RuntimeError(f"Refusing to clean worktree outside state root: {worktree}")
    trash = WORKTREE_TRASH
    if trash is not None and worktree.is_dir() and not within(worktree, trash.root):
        # Rename first: `git worktree remove` would otherwise delete every
        # file itself.  With the directory gone it only drops the metadata.
        if trash.discard(worktree, f"{worktree.parent.name}-{worktree.name}"):
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            return
    git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    if worktree.exists():
        for delay in (0, 2, 5):
            if delay:
                time.sleep(delay)
            try:
                # Lake's dependency clones contain read-only Git objects.
                shutil.rmtree(worktree, onerror=_make_writable_and_retry)
                break
            except PermissionError:
                continue


def add_worktree(repo: Path, worktree: Path, commit_hash: str, worktrees_root: Path) -> None:
    remove_worktree(repo, worktree, worktrees_root)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    try:
        git(repo, "worktree", "add", "--detach", str(worktree), commit_hash, timeout=3600)
    except Exception as exc:
        detail = str(exc)
        if network_failure_text(detail) and not invalid_path_text(detail):
            NETWORK_WATCHDOG.failure(detail)
        raise
    NETWORK_WATCHDOG.success()


def cached_native_status(
    connection: sqlite3.Connection,
    repo_name: str,
    commit_hash: str,
    *,
    args=None,
    version: str | None = None,
) -> str | None:
    row = connection.execute(
        "SELECT status FROM native_checks WHERE repo=? AND commit_hash=?",
        (repo_name, commit_hash),
    ).fetchone()
    if row is None and args is not None and version is not None:
        row = reuse_native_check(args, connection, repo_name, commit_hash, version)
    return row["status"] if row else None


def native_log_dir(args, repo_name: str, commit_hash: str) -> Path:
    return args.state_root / "native-checks" / slug(repo_name) / commit_hash[:16]


def native_manifest_snapshot(args, repo_name: str, commit_hash: str) -> Path:
    return native_log_dir(args, repo_name, commit_hash) / LAKE_MANIFEST


def selected_worktrees_root(args) -> Path:
    """Return the short runtime-worktree root (with test/backward fallback)."""
    return getattr(args, "worktrees_root", args.state_root / "worktrees")


def restore_pristine_worktree(
    worktree: Path,
    commit_hash: str,
    manifest_snapshot: Path | None = None,
) -> None:
    """Remove build effects, then restore the manifest from the passing native build."""
    git(worktree, "submodule", "deinit", "--force", "--all", check=False)
    git(worktree, "reset", "--hard", commit_hash, timeout=1800)
    # The second -f allows removal of partial nested Git repositories left by
    # interrupted Lake dependency clones.  This function is only used inside a
    # verified disposable worktree.
    git(worktree, "clean", "-ffdx", timeout=1800)
    if manifest_snapshot and manifest_snapshot.is_file():
        shutil.copy2(manifest_snapshot, worktree / LAKE_MANIFEST)


def restore_pristine_worktree_with_retries(
    worktree: Path,
    commit_hash: str,
    manifest_snapshot: Path | None = None,
    *,
    attempts: int = 3,
) -> None:
    """Retry cleanup because terminated Windows processes release files slowly."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            restore_pristine_worktree(worktree, commit_hash, manifest_snapshot)
            return
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            if STOP_EVENT.wait(min(2**attempt, 5)):
                raise PipelineInterrupted("pipeline interrupted") from exc
    assert last_error is not None
    raise last_error


def update_pair(connection: sqlite3.Connection, pair_id: str, **fields) -> None:
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key}=?" for key in fields)

    def operation() -> None:
        connection.execute(
            f"UPDATE pairs SET {assignments} WHERE pair_id=?",
            (*fields.values(), pair_id),
        )

    commit_database_write(connection, operation)


def native_check(
    args,
    connection: sqlite3.Connection,
    repo_name: str,
    commit_hash: str,
    version: str,
    worktree: Path,
    lake_command: list[str],
) -> str:
    log_dir = native_log_dir(args, repo_name, commit_hash)
    manifest_snapshot = native_manifest_snapshot(args, repo_name, commit_hash)
    cached = connection.execute(
        "SELECT * FROM native_checks WHERE repo=? AND commit_hash=?",
        (repo_name, commit_hash),
    ).fetchone()
    if cached is None:
        cached = reuse_native_check(args, connection, repo_name, commit_hash, version)
    if cached:
        if cached["status"] != "passed":
            return cached["status"]
        valid_cached_pass = native_pass_is_valid(cached, version)
        if valid_cached_pass and manifest_snapshot.is_file():
            return "passed"
        committed_manifest = worktree / LAKE_MANIFEST
        if valid_cached_pass and committed_manifest.is_file():
            log_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(committed_manifest, manifest_snapshot)
            return "passed"
        # Results created before manifest snapshots were introduced need one
        # native rebuild. This recovers an uncommitted manifest generated by Lake.
        print(
            f"  refreshing cached native check to recover {LAKE_MANIFEST}: "
            f"{repo_name}@{commit_hash[:12]}",
            flush=True,
        )

    log_dir.mkdir(parents=True, exist_ok=True)
    archive_attempt_logs(log_dir)
    manifest_snapshot.unlink(missing_ok=True)
    for stable_log_name in ("cache.log", "build.log", "submodules.log"):
        (log_dir / stable_log_name).unlink(missing_ok=True)
    attempts = max(1, int(getattr(args, "native_attempts", 2)))
    cache_rc = -1
    build_rc = -1
    status = "inconclusive"
    predicted = False
    final_cache_log: Path | None = None
    final_build_log: Path | None = None
    final_extra_logs: tuple[Path, ...] = ()
    mirror_root = dependency_mirror_root(args)

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            if STOP_EVENT.wait(min(2 ** (attempt - 2), 4)):
                raise PipelineInterrupted("pipeline interrupted")
            try:
                restore_pristine_worktree_with_retries(worktree, commit_hash)
            except PipelineInterrupted:
                raise
            except Exception as exc:
                cleanup_log = log_dir / f"cleanup-attempt-{attempt:02d}.log"
                cleanup_log.write_text(
                    f"Native retry could not clean the disposable worktree:\n{exc}\n",
                    encoding="utf-8",
                )
                status = "inconclusive"
                cache_rc = -1
                build_rc = 124
                final_extra_logs = (cleanup_log,)
                break

        attempt_tag = f"attempt-{attempt:02d}"
        submodule_log = log_dir / f"submodules-{attempt_tag}.log"
        cache_log = log_dir / f"cache-{attempt_tag}.log"
        build_log = log_dir / f"build-{attempt_tag}.log"
        extra_logs: list[Path] = []

        submodule_rc = 0
        if (worktree / ".gitmodules").is_file():
            submodule_rc = run_logged(
                [
                    "git",
                    "-c",
                    "core.longpaths=true",
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                ],
                worktree,
                submodule_log,
                args.cache_timeout,
            )
            extra_logs.append(submodule_log)

        cache_rc = -1
        if submodule_rc == 0:
            if mirror_root is not None:
                seed_dependency_clones(worktree, log_dir / f"seed-{attempt_tag}.log", mirror_root,
                                       timeout=args.cache_timeout)
            cache_rc = run_cache_get(args, lake_command, worktree, cache_log, version)

        if submodule_rc == 0:
            predict_log = log_dir / f"predict-{attempt_tag}.log"
            if proofwidgets_cascade_predicted(args, lake_command, worktree, version, predict_log, cache_rc):
                predicted = True
                build_rc = -1
                status = "inconclusive"
                final_cache_log = cache_log if cache_log.is_file() else None
                final_build_log = None
                final_extra_logs = (*extra_logs, predict_log)
                break
            # Very old Lake releases have no verbose flag at all, while newer
            # ones accept it only as a global option.  Plain `lake build` works
            # across the historical toolchains and still prints diagnostics.
            build_rc = run_logged(
                lake_command_for(lake_command, worktree, ["build"]),
                worktree,
                build_log,
                args.native_timeout,
                toolchain=version,
            )

            # Some historical commits intentionally omit or commit a stale
            # manifest.  Lake explicitly asks for an update in that case; a
            # fresh clone must honor it before deciding whether source builds.
            if build_rc != 0 and log_requests_lake_update(build_log):
                update_log = log_dir / f"update-{attempt_tag}.log"
                update_rc = run_logged(
                    lake_command_for(lake_command, worktree, ["update"]),
                    worktree,
                    update_log,
                    args.cache_timeout,
                    toolchain=version,
                )
                extra_logs.append(update_log)
                if update_rc == 0:
                    cache_log = log_dir / f"cache-after-update-{attempt_tag}.log"
                    cache_rc = run_cache_get(args, lake_command, worktree, cache_log, version)
                    build_log = log_dir / f"build-after-update-{attempt_tag}.log"
                    build_rc = run_logged(
                        lake_command_for(lake_command, worktree, ["build"]),
                        worktree,
                        build_log,
                        args.native_timeout,
                        toolchain=version,
                    )
        else:
            build_rc = submodule_rc

        final_cache_log = cache_log if cache_log.is_file() else None
        final_build_log = build_log if build_log.is_file() else None
        final_extra_logs = tuple(extra_logs)
        invalid_native_evidence = (
            empty_build(build_log)
            or committed_toolchain(worktree) != version
            or toolchain_mismatch(build_log, version)
            or toolchain_mismatch(cache_log, version)
            or any(toolchain_mismatch(path, version) for path in extra_logs)
        )
        if build_rc == 0 and not invalid_native_evidence and (
            has_build_evidence(build_log)
            and has_toolchain_evidence(build_log, version)
            and not parse_log(build_log)
        ):
            status = "passed"
            break

        if invalid_native_evidence or build_rc == 0:
            status = "inconclusive"
            break

        if native_failure_is_inconclusive(
            cache_log,
            build_log,
            cache_rc,
            build_rc,
            tuple(extra_logs),
        ):
            status = "inconclusive"
            # A build that hit the time limit will hit it again; retrying only
            # doubles the most expensive failure mode.
            if attempt < attempts and build_rc != TIMEOUT_EXIT_CODE:
                print(
                    f"  retrying inconclusive native check "
                    f"({attempt}/{attempts}): {repo_name}@{commit_hash[:12]}",
                    flush=True,
                )
                continue
        else:
            status = "failed"
        break

    if status == "inconclusive":
        # Optional release downloads may fail without consequence, and a build
        # that timed out while compiling will time out again: only a network
        # failure in required setup output (or in a build that did not time
        # out) makes this a network result.
        setup_logs = [final_cache_log, *final_extra_logs]
        if build_rc != TIMEOUT_EXIT_CODE:
            setup_logs.append(final_build_log)
        evidence = "\n".join(
            required_build_output(path.read_text(encoding="utf-8", errors="replace"))
            for path in setup_logs
            if path is not None and path.is_file()
        )
        if network_failure_text(evidence):
            # Not a result about this commit at all: nothing is cached, so
            # the next run repeats the check instead of pausing the pair.
            NETWORK_WATCHDOG.failure(evidence)
            return NETWORK_FAILURE_OUTCOME

    # Keep stable filenames for inspection and for migration from this format.
    # The final attempt is moved, not copied: earlier attempts keep their
    # tagged names as retry evidence, without storing every log twice.
    if final_cache_log and final_cache_log.is_file():
        os.replace(final_cache_log, log_dir / "cache.log")
    if final_build_log and final_build_log.is_file():
        os.replace(final_build_log, log_dir / "build.log")
    for extra_log in final_extra_logs:
        if extra_log.is_file() and extra_log.name.startswith("submodules-"):
            os.replace(extra_log, log_dir / "submodules.log")
        elif extra_log.is_file() and extra_log.name.startswith("predict-"):
            os.replace(extra_log, log_dir / "predict.log")

    generated_manifest = worktree / LAKE_MANIFEST
    if status == "passed" and generated_manifest.is_file():
        shutil.copy2(generated_manifest, manifest_snapshot)

    def save_native_check() -> None:
        connection.execute(
            """
            INSERT INTO native_checks(
                repo,commit_hash,version,status,cache_exit_code,build_exit_code,log_dir,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(repo,commit_hash) DO UPDATE SET
                version=excluded.version,
                status=excluded.status,
                cache_exit_code=excluded.cache_exit_code,
                build_exit_code=excluded.build_exit_code,
                log_dir=excluded.log_dir,
                updated_at=excluded.updated_at
            """,
            (repo_name, commit_hash, version, status, cache_rc, build_rc, str(log_dir), now()),
        )

    commit_database_write(connection, save_native_check)
    return PREDICTED_TIMEOUT_OUTCOME if predicted else status


def error_file_for(args, pair: sqlite3.Row, error_index: int | None = None) -> Path:
    stem = (
        f"{slug(pair['repo'])}__pair-{int(pair['pair_index']):04d}__"
        f"{version_slug(pair['old_version'])}__to__{version_slug(pair['new_version'])}__"
        f"{pair['pair_id']}"
    )
    if error_index is not None:
        stem += f"__error-{error_index:04d}"
    return args.errors_root / f"{stem}.json"


def clear_pair_error_files(args, pair: sqlite3.Row) -> None:
    legacy = error_file_for(args, pair)
    if legacy.is_file():
        legacy.unlink()
    pattern = f"*__{pair['pair_id']}__error-*.json"
    for output in args.errors_root.glob(pattern):
        if output.is_file():
            output.unlink()


def write_pair_error_files(
    args,
    pair: sqlite3.Row,
    errors: list[dict],
    source_root: Path,
    source_fallback: Callable[[str], str | None] | None = None,
) -> list[Path]:
    clear_pair_error_files(args, pair)
    lean_errors = [error for error in errors if error.get("kind") == "lean-diagnostic"]
    outputs: list[Path] = []
    for index, error in enumerate(lean_errors, 1):
        report = enrich_lean_error(
            error,
            source_root,
            source_fallback=source_fallback,
        )
        report.update(
            {
                "repo": pair["repo"],
                "old_version": pair["old_version"],
                "new_version": pair["new_version"],
                "old_hash": pair["old_hash"],
                "new_hash": pair["new_hash"],
                "pair_index": pair["pair_index"],
                "pair_id": pair["pair_id"],
            }
        )
        output = error_file_for(args, pair, index)
        write_report_atomic(output, report)
        outputs.append(output)
    return outputs


def record_error_export(
    connection: sqlite3.Connection,
    pair_id: str,
    error_count: int,
) -> None:
    def operation() -> None:
        connection.execute(
            """
            INSERT INTO error_exports(pair_id,format_version,error_count,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(pair_id) DO UPDATE SET
                format_version=excluded.format_version,
                error_count=excluded.error_count,
                updated_at=excluded.updated_at
            """,
            (pair_id, RESULT_FORMAT_VERSION, error_count, now()),
        )

    commit_database_write(connection, operation)


class AdaptedResult(NamedTuple):
    status: str
    outcome: str
    errors: list[dict]
    build_rc: int
    cache_rc: int


def saved_source_reader(run_dir: Path) -> Callable[[str], str | None]:
    """Read diagnostic sources snapshotted before the target worktree was removed."""
    snapshot = run_dir / "diagnostic-sources.json"
    saved = json.loads(snapshot.read_text(encoding="utf8")) if snapshot.is_file() else {}

    def read(relative_path: str) -> str | None:
        normalized = relative_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return saved.get(normalized)

    return read


def record_native_rejection(connection, pair, side: str, status: str) -> None:
    if status == PREDICTED_TIMEOUT_OUTCOME:
        update_pair(
            connection,
            pair["pair_id"],
            status="native_inconclusive",
            outcome=PREDICTED_TIMEOUT_OUTCOME,
            last_stage="native-check-inconclusive",
            error_file=None,
            error=(
                f"The {side} native build was skipped: ProofWidgets is out of date under "
                "Lake < v4.16, which rebuilds all of Mathlib from source and exceeds the "
                "time limit (see predict.log). Retry with --retry-native-failures."
            ),
        )
        return
    failed = status == "failed"
    update_pair(
        connection,
        pair["pair_id"],
        status="discarded_native" if failed else "native_inconclusive",
        outcome=(
            f"{side}-commit-does-not-build-natively"
            if failed
            else f"{side}-native-check-inconclusive"
        ),
        last_stage="discarded" if failed else "native-check-inconclusive",
        error_file=None,
    )


def validate_native_endpoints(
    args, connection, pair, repo_path, old_tree, new_tree, worktrees_root, lake_command
) -> bool:
    """Run or reuse both native checks; record the rejection and return False on failure."""
    for side, tree in (("old", old_tree), ("new", new_tree)):
        # The second checkout is delayed until the first endpoint is known-good.
        if tree.exists():
            restore_pristine_worktree_with_retries(tree, pair[side + "_hash"])
        else:
            add_worktree(repo_path, tree, pair[side + "_hash"], worktrees_root)
        update_pair(connection, pair["pair_id"], last_stage=f"native-check-{side}")
        status = native_check(
            args,
            connection,
            pair["repo"],
            pair[side + "_hash"],
            pair[side + "_version"],
            tree,
            lake_command,
        )
        if status == NETWORK_FAILURE_OUTCOME:
            clear_pair_error_files(args, pair)
            update_pair(
                connection,
                pair["pair_id"],
                status="pending",
                outcome=NETWORK_FAILURE_OUTCOME,
                last_stage="network-failure",
                error_file=None,
                error=f"The {side} native check could not reach the network; it was not recorded.",
            )
            return False
        if status != "passed":
            clear_pair_error_files(args, pair)
            record_native_rejection(connection, pair, side, status)
            return False
    for side, tree in (("old", old_tree), ("new", new_tree)):
        if not ensure_native_manifest(args, pair["repo"], pair[side + "_hash"],
                                      pair[side + "_version"], tree, lake_command):
            update_pair(connection, pair["pair_id"], status="adapt_failed",
                        outcome="missing-native-manifest", last_stage="manifest-recovery",
                        error_file=None,
                        error=f"Could not recover {side} native manifest; see manifest-recovery.log")
            return False
    return True


def prepare_adaptation_sources(
    args, pair, repo_path, old_tree, new_tree, worktrees_root
) -> dict[str, bytes] | None:
    """Pristine old/new checkouts carrying the manifest each native build used.

    A passing native build's manifest snapshot wins; otherwise the committed
    manifest is used.  Returns the manifests, or None when either is missing
    (lakeva cannot plan without both; a native build will generate them).
    """
    manifests: dict[str, bytes] = {}
    for side, tree in (("old", old_tree), ("new", new_tree)):
        commit_hash = pair[side + "_hash"]
        snapshot = native_manifest_snapshot(args, pair["repo"], commit_hash)
        if tree.exists():
            restore_pristine_worktree_with_retries(tree, commit_hash, snapshot)
        else:
            add_worktree(repo_path, tree, commit_hash, worktrees_root)
            if snapshot.is_file():
                shutil.copy2(snapshot, tree / LAKE_MANIFEST)
        manifest = tree / LAKE_MANIFEST
        if not manifest.is_file():
            return None
        manifests[side] = manifest.read_bytes()
    return manifests


def run_adapted_build(
    args, connection, pair, repo_path, old_tree, new_tree, target_tree, run_dir,
    worktrees_root, lakeva_command, lake_command,
) -> AdaptedResult | None:
    """Adapt the old commit to the new environment and build it.

    Returns None after recording an adaptation/setup failure.
    """
    pair_id = pair["pair_id"]
    add_worktree(repo_path, target_tree, pair["old_hash"], worktrees_root)
    if (old_tree / LAKE_MANIFEST).is_file():
        shutil.copy2(old_tree / LAKE_MANIFEST, target_tree / LAKE_MANIFEST)
    mirror_root = dependency_mirror_root(args)
    if mirror_root is not None:
        # lakeva's `lake update <pkg>` reuses these package directories and
        # fetches only the delta to the new revisions.
        seed_dependency_clones(target_tree, run_dir / "seed.log", mirror_root, timeout=args.cache_timeout)
    update_pair(connection, pair_id, last_stage="lakeva-adapt")
    adapt_log = run_dir / "lakeva-adapt.log"
    adapt_rc = run_logged(
        [*lakeva_command, "adapt", str(old_tree), str(new_tree), str(target_tree)],
        HERE,
        adapt_log,
        args.adapt_timeout,
    )
    if adapt_rc != 0:
        clear_pair_error_files(args, pair)
        adapt_text = adapt_log.read_text(encoding="utf-8", errors="replace") if adapt_log.is_file() else ""
        if network_failure_text(adapt_text):
            # The adapter could not reach a dependency's remote; that is not
            # an adaptation result.  Keep the pair for the next run.
            update_pair(
                connection,
                pair_id,
                status="pending",
                outcome=NETWORK_FAILURE_OUTCOME,
                last_stage="network-failure",
                error_file=None,
                error=tail(adapt_log),
            )
            NETWORK_WATCHDOG.failure(adapt_text)
            return None
        update_pair(
            connection,
            pair_id,
            status="adapt_failed",
            outcome="lakeva-adapt-failed",
            last_stage="lakeva-adapt",
            error_file=None,
            error=tail(adapt_log),
        )
        return None
    NETWORK_WATCHDOG.success()

    if committed_toolchain(target_tree) != pair['new_version']:
        raise RuntimeError('lakeva returned success without setting the requested target toolchain')

    if (target_tree / ".gitmodules").is_file():
        update_pair(connection, pair_id, last_stage="adapted-submodules")
        submodule_log = run_dir / "submodules.log"
        submodule_rc = run_logged(
            [
                "git",
                "-c",
                "core.longpaths=true",
                "submodule",
                "update",
                "--init",
                "--recursive",
            ],
            target_tree,
            submodule_log,
            args.cache_timeout,
        )
        if submodule_rc != 0:
            clear_pair_error_files(args, pair)
            update_pair(
                connection,
                pair_id,
                status="adapt_failed",
                outcome="adapted-submodule-setup-failed",
                last_stage="adapted-submodules",
                error_file=None,
                error=tail(submodule_log),
            )
            return None

    update_pair(connection, pair_id, last_stage="adapted-cache-get")
    cache_rc = run_cache_get(args, lake_command, target_tree, run_dir / "cache.log", pair['new_version'])

    if proofwidgets_cascade_predicted(
        args, lake_command, target_tree, pair['new_version'], run_dir / "predict.log", cache_rc
    ):
        for name in ('lean-toolchain', LAKE_MANIFEST, '.lakeva-plan.json'):
            if (target_tree / name).is_file():
                shutil.copy2(target_tree / name, run_dir / name)
        return AdaptedResult("build_inconclusive", PREDICTED_TIMEOUT_OUTCOME, [], -1, cache_rc)

    update_pair(connection, pair_id, last_stage="adapted-build")
    build_log = run_dir / "build.log"
    build_rc = run_logged(
        lake_command_for(lake_command, target_tree, ["build"]),
        target_tree,
        build_log,
        args.build_timeout,
        toolchain=pair['new_version'],
    )
    errors = parse_log(build_log)
    # Preserve the actual adapted source before disposable worktrees are
    # removed, so a later parser repair cannot attach text from a different
    # revision or lose dependency/generated-file context.
    source_snapshot = {}
    for diagnostic in errors:
        if diagnostic.get('kind') != 'lean-diagnostic':
            continue
        name, path = _relative_source_path(diagnostic['file'], target_tree)
        if path is not None and path.is_file():
            source_snapshot[name] = path.read_text(encoding='utf8', errors='replace')
    write_report_atomic(run_dir / 'diagnostic-sources.json', source_snapshot)
    for name in ('lean-toolchain', LAKE_MANIFEST, '.lakeva-plan.json'):
        if (target_tree / name).is_file():
            shutil.copy2(target_tree / name, run_dir / name)
    status, outcome = classify_adapted_build(
        build_rc,
        errors,
        build_log,
        infrastructure_failure=infrastructure_failure(build_log),
        expected_toolchain=pair['new_version'],
        cache_log=run_dir / 'cache.log',
    )
    return AdaptedResult(status, outcome, errors, build_rc, cache_rc)


def finish_adapted_pair(
    args, connection, pair, result: AdaptedResult, target_tree: Path, run_dir: Path,
    *, natives_validated: bool,
) -> None:
    status = result.status
    if status == "completed" and not natives_validated:
        # A clean adapted build whose endpoints were not built natively is not
        # evidence for the no-error dataset; --validate-clean-pairs finishes it.
        status = "unvalidated"
    if status == "completed":
        outputs = write_pair_error_files(
            args, pair, result.errors, target_tree,
            source_fallback=saved_source_reader(run_dir),
        )
    else:
        clear_pair_error_files(args, pair)
        outputs = []
    record_error_export(connection, pair["pair_id"], len(outputs))
    detail_log = "predict.log" if result.outcome == PREDICTED_TIMEOUT_OUTCOME else "build.log"
    update_pair(
        connection,
        pair["pair_id"],
        status=status,
        outcome=result.outcome,
        last_stage="parsed",
        error_file=str(outputs[0]) if outputs else None,
        error=tail(run_dir / detail_log) if status == "build_inconclusive" else None,
        build_exit_code=result.build_rc,
        cache_exit_code=result.cache_rc,
        result_version=RESULT_FORMAT_VERSION,
    )


def repair_diff_path(args, pair) -> Path | None:
    root = getattr(args, "repair_diffs_root", None)
    if root is None:
        return None
    return root / f"{slug(pair['repo'])}__{pair['pair_id']}.diff.gz"


def save_repair_diff(args, pair, repo_path: Path) -> bool:
    """Keep the old->new commit diff: the candidate repair for a broken pair.

    The error reports hold only the broken code; this diff is what the repair
    corpus is mined from, and it must survive deletion of the clone.
    """
    output = repair_diff_path(args, pair)
    if output is None or output.is_file():
        return True
    if not (repo_path / ".git").exists():
        return False
    result = git(
        repo_path,
        "diff", "--no-renames", "--no-color", "--no-ext-diff",
        pair["old_hash"], pair["new_hash"],
        "--", ".", ":(exclude).lake", ":(exclude)lake-packages",
        check=False,
    )
    if result.returncode != 0:
        print(f"  could not save repair diff for {pair['repo']} {pair['pair_id']}: "
              f"{result.stderr.strip()[:300]}", file=sys.stderr, flush=True)
        return False
    header = (
        f"# repo: {pair['repo']}\n# pair_id: {pair['pair_id']}\n"
        f"# old: {pair['old_hash']} {pair['old_version']}\n"
        f"# new: {pair['new_hash']} {pair['new_version']}\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(header + result.stdout)
    temporary.replace(output)
    return True


def error_pairs(connection, repo: str | None = None) -> list[sqlite3.Row]:
    query = ("SELECT * FROM pairs WHERE active=1 AND status='completed' "
             "AND outcome='build-errors'")
    if repo is None:
        return connection.execute(query).fetchall()
    return connection.execute(query + " AND repo=?", (repo,)).fetchall()


def repository_finished(connection, repo: str) -> bool:
    """No pair of this repository may still need its clone.

    Pending and running pairs are obvious; inconclusive native/adapted checks
    and orchestration failures are retried with the ``--retry-*`` flags, which
    is impossible once the clone is gone.
    """
    placeholders = ",".join("?" for _ in UNFINISHED_PAIR_STATUSES)
    return connection.execute(
        f"SELECT 1 FROM pairs WHERE repo=? AND active=1 AND status IN ({placeholders}) LIMIT 1",
        (repo, *UNFINISHED_PAIR_STATUSES),
    ).fetchone() is None


def _make_writable_and_retry(function, path, _info) -> None:
    os.chmod(path, 0o700)  # Git marks pack files read-only on Windows
    function(path)


def delete_finished_clone(args, connection, repo: str, repo_path: Path) -> bool:
    """Delete a finished repository's clone unless it produced Lean errors.

    Clones with error pairs are kept: minimizing their repairs later means
    rebuilding the old commit with selected diff hunks applied.
    """
    if not repo_path.exists() or not repository_finished(connection, repo):
        return False
    if error_pairs(connection, repo):
        return False
    if not within(repo_path, args.repos_root) or repo_path.resolve() == args.repos_root.resolve():
        raise RuntimeError(f"Refusing to delete a clone outside the repository root: {repo_path}")
    shutil.rmtree(repo_path, onerror=_make_writable_and_retry)

    def mark() -> None:
        connection.execute(
            "UPDATE repos SET clone_deleted=1, updated_at=? WHERE repo=?", (now(), repo)
        )

    commit_database_write(connection, mark)
    return True


def preserve_and_prune_clones(args, connection, repositories: list[dict]) -> None:
    """Save missing repair diffs; with --delete-finished-repos, drop finished clones."""
    paths = {item["name"]: item["path"] for item in repositories if not item.get("clone_deleted")}
    saved = 0
    for pair in error_pairs(connection):
        output = repair_diff_path(args, pair)
        if output is not None and pair["repo"] in paths and not output.is_file():
            saved += save_repair_diff(args, pair, paths[pair["repo"]])
    if saved:
        print(f"Saved {saved} repair diff(s) to {args.repair_diffs_root}", flush=True)
    if not getattr(args, "delete_finished_repos", False):
        return
    deleted = 0
    for repo, path in paths.items():
        if delete_finished_clone(args, connection, repo, path):
            deleted += 1
            for item in repositories:
                if item["name"] == repo:
                    item["clone_deleted"] = True
    if deleted:
        print(f"Deleted {deleted} finished repository clone(s).", flush=True)


def clone_command(url: str, path: Path, clone_mode: str, *, no_checkout: bool = False) -> list[str]:
    """Reproduce the collection's clone shape (blobless / no-checkout) for one repo.

    Shallow clones are not reproduced: discovery deepens them anyway.  The
    pipeline only reads Git objects and creates its own worktrees, so a clone
    may skip the default-branch checkout (``no_checkout``) when that checkout
    contains a path Git cannot create on this platform.
    """
    command = ["git", "-c", "core.longpaths=true", "clone", "--quiet"]
    mode = (clone_mode or "").casefold()
    if "blobless" in mode:
        command.append("--filter=blob:none")
    if no_checkout or "no-checkout" in mode:
        command.append("--no-checkout")
    return [*command, url, str(path)]


def restore_deleted_clones(args, connection, repositories: list[dict]) -> None:
    """Re-clone deleted repositories whose pairs still need the clone.

    A clone is restored only when the repository is unfinished (see
    ``repository_finished``); finished repositories stay deleted.  The
    ``clone_deleted`` flag is reset per repository as soon as its clone exists.
    """
    candidates = [
        item for item in repositories
        if item.get("clone_deleted") and not repository_finished(connection, item["name"])
    ]
    if not candidates:
        print("No deleted clones need restoring.", flush=True)
        return
    missing_url = [item["name"] for item in candidates if not item.get("url")]
    if missing_url:
        raise RuntimeError(
            "The manifest has no clone URL for: " + ", ".join(missing_url[:5])
            + (" ..." if len(missing_url) > 5 else "")
        )
    print(f"Restoring {len(candidates)} deleted repository clone(s)...", flush=True)
    environment = child_process_environment()
    timeout = max(600, int(getattr(args, "cache_timeout", 1800)))

    def restore(item: dict) -> tuple[dict, str | None]:
        path: Path = item["path"]
        if (path / ".git").is_dir():
            return item, None
        path.parent.mkdir(parents=True, exist_ok=True)
        no_checkout = False
        while True:
            if path.exists():
                shutil.rmtree(path, onerror=_make_writable_and_retry)
            try:
                result = subprocess.run(
                    clone_command(item["url"], path, item.get("clone_mode", ""), no_checkout=no_checkout),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                shutil.rmtree(path, ignore_errors=True)
                return item, f"clone timed out after {timeout} seconds"
            if result.returncode == 0:
                return item, None
            detail = (result.stderr or result.stdout).strip()[-500:]
            if not no_checkout and invalid_path_text(detail):
                no_checkout = True  # the objects are fine; only the checkout is not
                continue
            shutil.rmtree(path, ignore_errors=True)
            return item, detail

    restored = 0
    failures: list[tuple[str, str]] = []
    workers = max(1, min(8, 4 * int(getattr(args, "workers", 2))))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lean-pipeline-clone")
    try:
        futures = [executor.submit(restore, item) for item in candidates]
        for index, future in enumerate(as_completed(futures), 1):
            item, error = future.result()
            if error is None:
                connection.execute(
                    "UPDATE repos SET clone_deleted=0, updated_at=? WHERE repo=?",
                    (now(), item["name"]),
                )
                connection.commit()
                item["clone_deleted"] = False
                restored += 1
                print(f"  [{index}/{len(candidates)}] restored {item['name']}", flush=True)
            else:
                failures.append((item["name"], error))
                print(
                    f"  [{index}/{len(candidates)}] could not restore {item['name']}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
    print(
        f"Restored {restored} clone(s); {len(failures)} could not be restored.",
        flush=True,
    )
    if failures and all(network_failure_text(error) for _, error in failures):
        print(
            "Every failed clone hit a network error; re-run with --restore-deleted-clones "
            "once connectivity is restored.",
            flush=True,
        )


def migrate_network_failures(args, connection: sqlite3.Connection) -> None:
    """Requeue results that only recorded a network outage; file invalid paths.

    Before the network watchdog existed, `git worktree add`, `lakeva adapt`, and
    native checks that failed because the network was down were stored as
    pipeline, adaptation, or inconclusive-native results.  Those records say
    nothing about their pairs, so the pairs go back to ``pending``.  Paths Git
    cannot create on this platform become ``ineligible_pair``.
    """
    row = connection.execute(
        "SELECT value FROM pipeline_meta WHERE key='network-failure-repair'"
    ).fetchone()
    if row and row["value"] == NETWORK_REPAIR_VERSION:
        return
    stamp = now()
    requeued = invalid = natives = 0

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def requeue(pair_id: str, detail: str | None) -> None:
        nonlocal requeued
        connection.execute(
            "UPDATE pairs SET status='pending',outcome=?,last_stage='network-failure',"
            "error_file=NULL,error=?,result_version=NULL,updated_at=? WHERE pair_id=?",
            (NETWORK_FAILURE_OUTCOME, detail, stamp, pair_id),
        )
        connection.execute("DELETE FROM error_exports WHERE pair_id=?", (pair_id,))
        requeued += 1

    for pair in connection.execute(
        "SELECT pair_id,error FROM pairs WHERE active=1 AND status='pipeline_failed'"
    ).fetchall():
        detail = pair["error"] or ""
        if invalid_path_text(detail):
            connection.execute(
                "UPDATE pairs SET status='ineligible_pair',outcome=?,last_stage='materialize-worktrees',"
                "error_file=NULL,updated_at=? WHERE pair_id=?",
                (INVALID_PATH_OUTCOME, stamp, pair["pair_id"]),
            )
            invalid += 1
        elif network_failure_text(detail):
            requeue(pair["pair_id"], detail)

    for pair in connection.execute(
        "SELECT pair_id,repo,error FROM pairs WHERE active=1 AND status='adapt_failed' "
        "AND outcome='lakeva-adapt-failed'"
    ).fetchall():
        log = args.state_root / "runs" / slug(pair["repo"]) / pair["pair_id"] / "lakeva-adapt.log"
        if network_failure_text((pair["error"] or "") + "\n" + read(log)):
            requeue(pair["pair_id"], tail(log) if log.is_file() else pair["error"])

    for pair in connection.execute(
        "SELECT pair_id,repo,build_exit_code FROM pairs WHERE active=1 AND status='build_inconclusive' "
        "AND outcome='adapted-build-infrastructure-failure'"
    ).fetchall():
        run_dir = args.state_root / "runs" / slug(pair["repo"]) / pair["pair_id"]
        names = ["cache.log", "submodules.log"]
        if pair["build_exit_code"] != TIMEOUT_EXIT_CODE:
            names.append("build.log")
        evidence = "\n".join(required_build_output(read(run_dir / name)) for name in names)
        if network_failure_text(evidence):
            requeue(pair["pair_id"], tail(run_dir / "build.log"))

    for check in connection.execute(
        "SELECT repo,commit_hash,log_dir,build_exit_code FROM native_checks WHERE status='inconclusive'"
    ).fetchall():
        log_dir = Path(check["log_dir"])
        logs = sorted(log_dir.glob("*.log"))
        if check["build_exit_code"] == TIMEOUT_EXIT_CODE:
            logs = [path for path in logs if not path.name.startswith("build")]
        evidence = "\n".join(required_build_output(read(path)) for path in logs)
        if not network_failure_text(evidence):
            continue
        connection.execute(
            "DELETE FROM native_checks WHERE repo=? AND commit_hash=?",
            (check["repo"], check["commit_hash"]),
        )
        natives += 1
        for pair in connection.execute(
            "SELECT pair_id FROM pairs WHERE active=1 AND repo=? AND status='native_inconclusive' "
            "AND (old_hash=? OR new_hash=?)",
            (check["repo"], check["commit_hash"], check["commit_hash"]),
        ).fetchall():
            requeue(pair["pair_id"], "A native check was interrupted by a network failure; it was not recorded.")

    connection.execute(
        "INSERT INTO pipeline_meta(key,value,updated_at) VALUES('network-failure-repair',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (NETWORK_REPAIR_VERSION, stamp),
    )
    connection.commit()
    if requeued or invalid or natives:
        print(
            f"Network-failure fix: requeued {requeued} pair(s) whose stored result was only a "
            f"network outage, discarded {natives} network-interrupted native check(s), and "
            f"marked {invalid} pair(s) with paths Git cannot create on this platform.",
            flush=True,
        )


# Present since the first Mathlib 4 commits; a project depending on Mathlib
# has neither at its root.
MATHLIB_MARKERS = ("Mathlib.lean", "Mathlib/Tactic")


def is_mathlib_itself(repo_path: Path, commit_hash: str) -> bool:
    """Whether a commit is (a fork of) Mathlib, from local Git trees only.

    Adapting Mathlib's own source means compiling all of Mathlib without a
    usable cache, which exceeds the build timeout and yields no examples.
    """
    if not repo_path.is_dir():
        return False
    result = git(repo_path, "ls-tree", "--name-only", commit_hash, *MATHLIB_MARKERS, check=False)
    return result.returncode == 0 and set(result.stdout.split()) == set(MATHLIB_MARKERS)


def pair_run_dir(args, repo_name: str, pair_id: str) -> Path:
    return args.state_root / "runs" / slug(repo_name) / pair_id


def copy_log_directory(source: Path, destination: Path) -> list[Path]:
    """Copy the files of one log directory (not its history/ of earlier attempts)."""
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for entry in sorted(source.iterdir()):
        if entry.is_file():
            target = destination / entry.name
            shutil.copy2(entry, target)
            copied.append(target)
    return copied


def write_provenance(directory: Path, **fields) -> None:
    """Record where copied evidence came from, next to the copies."""
    write_report_atomic(directory / "provenance.json", {**fields, "copied_at": now()})


def native_check_reusable(row, version: str) -> bool:
    directory = Path(row["log_dir"])
    if row["status"] == "passed":
        return native_pass_is_valid(row, version) and (directory / LAKE_MANIFEST).is_file()
    if row["status"] == "failed":
        return row["version"] == version and (directory / "build.log").is_file()
    return False


def reuse_native_check(args, connection, repo_name: str, commit_hash: str, version: str):
    """Copy another repository's native result for the same commit and toolchain.

    A commit's native build depends only on the commit and its toolchain, so a
    fork sharing the commit need not rebuild it.  ``passed`` results must
    still satisfy the current evidence rules and carry their manifest
    snapshot; ``failed`` results need their build log.  Inconclusive results
    are never copied, except a build that hit the time limit, which would hit
    it again (``--no-timeout-prediction`` disables that).  Returns the new
    native_checks row of this repository, or None.
    """
    reuse_results = not getattr(args, "no_fork_reuse", False)
    reuse_timeouts = not getattr(args, "no_timeout_prediction", False)
    if not reuse_results and not reuse_timeouts:
        return None
    candidates = connection.execute(
        "SELECT * FROM native_checks WHERE commit_hash=? AND version=? AND repo<>? "
        "ORDER BY updated_at DESC",
        (commit_hash, version, repo_name),
    ).fetchall()
    log_dir = native_log_dir(args, repo_name, commit_hash)
    for source in candidates:
        source_dir = Path(source["log_dir"])
        if not source_dir.is_dir():
            continue
        if source["status"] in ("passed", "failed"):
            if not reuse_results or not native_check_reusable(source, version):
                continue
            archive_attempt_logs(log_dir)
            copy_log_directory(source_dir, log_dir)
            build_rc = source["build_exit_code"]
            note = f"reused {source['status']} native check"
        elif source["status"] == "inconclusive" and source["build_exit_code"] == TIMEOUT_EXIT_CODE:
            if not reuse_timeouts:
                continue
            archive_attempt_logs(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "build.log").write_text(
                f"# skipped: duplicate of {source['repo']}/{commit_hash} timed out\n"
                f"# source_log_dir: {source_dir}\n"
                f"# exit_code: {TIMEOUT_EXIT_CODE}\n",
                encoding="utf-8", newline="\n",
            )
            build_rc = TIMEOUT_EXIT_CODE
            note = "skipped native build: identical commit timed out"
        else:
            continue
        write_provenance(
            log_dir,
            source_repo=source["repo"],
            source_commit=commit_hash,
            source_log_dir=str(source_dir),
            source_status=source["status"],
        )

        def save() -> None:
            connection.execute(
                """
                INSERT INTO native_checks(
                    repo,commit_hash,version,status,cache_exit_code,build_exit_code,log_dir,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(repo,commit_hash) DO UPDATE SET
                    version=excluded.version,
                    status=excluded.status,
                    cache_exit_code=excluded.cache_exit_code,
                    build_exit_code=excluded.build_exit_code,
                    log_dir=excluded.log_dir,
                    updated_at=excluded.updated_at
                """,
                (repo_name, commit_hash, version, source["status"], source["cache_exit_code"],
                 build_rc, str(log_dir), now()),
            )

        commit_database_write(connection, save)
        print(f"  {note}: {source['repo']}@{commit_hash[:12]} -> {repo_name}", flush=True)
        return connection.execute(
            "SELECT * FROM native_checks WHERE repo=? AND commit_hash=?",
            (repo_name, commit_hash),
        ).fetchone()
    return None


REUSABLE_ADAPT_FAILURES = ("lakeva-adapt-failed", "adapted-submodule-setup-failed")


def find_reusable_pair(args, connection, pair):
    """A finished pair of another repository with identical commits and toolchains.

    Candidates are ``completed`` (build-errors or build-succeeded) and
    ``unvalidated`` pairs at the current result version whose run directory
    still holds the build log (and, for build-errors, the diagnostic-source
    snapshot and every exported error file), and adaptation failures whose
    adapter log exists.  Inconclusive, network, orchestration, and
    native-discard results are never reused.
    """
    if getattr(args, "no_fork_reuse", False):
        return None
    rows = connection.execute(
        "SELECT * FROM pairs WHERE active=1 AND repo<>? AND old_hash=? AND new_hash=? "
        "AND old_version=? AND new_version=? AND ("
        "(status IN ('completed','unvalidated') AND outcome IN ('build-errors','build-succeeded') "
        " AND result_version=?) "
        "OR (status='adapt_failed' AND outcome IN (?,?))) ORDER BY updated_at DESC",
        (pair["repo"], pair["old_hash"], pair["new_hash"], pair["old_version"], pair["new_version"],
         RESULT_FORMAT_VERSION, *REUSABLE_ADAPT_FAILURES),
    ).fetchall()
    validate_clean = getattr(args, "validate_clean_pairs", False)
    for row in rows:
        run_dir = pair_run_dir(args, row["repo"], row["pair_id"])
        if row["status"] == "adapt_failed":
            log_name = "lakeva-adapt.log" if row["outcome"] == "lakeva-adapt-failed" else "submodules.log"
            if (run_dir / log_name).is_file():
                return row
            continue
        if row["status"] == "unvalidated" and validate_clean:
            continue  # this run validates clean pairs natively; a copy would not
        if not (run_dir / "build.log").is_file():
            continue
        export = connection.execute(
            "SELECT format_version,error_count FROM error_exports WHERE pair_id=?", (row["pair_id"],)
        ).fetchone()
        if not export or export["format_version"] != RESULT_FORMAT_VERSION:
            continue
        if row["outcome"] == "build-errors":
            if not (run_dir / "diagnostic-sources.json").is_file() or export["error_count"] < 1:
                continue
            exported = list(args.errors_root.glob(f"*__{row['pair_id']}__error-*.json"))
            if len(exported) != export["error_count"]:
                continue
        return row
    return None


def reuse_pair_from_fork(args, connection, pair, source, repo_path: Path, run_dir: Path, target_tree: Path) -> bool:
    """Finish this pair from an identical finished pair of another repository.

    The run directory is copied with provenance; this pair's error files are
    regenerated from the copied log and source snapshot so they carry this
    repository, pair id, and index; the repair diff comes from this
    repository's clone; and both native rows are copied for this repository.
    Returns False (after removing the copies) when the result cannot be
    carried over completely, in which case the pair is processed as usual.
    """
    pair_id = pair["pair_id"]
    source_dir = pair_run_dir(args, source["repo"], source["pair_id"])
    copied = copy_log_directory(source_dir, run_dir)
    write_provenance(run_dir, source_pair_id=source["pair_id"], source_repo=source["repo"],
                     source_run_dir=str(source_dir))

    def undo() -> bool:
        clear_pair_error_files(args, pair)
        for path in (*copied, run_dir / "provenance.json"):
            path.unlink(missing_ok=True)
        return False

    if source["status"] == "adapt_failed":
        clear_pair_error_files(args, pair)
        update_pair(
            connection, pair_id, status="adapt_failed", outcome=source["outcome"],
            last_stage=REUSED_STAGE, error_file=None, error=source["error"],
        )
        print(f"  reused adaptation failure of {source['repo']} {source['pair_id']} for {pair['repo']}", flush=True)
        return True

    if source["status"] == "completed":
        for side in ("old", "new"):
            row = connection.execute(
                "SELECT * FROM native_checks WHERE repo=? AND commit_hash=?",
                (pair["repo"], pair[side + "_hash"]),
            ).fetchone()
            if row is None:
                row = reuse_native_check(args, connection, pair["repo"], pair[side + "_hash"],
                                         pair[side + "_version"])
            if row is None or not native_pass_is_valid(row, pair[side + "_version"]):
                return undo()

    build_log = run_dir / "build.log"
    errors = parse_log(build_log)
    outputs = write_pair_error_files(
        args, pair, errors, log_source_root(build_log, target_tree),
        source_fallback=saved_source_reader(run_dir),
    )
    export = connection.execute(
        "SELECT error_count FROM error_exports WHERE pair_id=?", (source["pair_id"],)
    ).fetchone()
    if export is None or len(outputs) != export["error_count"]:
        return undo()
    if source["outcome"] == "build-errors" and not save_repair_diff(args, pair, repo_path):
        print(f"  repair diff for {pair['repo']} {pair_id} will be retried at the next start",
              file=sys.stderr, flush=True)
    record_error_export(connection, pair_id, len(outputs))
    update_pair(
        connection,
        pair_id,
        status=source["status"],
        outcome=source["outcome"],
        last_stage=REUSED_STAGE,
        error_file=str(outputs[0]) if outputs else None,
        error=None,
        build_exit_code=source["build_exit_code"],
        cache_exit_code=source["cache_exit_code"],
        result_version=RESULT_FORMAT_VERSION,
    )
    print(f"  reused {source['outcome']} result of {source['repo']} {source['pair_id']} "
          f"for {pair['repo']} ({len(outputs)} error file(s))", flush=True)
    return True


def find_duplicate_pair_timeout(args, connection, pair):
    """An identical pair of another repository whose adapted build hit the time limit."""
    if getattr(args, "no_timeout_prediction", False):
        return None
    return connection.execute(
        "SELECT * FROM pairs WHERE active=1 AND repo<>? AND old_hash=? AND new_hash=? "
        "AND old_version=? AND new_version=? AND status='build_inconclusive' "
        "AND outcome='adapted-build-incomplete' AND build_exit_code=? ORDER BY updated_at DESC",
        (pair["repo"], pair["old_hash"], pair["new_hash"], pair["old_version"], pair["new_version"],
         TIMEOUT_EXIT_CODE),
    ).fetchone()


def record_duplicate_pair_timeout(args, connection, pair, source, run_dir: Path) -> None:
    """Record the same inconclusive timeout without running the build again."""
    run_dir.mkdir(parents=True, exist_ok=True)
    build_log = run_dir / "build.log"
    build_log.write_text(
        f"# skipped: duplicate of {source['repo']}/{source['pair_id']} timed out\n"
        f"# source_run_dir: {pair_run_dir(args, source['repo'], source['pair_id'])}\n"
        f"# exit_code: {TIMEOUT_EXIT_CODE}\n",
        encoding="utf-8", newline="\n",
    )
    write_provenance(run_dir, source_pair_id=source["pair_id"], source_repo=source["repo"],
                     source_status=source["status"], source_outcome=source["outcome"])
    clear_pair_error_files(args, pair)
    record_error_export(connection, pair["pair_id"], 0)
    update_pair(
        connection,
        pair["pair_id"],
        status="build_inconclusive",
        outcome=source["outcome"],
        last_stage=DUPLICATE_TIMEOUT_STAGE,
        error_file=None,
        error=tail(build_log),
        build_exit_code=TIMEOUT_EXIT_CODE,
        cache_exit_code=source["cache_exit_code"],
        result_version=RESULT_FORMAT_VERSION,
    )
    print(f"  skipped adapted build: identical pair {source['repo']} {source['pair_id']} timed out", flush=True)


def process_pair(
    args,
    connection: sqlite3.Connection,
    pair: sqlite3.Row,
    repo_path: Path,
    lakeva_command: list[str],
    lake_command: list[str],
) -> None:
    pair_id = pair["pair_id"]
    run_dir = args.state_root / "runs" / slug(pair["repo"]) / pair_id
    worktrees_root = selected_worktrees_root(args)
    worktree_base = worktrees_root / pair_id
    old_tree = worktree_base / "old"
    new_tree = worktree_base / "new"
    target_tree = worktree_base / "target"
    try:
        if pair['old_version'] == pair['new_version'] or not all(
            immutable_toolchain(pair[key]) for key in ('old_version', 'new_version')
        ):
            clear_pair_error_files(args, pair)
            update_pair(connection, pair_id, status='ineligible_pair',
                        outcome='identical-toolchains' if pair['old_version'] == pair['new_version'] else 'moving-toolchain-alias',
                        error_file=None, last_stage='pair-validation')
            return
        if not getattr(args, "include_mathlib", False) and is_mathlib_itself(repo_path, pair["old_hash"]):
            clear_pair_error_files(args, pair)
            update_pair(connection, pair_id, status='ineligible_pair', outcome='mathlib-itself',
                        error_file=None, last_stage='pair-validation')
            return
        update_pair(
            connection,
            pair_id,
            status="running",
            last_stage="materialize-native-worktrees",
            attempts=int(pair["attempts"]) + 1,
            error=None,
            outcome=None,
            error_file=None,
            result_version=None,
            build_exit_code=None,
            cache_exit_code=None,
        )
        clear_pair_error_files(args, pair)
        archive_attempt_logs(run_dir)
        old_cached = cached_native_status(
            connection, pair["repo"], pair["old_hash"], args=args, version=pair["old_version"]
        )
        new_cached = cached_native_status(
            connection, pair["repo"], pair["new_hash"], args=args, version=pair["new_version"]
        )
        if old_cached == "failed" or new_cached == "failed":
            clear_pair_error_files(args, pair)
            old_failed = old_cached == "failed"
            update_pair(
                connection,
                pair_id,
                status="discarded_native",
                outcome=(
                    "old-commit-does-not-build-natively"
                    if old_failed
                    else "new-commit-does-not-build-natively"
                ),
                last_stage="discarded-cached-native-failure",
                error_file=None,
            )
            return
        if old_cached == "inconclusive" or new_cached == "inconclusive":
            clear_pair_error_files(args, pair)
            old_inconclusive = old_cached == "inconclusive"
            update_pair(
                connection,
                pair_id,
                status="native_inconclusive",
                outcome=(
                    "old-native-check-inconclusive"
                    if old_inconclusive
                    else "new-native-check-inconclusive"
                ),
                last_stage="cached-native-check-inconclusive",
                error_file=None,
                error=(
                    "Native validation was interrupted or failed during toolchain, "
                    "network, dependency, path, or machine setup. It was not treated "
                    "as evidence that this commit is broken."
                ),
            )
            return

        # The same commits under the same toolchains give the same result:
        # a fork copies a finished twin instead of building (provenance is
        # recorded), and a twin that timed out is not built again.
        source = find_reusable_pair(args, connection, pair)
        if source is not None:
            update_pair(connection, pair_id, last_stage="reuse-from-fork")
            if reuse_pair_from_fork(args, connection, pair, source, repo_path, run_dir, target_tree):
                return
        duplicate = find_duplicate_pair_timeout(args, connection, pair)
        if duplicate is not None:
            record_duplicate_pair_timeout(args, connection, pair, duplicate, run_dir)
            return

        # One prune per pair is enough; removing each individual worktree does
        # not need to rescan the repository's worktree registry.
        git(repo_path, "worktree", "prune", check=False)

        # Target-first: most pairs never yield a Lean error, and the two native
        # builds are by far the most expensive step.  Build the adapted target
        # first and validate the endpoints only when the result can be used.
        validate_clean = getattr(args, "validate_clean_pairs", False)
        manifests = None
        if not getattr(args, "native_first", False):
            manifests = prepare_adaptation_sources(
                args, pair, repo_path, old_tree, new_tree, worktrees_root
            )
        natives_validated = False
        if manifests is None:
            if not validate_native_endpoints(
                args, connection, pair, repo_path, old_tree, new_tree,
                worktrees_root, lake_command,
            ):
                return
            natives_validated = True
            manifests = prepare_adaptation_sources(
                args, pair, repo_path, old_tree, new_tree, worktrees_root
            )
            if manifests is None:
                raise RuntimeError("native manifests disappeared before adaptation")

        def adapted_build() -> AdaptedResult | None:
            return run_adapted_build(
                args, connection, pair, repo_path, old_tree, new_tree, target_tree,
                run_dir, worktrees_root, lakeva_command, lake_command,
            )

        result = adapted_build()
        if result is None:
            return

        if not natives_validated and result.status == "completed" and (
            result.outcome == "build-errors" or validate_clean
        ):
            # Release the built target (often several GB) before native builds.
            if not args.keep_worktrees:
                remove_worktree(repo_path, target_tree, worktrees_root)
            if not validate_native_endpoints(
                args, connection, pair, repo_path, old_tree, new_tree,
                worktrees_root, lake_command,
            ):
                return
            natives_validated = True
            native_manifests = prepare_adaptation_sources(
                args, pair, repo_path, old_tree, new_tree, worktrees_root
            )
            if native_manifests != manifests:
                # Lake had to refresh a stale committed manifest natively.
                # Repeat the experiment in exactly the native environment.
                print(f"  native manifest differs; re-running adapted build: {pair['repo']}", flush=True)
                archive_attempt_logs(run_dir)
                result = adapted_build()
                if result is None:
                    return

        finish_adapted_pair(
            args, connection, pair, result, target_tree, run_dir,
            natives_validated=natives_validated,
        )
    except (KeyboardInterrupt, PipelineInterrupted):
        update_pair(connection, pair_id, status="pending", last_stage="interrupted", error="interrupted")
        raise
    except Exception as exc:
        clear_pair_error_files(args, pair)
        detail = str(exc)
        if invalid_path_text(detail):
            # Git cannot materialize this commit on this platform; a retry
            # here would fail identically.
            update_pair(
                connection,
                pair_id,
                status="ineligible_pair",
                outcome=INVALID_PATH_OUTCOME,
                last_stage="materialize-worktrees",
                error_file=None,
                error=detail,
            )
        elif network_failure_text(detail):
            # Nothing was learned about the pair.  It is retried by the next
            # run, and the watchdog pauses this run if the outage persists.
            update_pair(
                connection,
                pair_id,
                status="pending",
                outcome=NETWORK_FAILURE_OUTCOME,
                last_stage="network-failure",
                error_file=None,
                error=detail,
            )
        else:
            update_pair(
                connection,
                pair_id,
                status="pipeline_failed",
                outcome="pipeline-error",
                last_stage="orchestrator-error",
                error_file=None,
                error=detail,
            )
    finally:
        if hasattr(args, 'no_lean_errors_output'):
            export_pairs_without_lean_errors(connection, args.no_lean_errors_output)
        if not args.keep_worktrees:
            for worktree in (target_tree, new_tree, old_tree):
                try:
                    remove_worktree(repo_path, worktree, worktrees_root)
                except Exception as cleanup_error:
                    print(f"  cleanup warning for {worktree}: {cleanup_error}", file=sys.stderr)
            try:
                worktree_base.rmdir()
            except OSError:
                pass
            git(repo_path, "worktree", "prune", check=False)


def show_status(connection: sqlite3.Connection, *, mirror_root: Path | None = None) -> None:
    print("Repositories:")
    for row in connection.execute("SELECT status, COUNT(*) AS count FROM repos GROUP BY status ORDER BY status"):
        print(f"  {row['status']}: {row['count']}")
    print("Pairs:")
    for row in connection.execute(
        "SELECT status, outcome, COUNT(*) AS count FROM pairs WHERE active=1 "
        "GROUP BY status,outcome ORDER BY status,outcome"
    ):
        detail = f" / {row['outcome']}" if row["outcome"] else ""
        print(f"  {row['status']}{detail}: {row['count']}")
    native = connection.execute(
        "SELECT status, COUNT(*) AS count FROM native_checks GROUP BY status ORDER BY status"
    ).fetchall()
    print("Native commit checks:")
    if not native:
        print("  none")
    for row in native:
        print(f"  {row['status']}: {row['count']}")
    reused = connection.execute(
        "SELECT COUNT(*) FROM pairs WHERE active=1 AND last_stage=?", (REUSED_STAGE,)
    ).fetchone()[0]
    skipped = connection.execute(
        "SELECT COUNT(*) FROM pairs WHERE active=1 AND (last_stage=? OR outcome=?)",
        (DUPLICATE_TIMEOUT_STAGE, PREDICTED_TIMEOUT_OUTCOME),
    ).fetchone()[0]
    print(f"Pairs reused from forks: {reused}; builds skipped as certain timeouts: {skipped}")
    if mirror_root is not None:
        count, size = dependency_mirror_summary(mirror_root)
        print(f"Dependency mirrors: {count} ({size / 2**30:.1f} GiB) in {mirror_root}")


def export_pairs_without_lean_errors(
    connection: sqlite3.Connection,
    output: Path,
) -> int:
    """Serialize snapshot + replacement so concurrent exports cannot regress."""
    with EXPORT_LOCK:
        return _export_pairs_without_lean_errors(connection, output)


def _export_pairs_without_lean_errors(connection, output) -> int:
    """Only validated, successful builds with two native passes are eligible."""
    rows = connection.execute(
        """
        SELECT repo,pair_index,old_hash,new_hash,old_version,new_version,pair_id,outcome
        FROM pairs
        WHERE active=1
          AND status='completed'
          AND outcome='build-succeeded'
          AND result_version=? AND build_exit_code=0
          AND old_version <> new_version
          AND EXISTS (SELECT 1 FROM native_checks n WHERE n.repo=pairs.repo
            AND n.commit_hash=pairs.old_hash AND n.version=pairs.old_version AND n.status='passed')
          AND EXISTS (SELECT 1 FROM native_checks n WHERE n.repo=pairs.repo
            AND n.commit_hash=pairs.new_hash AND n.version=pairs.new_version AND n.status='passed')
          AND EXISTS (SELECT 1 FROM error_exports e WHERE e.pair_id=pairs.pair_id
            AND e.format_version=pairs.result_version AND e.error_count=0)
        ORDER BY repo COLLATE NOCASE,pair_index
        """,
        (RESULT_FORMAT_VERSION,),
    ).fetchall()
    records = [
        {
            "repo": row["repo"],
            "pair_index": row["pair_index"],
            "old_hash": row["old_hash"],
            "new_hash": row["new_hash"],
            "old_version": row["old_version"],
            "new_version": row["new_version"],
            "pair_id": row["pair_id"],
            "outcome": row["outcome"],
            "lean_error_count": 0,
        }
        for row in rows
    ]
    write_jsonl_atomic(output, records)
    return len(records)


def cleanup_stale_worktrees(
    args,
    connection: sqlite3.Connection,
    repo_paths: dict[str, Path],
) -> None:
    if args.keep_worktrees:
        return
    touched_repositories: set[str] = set()
    short_root = selected_worktrees_root(args)
    legacy_root = args.state_root / "worktrees"
    trash_root = short_root / ".trash"
    if trash_root.is_dir():
        # Worktrees an earlier run discarded but had not finished deleting.
        if WORKTREE_TRASH is not None:
            leftover = WORKTREE_TRASH.sweep()
            if leftover:
                print(f"Deleting {leftover} discarded worktree(s) in the background.", flush=True)
        else:
            for entry in list(trash_root.iterdir()):
                if entry.is_dir():
                    shutil.rmtree(entry, onerror=_make_writable_and_retry)
    roots_and_patterns = [(short_root, "*")]
    if legacy_root.resolve() != short_root.resolve():
        roots_and_patterns.append((legacy_root, "*/*"))

    for root, pattern in roots_and_patterns:
        if not root.exists():
            continue
        for pair_dir in root.glob(pattern):
            if not pair_dir.is_dir() or pair_dir.name == ".trash":
                continue
            row = connection.execute(
                "SELECT repo FROM pairs WHERE pair_id=?",
                (pair_dir.name,),
            ).fetchone()
            if not row or row["repo"] not in repo_paths:
                continue
            touched_repositories.add(row["repo"])
            repo_path = repo_paths[row["repo"]]
            for worktree in (pair_dir / "target", pair_dir / "new", pair_dir / "old"):
                if worktree.exists():
                    remove_worktree(repo_path, worktree, root)
            try:
                pair_dir.rmdir()
            except OSError:
                pass
    for repo_name in touched_repositories:
        git(repo_paths[repo_name], "worktree", "prune", check=False)


def order_pairs(args, pairs: list[dict]) -> list[dict]:
    """Highest expected Lean-error yield first; equal-yield pairs grouped by toolchain.

    Grouping by toolchain lets the bounded toolchain store reuse installs
    instead of re-downloading them.  Within one repository, ties keep
    discovery order.
    """
    metadata = load_pair_metadata(args.state_root / "pairs")
    ranked = []
    for pair in pairs:
        priority = pair_priority(pair, metadata.get((pair["new_hash"], pair["old_hash"])))
        if getattr(args, "skip_low_yield", False) and priority >= 3:
            continue
        ranked.append((priority, pair["new_version"], pair["old_version"],
                       pair["repo"].casefold(), pair["pair_index"], pair))
    ranked.sort(key=lambda item: item[:5])
    return [item[-1] for item in ranked]


def error_file_count(errors_root: Path) -> int:
    try:
        with os.scandir(errors_root) as iterator:
            return sum(1 for entry in iterator if entry.name.endswith(".json"))
    except OSError:
        return 0


def make_after_pair_hook(args, connection: sqlite3.Connection):
    """Trim disk caches between pairs (runs under the scheduler lock)."""
    managed_versions = {
        value
        for row in connection.execute("SELECT DISTINCT old_version,new_version FROM pairs")
        for value in row
    }
    toolchains = ToolchainJanitor(
        managed_versions,
        getattr(args, "max_toolchains", 0),
        protect=getattr(args, "protect_toolchain", None) or (),
        log=lambda message: print(message, flush=True),
    )
    mathlib = MathlibCacheJanitor(
        getattr(args, "mathlib_cache_max_gb", 0),
        CACHE_GATE,
        log=lambda message: print(message, flush=True),
    )
    lookahead = 2 * max(1, args.workers) + 2

    def after_pair(scheduler: PairScheduler) -> None:
        in_use = scheduler.versions_in_use(lookahead)
        toolchains.touch(in_use)
        try:
            toolchains.evict(in_use)
            mathlib.maybe_prune()
        except Exception as exc:  # disk hygiene must never stop data collection
            print(f"  cache cleanup warning: {exc}", file=sys.stderr, flush=True)

    return after_pair


def preserve_pair_outputs(args, connection, pair_id: str, repo_path: Path) -> None:
    """Runs while the repository is still reserved by this worker."""
    try:
        row = connection.execute("SELECT * FROM pairs WHERE pair_id=?", (pair_id,)).fetchone()
        if row and row["status"] == "completed" and row["outcome"] == "build-errors":
            save_repair_diff(args, row, repo_path)
        if row and getattr(args, "delete_finished_repos", False):
            if delete_finished_clone(args, connection, row["repo"], repo_path):
                print(f"  deleted finished clone: {row['repo']}", flush=True)
    except PipelineInterrupted:
        raise
    except Exception as exc:  # disk hygiene must never stop data collection
        print(f"  clone cleanup warning for {pair_id}: {exc}", file=sys.stderr, flush=True)


def pair_worker(
    args,
    scheduler: PairScheduler,
    connection: sqlite3.Connection,
    repo_paths: dict[str, Path],
    numbering: dict[str, int],
    total: int,
    lakeva_command: list[str],
    lake_command: list[str],
) -> None:
    while True:
        pair = scheduler.next()
        if pair is None:
            if STOP_EVENT.is_set():
                raise PipelineInterrupted("pipeline interrupted")
            return
        try:
            if WORKTREE_TRASH is not None and WORKTREE_TRASH.queued_bytes() > WORKTREE_TRASH.max_bytes:
                print(f"  waiting for background worktree deletion "
                      f"({WORKTREE_TRASH.queued_bytes() / 2**30:.1f} GiB queued)", flush=True)
                WORKTREE_TRASH.wait_below()
            print(
                f"[pair {numbering[pair['pair_id']]}/{total}] {pair['repo']} "
                f"{pair['old_version']} -> {pair['new_version']}",
                flush=True,
            )
            process_pair(
                args,
                connection,
                pair,
                repo_paths[pair["repo"]],
                lakeva_command,
                lake_command,
            )
            preserve_pair_outputs(args, connection, pair["pair_id"], repo_paths[pair["repo"]])
        finally:
            scheduler.done(pair)


def threaded_pair_worker(args, database_path: Path, scheduler: PairScheduler, *rest) -> None:
    connection = open_database(database_path)
    try:
        pair_worker(args, scheduler, connection, *rest)
    finally:
        connection.close()


def stop_parallel_workers(
    executor: ThreadPoolExecutor,
    futures: list[Future],
) -> None:
    STOP_EVENT.set()
    for future in futures:
        future.cancel()
    executor.shutdown(wait=True, cancel_futures=True)


def run_pairs(args, connection: sqlite3.Connection, repositories: list[dict]) -> None:
    """Run the pair phase with the background worktree janitor (see --no-async-cleanup).

    The janitor exists only for the duration of the run.  After a normal
    completion its queue is drained; after an interruption the remaining
    trash is left in place and swept at the next startup.
    """
    global WORKTREE_TRASH
    WORKTREE_TRASH = None
    if not getattr(args, "no_async_cleanup", False) and not getattr(args, "keep_worktrees", False):
        WORKTREE_TRASH = WorktreeTrash(
            selected_worktrees_root(args) / ".trash",
            int(float(getattr(args, "trash_max_gb", 20)) * 2**30),
            onerror=_make_writable_and_retry,
            log=lambda message: print(message, flush=True),
            stop=STOP_EVENT,
        )
        WORKTREE_TRASH.start()
    try:
        _run_pairs(args, connection, repositories)
        drain_worktree_trash()
    finally:
        WORKTREE_TRASH = None


def _run_pairs(args, connection: sqlite3.Connection, repositories: list[dict]) -> None:
    if not args.acknowledge_untrusted_builds:
        raise RuntimeError(
            "Building public repositories executes untrusted Lake/Lean code. "
            "Re-run with --acknowledge-untrusted-builds after reviewing this risk."
        )
    lakeva_command = command_prefix(args.lakeva)
    lake_command = command_prefix(args.lake)
    STOP_EVENT.clear()
    NETWORK_WATCHDOG.configure(getattr(args, "network_failure_limit", 5))
    preserve_and_prune_clones(args, connection, repositories)
    repo_paths = {item["name"]: item["path"] for item in repositories if not item.get("clone_deleted")}
    skipped = connection.execute(
        "SELECT COUNT(*) FROM pairs p JOIN repos r ON r.repo=p.repo "
        "WHERE p.active=1 AND p.status='pending' AND r.clone_deleted=1"
    ).fetchone()[0]
    if skipped:
        print(
            f"Skipping {skipped} pending pair(s) whose repository clone was deleted "
            "(--restore-deleted-clones re-clones them).",
            flush=True,
        )
    cleanup_stale_worktrees(args, connection, repo_paths)
    refresh_dependency_mirrors(dependency_mirror_root(args), getattr(args, "cache_timeout", 1800))

    statuses = ["pending", "pipeline_failed"] if args.retry_failed else ["pending"]
    placeholders = ",".join("?" for _ in statuses)
    query = (
        f"SELECT * FROM pairs WHERE active=1 AND status IN ({placeholders}) "
        "ORDER BY repo COLLATE NOCASE, pair_index"
    )
    pairs = order_pairs(args, [
        dict(row)
        for row in connection.execute(query, statuses).fetchall()
        if row["repo"] in repo_paths
    ])
    if args.max_pairs:
        pairs = pairs[: args.max_pairs]
    total = len(pairs)
    if not pairs:
        return
    numbering = {pair["pair_id"]: index for index, pair in enumerate(pairs, 1)}

    target = getattr(args, "target_error_files", None)
    should_drain = None
    if target:
        def should_drain() -> bool:
            reached = error_file_count(args.errors_root) >= target
            if reached:
                print(f"Reached {target} parsed Lean error files; finishing active pairs.", flush=True)
            return reached

    after_pair = make_after_pair_hook(args, connection)
    scheduler = PairScheduler(
        pairs,
        stop=STOP_EVENT,
        should_drain=should_drain,
        after_pair=after_pair,
    )
    # The first trim can remove hundreds of stale toolchains and cache files;
    # do it before workers start rather than while they wait on the scheduler.
    print("Trimming toolchain and Mathlib caches to their configured limits...", flush=True)
    after_pair(scheduler)
    common = (scheduler, repo_paths, numbering, total, lakeva_command, lake_command)
    if args.workers == 1:
        pair_worker(args, scheduler, connection, *common[1:])
        return

    database_path = args.state_root / "progress.sqlite3"
    worker_count = min(args.workers, len({pair["repo"] for pair in pairs}))
    print(
        f"Running {total} pair(s) from {len({pair['repo'] for pair in pairs})} repositories "
        f"with {worker_count} workers.",
        flush=True,
    )
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="lean-pipeline")
    futures: list[Future] = []
    try:
        for _ in range(worker_count):
            futures.append(executor.submit(threaded_pair_worker, args, database_path, *common))
        for future in as_completed(futures):
            future.result()
    except (KeyboardInterrupt, PipelineInterrupted):
        stop_parallel_workers(executor, futures)
        raise KeyboardInterrupt
    except BaseException:
        stop_parallel_workers(executor, futures)
        raise
    else:
        executor.shutdown(wait=True)


def drain_worktree_trash() -> None:
    """After a normal completion, finish background deletions before exiting."""
    trash = WORKTREE_TRASH
    if trash is None or STOP_EVENT.is_set() or not trash.pending():
        return
    print(f"Finishing background deletion of {trash.pending()} discarded worktree(s)...", flush=True)
    trash.drain()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repos-root", type=Path, default=DEFAULT_REPOS)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--worktrees-root",
        type=Path,
        help=(
            "short-lived checkout directory "
            "(defaults to a state-specific directory under the system temp path)"
        ),
    )
    parser.add_argument("--errors-root", type=Path, default=DEFAULT_ERRORS)
    parser.add_argument(
        "--repair-diffs-root",
        type=Path,
        default=DEFAULT_REPAIR_DIFFS,
        help="where old->new commit diffs of pairs with Lean errors are saved (gzip)",
    )
    parser.add_argument(
        "--delete-finished-repos",
        action="store_true",
        help="delete a repository clone once none of its pairs is pending, after "
             "saving the repair diff of every pair with Lean errors",
    )
    parser.add_argument(
        "--restore-deleted-clones",
        action="store_true",
        help="re-clone deleted repositories that still have pending or retryable pairs, "
             "using the manifest's URL and clone mode, before running",
    )
    parser.add_argument(
        "--network-failure-limit",
        type=int,
        default=5,
        help="pause the run after this many consecutive network failures when "
             f"{NETWORK_PROBE_HOST} is unreachable; affected pairs stay pending "
             "(0 disables; default: 5)",
    )
    parser.add_argument(
        "--no-lean-errors-output",
        type=Path,
        default=DEFAULT_NO_LEAN_ERRORS,
        help="JSONL list of analyzed pairs with zero Lean diagnostics",
    )
    parser.add_argument("--phase", choices=("all", "discover", "run"), default="all")
    parser.add_argument("--repo", action="append", help="process only this owner/repo; repeatable")
    parser.add_argument("--max-repos", type=int, help="limit selected repositories (testing)")
    parser.add_argument("--max-pairs", type=int, help="limit pairs processed this run (testing)")
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="repositories processed concurrently during the run phase (default: 2)",
    )
    parser.add_argument("--rediscover", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-adapt-failures", action="store_true")
    parser.add_argument("--retry-native-failures", action="store_true")
    parser.add_argument('--retry-build-inconclusive', action='store_true')
    parser.add_argument('--repair-results', action='store_true', help='Revalidate and re-export saved results without building repositories')
    parser.add_argument("--keep-worktrees", action="store_true")
    parser.add_argument("--skip-cache", action="store_true")
    parser.add_argument("--lakeva", default="lakeva")
    parser.add_argument("--lake", default="lake")
    parser.add_argument(
        "--native-first",
        action="store_true",
        help="validate both endpoints natively before adapting (previous, slower order); "
             "by default native builds run only for adapted builds with Lean errors",
    )
    parser.add_argument(
        "--validate-clean-pairs",
        action="store_true",
        help="also natively validate pairs whose adapted build is clean, for "
             "pairs-without-lean-errors.jsonl; requeues earlier 'unvalidated' pairs",
    )
    parser.add_argument(
        "--skip-low-yield",
        action="store_true",
        help="skip same-minor (patch/rc) transitions and pairs without .lean changes "
             "instead of running them last",
    )
    parser.add_argument(
        "--target-error-files",
        type=int,
        help="stop scheduling new pairs once parsed-errors/ holds this many files",
    )
    parser.add_argument(
        "--include-mathlib",
        action="store_true",
        help="also adapt commits of Mathlib itself (forks); each needs a full Mathlib "
             "compile and normally exceeds the build timeout. Requeues skipped pairs",
    )
    parser.add_argument(
        "--max-toolchains",
        type=int,
        default=12,
        help="keep at most this many pipeline-used Lean toolchains installed, evicting "
             "the least recently used (elan reinstalls on demand); 0 disables (default: 12)",
    )
    parser.add_argument(
        "--protect-toolchain",
        action="append",
        help="never evict this toolchain (the elan default and overrides are always kept); repeatable",
    )
    parser.add_argument(
        "--mathlib-cache-max-gb",
        type=float,
        default=15,
        help="prune Mathlib's downloaded .ltar cache to this size; 0 disables (default: 15)",
    )
    parser.add_argument(
        "--no-compress-logs",
        action="store_true",
        help="do not NTFS-compress state/runs and state/native-checks on Windows",
    )
    parser.add_argument(
        "--dep-mirror-root",
        type=Path,
        default=DEFAULT_DEP_MIRROR_ROOT,
        help="local bare mirrors of the common Lean dependencies (Mathlib, Batteries, Std, "
             "Aesop, Quote4, ImportGraph, Cli, Plausible, LeanSearchClient); every worktree "
             "is seeded from them so Lake does not re-download them "
             "(default: %%USERPROFILE%%\\.cache\\lvp-dep-mirrors)",
    )
    parser.add_argument(
        "--no-dep-mirror",
        action="store_true",
        help="let Lake clone every dependency from its remote, as before",
    )
    parser.add_argument(
        "--no-async-cleanup",
        action="store_true",
        help="delete finished worktrees inline instead of on a background thread",
    )
    parser.add_argument(
        "--trash-max-gb",
        type=float,
        default=20,
        help="pause starting new pairs while more than this much discarded worktree data "
             "awaits background deletion (default: 20)",
    )
    parser.add_argument(
        "--no-fork-reuse",
        action="store_true",
        help="do not copy finished results of another repository with identical commits "
             "and toolchains (fork duplicates are then rebuilt)",
    )
    parser.add_argument(
        "--no-timeout-prediction",
        action="store_true",
        help="always run builds whose identical twin already hit the time limit and builds "
             "whose ProofWidgets rebuild would cascade into a full Mathlib rebuild",
    )
    parser.add_argument("--cache-timeout", type=int, default=1800)
    parser.add_argument("--native-timeout", type=int, default=3600)
    parser.add_argument(
        "--native-attempts",
        type=int,
        default=2,
        help="attempts for inconclusive native infrastructure failures (default: 2)",
    )
    parser.add_argument("--adapt-timeout", type=int, default=3600)
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--acknowledge-untrusted-builds", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.network_failure_limit < 0:
        parser.error("--network-failure-limit must be 0 or more")
    if args.native_attempts < 1:
        parser.error("--native-attempts must be at least 1")
    if args.trash_max_gb < 0:
        parser.error("--trash-max-gb must be 0 or more")
    args.dep_mirror_root = args.dep_mirror_root.resolve()
    for name in (
        "manifest",
        "repos_root",
        "state_root",
        "errors_root",
        "no_lean_errors_output",
        "repair_diffs_root",
    ):
        setattr(args, name, getattr(args, name).resolve())
    if args.worktrees_root is None:
        state_key = hashlib.sha256(
            str(args.state_root).casefold().encode("utf-8")
        ).hexdigest()[:10]
        args.worktrees_root = (DEFAULT_WORKTREES / state_key).resolve()
    else:
        args.worktrees_root = args.worktrees_root.resolve()
    return args


def main() -> int:
    args = parse_args()
    database = args.state_root / 'progress.sqlite3'
    if args.status:
        if not database.is_file():
            print('No saved progress yet.')
            return 0
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            show_status(connection, mirror_root=dependency_mirror_root(args))
        finally:
            connection.close()
        return 0
    with state_lock(args.state_root):
        if database.exists():
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as inspection:
                try:
                    previous = inspection.execute("SELECT value FROM pipeline_meta WHERE key='result-repair-version'").fetchone()
                except sqlite3.OperationalError:
                    previous = None
            if args.repair_results or not previous or previous[0] != str(RESULT_FORMAT_VERSION):
                backup = backup_results(args)
                print(f'Preserved database and reports before repair: {backup}', flush=True)
        return main_locked(args)


def main_locked(args) -> int:
    validate_errors_directory(args.errors_root)
    connection = connect_database(args.state_root / "progress.sqlite3")
    migrate_native_check_protocol(connection, args)
    migrate_legacy_adapter_reports(args, connection)
    migrate_network_failures(args, connection)
    if args.retry_adapt_failures:
        connection.execute(
            "UPDATE pairs SET status='pending', outcome=NULL, error=NULL, error_file=NULL, "
            "updated_at=? WHERE status='adapt_failed'",
            (now(),),
        )
        connection.commit()
    if args.retry_native_failures:
        connection.execute(
            "DELETE FROM native_checks WHERE status IN ('failed','inconclusive')"
        )
        connection.execute(
            "UPDATE pairs SET status='pending', outcome=NULL, error=NULL, error_file=NULL, updated_at=? "
            "WHERE status IN ('discarded_native','native_inconclusive')",
            (now(),),
        )
        connection.commit()
    if args.retry_build_inconclusive:
        connection.execute("UPDATE pairs SET status='pending',outcome=NULL,error=NULL,error_file=NULL,updated_at=? WHERE status='build_inconclusive'", (now(),))
        connection.commit()
    if args.include_mathlib:
        connection.execute("UPDATE pairs SET status='pending',outcome=NULL,updated_at=? WHERE status='ineligible_pair' AND outcome='mathlib-itself'", (now(),))
        connection.commit()
    if args.validate_clean_pairs:
        connection.execute("UPDATE pairs SET status='pending',outcome=NULL,error=NULL,error_file=NULL,updated_at=? WHERE status='unvalidated'", (now(),))
        connection.commit()
    compressed = connection.execute(
        "SELECT 1 FROM pipeline_meta WHERE key='ntfs-compressed-logs'"
    ).fetchone()
    if os.name == "nt" and not (compressed or args.no_compress_logs or args.repair_results)             and args.phase in {"all", "run"}:
        # One pass compresses existing logs and marks the directories, so
        # every later log is compressed on write.
        print("Enabling NTFS compression for build logs (one-time)...", flush=True)
        if all(enable_ntfs_compression(directory)
               for directory in (args.state_root / "runs", args.state_root / "native-checks")):
            connection.execute(
                "INSERT INTO pipeline_meta(key,value,updated_at) VALUES('ntfs-compressed-logs','1',?) "
                "ON CONFLICT(key) DO NOTHING", (now(),))
            connection.commit()
    repositories = load_manifest(args, connection)
    if getattr(args, "restore_deleted_clones", False):
        restore_deleted_clones(args, connection, repositories)
    if args.repair_results:
        connection.execute("UPDATE pairs SET result_version=NULL WHERE status IN ('completed','build_inconclusive')")
        connection.commit()
    migrate_legacy_error_reports(args, connection, repositories)
    connection.execute("INSERT INTO pipeline_meta(key,value,updated_at) VALUES('result-repair-version',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(RESULT_FORMAT_VERSION), now()))
    connection.commit()
    no_error_count = export_pairs_without_lean_errors(
        connection, args.no_lean_errors_output
    )
    if args.repair_results:
        print(
            f"Pairs without Lean errors: {no_error_count} "
            f"({args.no_lean_errors_output})"
        )
        show_status(connection)
        connection.close()
        return 0
    if args.phase in {"all", "discover"}:
        discover_repositories(args, connection, repositories)
    if args.phase in {"all", "run"}:
        try:
            run_pairs(args, connection, repositories)
        finally:
            no_error_count = export_pairs_without_lean_errors(
                connection, args.no_lean_errors_output
            )
    elif args.phase == "discover":
        no_error_count = export_pairs_without_lean_errors(
            connection, args.no_lean_errors_output
        )
    print(
        f"Pairs without Lean errors: {no_error_count} "
        f"({args.no_lean_errors_output})"
    )
    show_status(connection)
    connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, PipelineInterrupted):
        if NETWORK_WATCHDOG.tripped:
            print(
                "\nPaused after repeated network failures. Progress is checkpointed; "
                "re-run the same command once connectivity is restored.",
                file=sys.stderr,
            )
            raise SystemExit(75)
        print("\nInterrupted. Progress is checkpointed; re-run the same command to resume.", file=sys.stderr)
        raise SystemExit(130)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)

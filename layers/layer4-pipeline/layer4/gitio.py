"""Thin, batched wrapper over git plumbing.

Design note: a `--filter=tree:0` (treeless) clone is *unusable* here -- any
path-filtered history walk triggers one lazy tree fetch per commit and takes
hours. Use `--filter=blob:none` (trees local, blobs lazy) or a full clone.
`Git.check_clone_health` warns about this.
"""

from __future__ import annotations

import functools
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_date: str
    subject: str
    body: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


_REC = "\x1e"  # record separator
_FLD = "\x1f"  # field separator
_FMT = _FLD.join(["%H", "%P", "%aI", "%s", "%b"]) + _REC


class Git:
    def __init__(self, repo: str | Path, timeout: int = 1800):
        self.repo = Path(repo)
        self.timeout = timeout
        if not (self.repo / ".git").exists() and not (self.repo / "HEAD").exists():
            raise GitError(f"not a git repository: {self.repo}")

    # ---------------------------------------------------------------- core

    def run(self, *args: str, check: bool = True) -> str:
        return self._run(args, check=check).decode("utf-8", "replace")

    def run_bytes(self, *args: str, check: bool = True) -> bytes:
        return self._run(args, check=check)

    def _run(self, args: tuple[str, ...], check: bool) -> bytes:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            timeout=self.timeout,
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args[:4])}... failed ({proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace')[:400]}"
            )
        return proc.stdout

    # ------------------------------------------------------------ queries

    def rev_parse(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def ref_exists(self, ref: str) -> bool:
        try:
            self.rev_parse(ref)
            return True
        except GitError:
            return False

    def local_heads(self) -> dict[str, str]:
        out = self.run("for-each-ref", "--format=%(refname:short)%09%(objectname)",
                       "refs/heads", "refs/remotes")
        heads: dict[str, str] = {}
        for line in out.splitlines():
            if "\t" in line:
                name, sha = line.split("\t", 1)
                heads[name] = sha
        return heads

    @staticmethod
    def ls_remote_heads(url: str, *patterns: str, timeout: int = 600) -> dict[str, str]:
        """Branch discovery without a clone. Patterns are git refspec globs."""
        cmd = ["git", "ls-remote", "--heads", url, *patterns]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise GitError(proc.stderr.decode("utf-8", "replace")[:400])
        heads = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if "\t" not in line:
                continue
            sha, ref = line.split("\t", 1)
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/"):]] = sha
        return heads

    def log(self, *revspec: str, paths: list[str] | None = None,
            no_merges: bool = False, limit: int | None = None) -> list[Commit]:
        args = ["log", f"--format={_FMT}"]
        if no_merges:
            args.append("--no-merges")
        if limit:
            args.append(f"-{limit}")
        args.extend(revspec)
        if paths:
            args.append("--")
            args.extend(paths)
        return self._parse_log(self.run(*args))

    def commit(self, sha: str) -> Commit:
        commits = self._parse_log(self.run("log", "-1", f"--format={_FMT}", sha))
        if not commits:
            raise GitError(f"no such commit: {sha}")
        return commits[0]

    @staticmethod
    def _parse_log(out: str) -> list[Commit]:
        commits = []
        for rec in out.split(_REC):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            fields = rec.split(_FLD)
            if len(fields) < 4:
                continue
            sha, parents, date, subject = fields[:4]
            body = fields[4] if len(fields) > 4 else ""
            commits.append(Commit(sha.strip(), tuple(parents.split()), date, subject, body))
        return commits

    def rev_list(self, include: str, exclude: str | None = None,
                 no_merges: bool = True) -> list[str]:
        """Commits reachable from `include` but not from `exclude`.

        This is the uniform primitive for adaptation windows: master merges
        *into* a bump branch are ancestors of `exclude`, so they drop out
        automatically and only genuine adaptation commits survive.
        """
        args = ["rev-list", "--reverse"]
        if no_merges:
            args.append("--no-merges")
        args.append(include)
        if exclude:
            args.append(f"^{exclude}")
        return self.run(*args).split()

    # -------------------------------------------------------------- blobs

    @functools.lru_cache(maxsize=4096)
    def _show_cached(self, sha: str, path: str) -> str | None:
        out = self._run(("show", f"{sha}:{path}"), check=False)
        return out.decode("utf-8", "replace") if out else None

    def file_at(self, sha: str, path: str) -> str | None:
        """File contents at a commit, or None if absent."""
        return self._show_cached(sha, path)

    def diff(self, base: str, tip: str, paths: list[str] | None = None,
             context: int = 6, find_renames: bool = True) -> str:
        args = ["diff", f"-U{context}", "--no-color", "--no-ext-diff",
                "--ignore-submodules", "--no-textconv"]
        if find_renames:
            args.append("--find-renames")
        args.extend([base, tip])
        if paths:
            args.append("--")
            args.extend(paths)
        return self.run_bytes(*args).decode("utf-8", "replace")

    def show_diff(self, sha: str, paths: list[str] | None = None,
                  context: int = 6) -> str:
        """Diff of a single commit against its first parent."""
        args = ["show", f"-U{context}", "--no-color", "--no-ext-diff",
                "--format=", "--find-renames", "--first-parent", sha]
        if paths:
            args.append("--")
            args.extend(paths)
        return self.run_bytes(*args).decode("utf-8", "replace")

    # ------------------------------------------------------------- health

    def check_clone_health(self) -> list[str]:
        warnings = []
        try:
            filt = self.run("config", "--get", "remote.origin.partialclonefilter",
                            check=False).strip()
        except GitError:
            filt = ""
        if filt.startswith("tree:"):
            warnings.append(
                "Clone uses a treeless filter (%s). Path-filtered history walks will "
                "lazily fetch a tree per commit and are effectively unusable. "
                "Re-clone with --filter=blob:none." % filt
            )
        elif filt.startswith("blob:"):
            warnings.append(
                "Clone uses %s. Blob access is lazy: expect network round-trips "
                "during hunk extraction. A full clone is ~3-5x faster end to end." % filt
            )
        if self.run("rev-parse", "--is-shallow-repository", check=False).strip() == "true":
            warnings.append("Shallow clone: history-based discovery will miss windows.")
        return warnings


def clone(url: str, dest: str | Path, *, blobless: bool = True,
          branch: str = "master", timeout: int = 3600) -> Git:
    """Recommended clone for this pipeline: trees local, blobs lazy."""
    dest = Path(dest)
    cmd = ["git", "clone", "--no-checkout", "--single-branch", "--branch", branch]
    if blobless:
        cmd.append("--filter=blob:none")
    cmd.extend([url, str(dest)])
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode("utf-8", "replace")[:800])
    return Git(dest)

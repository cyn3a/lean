from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ResolutionError


@dataclass(frozen=True)
class HistorySelection:
    revision: str
    reason: str


class GitHistoryResolver:
    def __init__(self, git_command: str = "git", timeout: float = 180.0) -> None:
        self.git_command = git_command
        self.timeout = timeout

    def find_exact_toolchain(
        self, repository_url: str, target_toolchain: str, subdir: str | None = None
    ) -> HistorySelection | None:
        toolchain_path = self._toolchain_path(subdir)
        with tempfile.TemporaryDirectory(prefix="lakeva-git-") as temporary:
            repo = Path(temporary) / "repo"
            self._run(
                [
                    self.git_command,
                    "clone",
                    "--quiet",
                    "--filter=blob:none",
                    "--no-checkout",
                    repository_url,
                    os.fspath(repo),
                ]
            )

            candidates: dict[str, str] = {}
            tips = self._lines(
                repo,
                [
                    "for-each-ref",
                    "--format=%(objectname)",
                    "refs/remotes/origin",
                    "refs/tags",
                ],
            )
            for revision in tips:
                if self._toolchain_at(repo, revision, toolchain_path) == target_toolchain:
                    candidates[revision] = "latest matching branch or tag tip"

            changes = self._lines(
                repo, ["log", "--all", "--format=%H", "--", toolchain_path]
            )
            for revision in changes:
                if self._toolchain_at(repo, revision, toolchain_path) == target_toolchain:
                    candidates.setdefault(revision, "commit that introduced the target toolchain")
                parents = self._lines(repo, ["show", "-s", "--format=%P", revision])
                if parents:
                    for parent in parents[0].split():
                        if self._toolchain_at(repo, parent, toolchain_path) == target_toolchain:
                            candidates.setdefault(parent, "last commit before a toolchain change")

            if not candidates:
                return None
            selected = max(candidates, key=lambda revision: self._commit_time(repo, revision))
            # A final exact check is deliberately redundant: never return an inferred revision.
            if self._toolchain_at(repo, selected, toolchain_path) != target_toolchain:
                return None
            return HistorySelection(selected, candidates[selected])

    def _commit_time(self, repo: Path, revision: str) -> int:
        value = self._run(
            [self.git_command, "-C", os.fspath(repo), "show", "-s", "--format=%ct", revision]
        ).stdout.strip()
        try:
            return int(value)
        except ValueError:
            return 0

    def _toolchain_at(self, repo: Path, revision: str, path: str) -> str | None:
        result = subprocess.run(
            [self.git_command, "-C", os.fspath(repo), "show", f"{revision}:{path}"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _lines(self, repo: Path, arguments: list[str]) -> list[str]:
        result = self._run([self.git_command, "-C", os.fspath(repo), *arguments])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                arguments,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=True,
            )
        except FileNotFoundError as exc:
            raise ResolutionError(f"Git executable not found: {self.git_command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ResolutionError(f"Git operation timed out for {arguments[-1]}") from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or f"exit {exc.returncode}"
            raise ResolutionError(f"Git operation failed: {detail}") from exc

    @staticmethod
    def _toolchain_path(subdir: str | None) -> str:
        if not subdir:
            return "lean-toolchain"
        path = PurePosixPath(subdir.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ResolutionError(f"unsafe package subdirectory: {subdir!r}")
        return str(path / "lean-toolchain")

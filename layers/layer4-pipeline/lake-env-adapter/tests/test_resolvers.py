from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lake_env_adapter.git_history import GitHistoryResolver
from lake_env_adapter.reservoir import ReservoirClient


class ResolverTests(unittest.TestCase):
    def test_reservoir_requires_success_and_exact_checked_toolchain(self) -> None:
        target = "leanprover/lean4:v4.20.0"

        def get_json(url: str):
            if url.endswith("/builds"):
                return {
                    "data": [
                        {
                            "revision": "failed",
                            "toolchain": target,
                            "built": False,
                            "runAt": "2026-01-03",
                        },
                        {
                            "revision": "mismatch",
                            "toolchain": target,
                            "built": True,
                            "runAt": "2026-01-02",
                        },
                        {
                            "revision": "eligible",
                            "toolchain": target,
                            "built": True,
                            "runAt": "2026-01-01",
                        },
                    ]
                }
            return {
                "sources": [
                    {
                        "host": "github",
                        "fullName": "acme/alpha",
                        "gitUrl": "https://github.com/acme/alpha.git",
                    }
                ]
            }

        def get_text(url: str) -> str:
            return target if "/eligible/" in url else "leanprover/lean4:v4.19.0"

        client = ReservoirClient(json_transport=get_json, text_transport=get_text)
        selected = client.select_exact_build("acme", "alpha", target)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.revision, "eligible")  # type: ignore[union-attr]

    def test_git_history_selects_last_commit_before_toolchain_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "source"
            repo.mkdir()
            self._git(repo, "init", "-q")
            self._git(repo, "config", "user.name", "Tests")
            self._git(repo, "config", "user.email", "tests@example.test")
            (repo / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n", encoding="utf-8")
            self._git(repo, "add", "lean-toolchain")
            self._git(repo, "commit", "-q", "-m", "lean 4.19")
            (repo / "README.md").write_text("latest 4.19 state\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-q", "-m", "work on 4.19")
            expected = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
            self._git(repo, "add", "lean-toolchain")
            self._git(repo, "commit", "-q", "-m", "lean 4.20")

            selected = GitHistoryResolver().find_exact_toolchain(
                os.fspath(repo), "leanprover/lean4:v4.19.0"
            )
            self.assertIsNotNone(selected)
            self.assertEqual(selected.revision, expected)  # type: ignore[union-attr]

    @staticmethod
    def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", os.fspath(repo), *arguments],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()

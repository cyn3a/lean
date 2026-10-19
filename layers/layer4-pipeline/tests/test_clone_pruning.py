from __future__ import annotations

from argparse import Namespace
import gzip
from pathlib import Path
import subprocess
import tempfile
import unittest

import run_pipeline as pipeline
from test_parallel_pipeline import insert_pair


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_repo(path: Path) -> tuple[str, str]:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "Demo.lean").write_text("example := foo\n", encoding="utf8")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "old")
    old = git(path, "rev-parse", "HEAD")
    (path / "Demo.lean").write_text("example := bar\n", encoding="utf8")
    git(path, "commit", "-q", "-am", "new")
    return old, git(path, "rev-parse", "HEAD")


class ClonePruningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repos_root = self.root / "repos"
        self.connection = pipeline.connect_database(self.root / "progress.sqlite3")
        self.addCleanup(self.connection.close)
        self.repositories = []
        for name, status, outcome in (("owner/errors", "completed", "build-errors"),
                                      ("owner/clean", "completed", "build-succeeded"),
                                      ("owner/busy", "pending", None)):
            path = self.repos_root / name.replace("/", "__")
            old, new = make_repo(path)
            insert_pair(self.connection, name.split("/")[1], name, 0, old, new)
            self.connection.execute("UPDATE pairs SET status=?,outcome=? WHERE repo=?", (status, outcome, name))
            self.connection.execute("INSERT INTO repos(repo,path,status,updated_at) VALUES(?,?,'discovered',?)",
                                    (name, str(path), pipeline.now()))
            self.repositories.append({"name": name, "path": path, "clone_deleted": False})
        self.connection.commit()
        self.args = Namespace(repos_root=self.repos_root, repair_diffs_root=self.root / "diffs",
                              delete_finished_repos=True)

    def test_deletes_only_finished_clones_without_lean_errors(self) -> None:
        pipeline.preserve_and_prune_clones(self.args, self.connection, self.repositories)

        diffs = list((self.root / "diffs").glob("*.diff.gz"))
        self.assertEqual(len(diffs), 1)
        with gzip.open(diffs[0], "rt", encoding="utf8") as handle:
            text = handle.read()
        self.assertIn("# repo: owner/errors", text)
        self.assertIn("-example := foo", text)
        self.assertIn("+example := bar", text)

        self.assertTrue((self.repos_root / "owner__errors").exists())
        self.assertFalse((self.repos_root / "owner__clean").exists())
        self.assertTrue((self.repos_root / "owner__busy").exists())
        flags = dict(self.connection.execute("SELECT repo,clone_deleted FROM repos").fetchall())
        self.assertEqual(flags, {"owner/errors": 0, "owner/clean": 1, "owner/busy": 0})
        self.assertTrue(self.repositories[1]["clone_deleted"])

    def test_without_flag_only_diffs_are_saved(self) -> None:
        self.args.delete_finished_repos = False
        pipeline.preserve_and_prune_clones(self.args, self.connection, self.repositories)
        self.assertEqual(len(list((self.root / "diffs").glob("*.diff.gz"))), 1)
        self.assertTrue((self.repos_root / "owner__clean").exists())


class MathlibDetectionTests(unittest.TestCase):
    def test_detects_mathlib_tree_from_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "fork"
            old, _new = make_repo(repo)
            self.assertFalse(pipeline.is_mathlib_itself(repo, old))
            (repo / "Mathlib.lean").write_text("import Mathlib.Tactic.Basic", encoding="utf8")
            self.assertFalse(pipeline.is_mathlib_itself(repo, old))
            (repo / "Mathlib" / "Tactic").mkdir(parents=True)
            (repo / "Mathlib" / "Tactic" / "Basic.lean").write_text("-- x", encoding="utf8")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "mathlib")
            self.assertTrue(pipeline.is_mathlib_itself(repo, git(repo, "rev-parse", "HEAD")))


if __name__ == "__main__":
    unittest.main()

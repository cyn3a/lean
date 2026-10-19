import tempfile
import unittest
from pathlib import Path

from result_validation import classify_adapted_build


class ResultValidationTests(unittest.TestCase):
    def test_empty_success_is_not_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "build.log"
            log.write_text("Build completed successfully (0 jobs).\n", encoding="utf-8")
            self.assertEqual(
                classify_adapted_build(0, [], log),
                ("build_inconclusive", "no-build-targets"),
            )

    def test_infrastructure_diagnostic_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "build.log"
            log.write_text(
                "error: external command 'git' exited with code 128\n"
                "Project/Main.lean:4:2: error: unknown identifier 'x'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                classify_adapted_build(
                    1,
                    [{"kind": "lean-diagnostic"}],
                    log,
                    infrastructure_failure=True,
                ),
                ("build_inconclusive", "adapted-build-infrastructure-failure"),
            )

    def test_nonzero_lean_build_is_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "build.log"
            log.write_text("Build completed (1 job).\n", encoding="utf-8")
            self.assertEqual(
                classify_adapted_build(
                    1,
                    [{"kind": "lean-diagnostic"}],
                    log,
                ),
                ("completed", "build-errors"),
            )


if __name__ == "__main__":
    unittest.main()

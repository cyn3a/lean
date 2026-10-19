from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from extract_lean_errors import enrich_lean_error, parse_lean_errors


class ExtractLeanErrorsTests(unittest.TestCase):
    def test_enriches_multiline_error_with_source_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "PFR" / "Example.lean"
            source.parent.mkdir()
            source.write_text(
                "\n".join(f"line {index}" for index in range(1, 13)) + "\n",
                encoding="utf-8",
            )
            log = (
                f"{source}:6:9: error: unsolved goals\n"
                "case example\n"
                "x : Nat\n"
                "⊢ x = x\n"
            )

            parsed = parse_lean_errors(log)
            self.assertEqual(len(parsed), 1)
            enriched = enrich_lean_error(parsed[0], root)

            self.assertEqual(
                enriched,
                {
                    "file": "PFR/Example.lean",
                    "line": 6,
                    "column": 9,
                    "message": "unsolved goals\ncase example\nx : Nat\n⊢ x = x",
                    "source_found": True,
                    "error_line": "line 6",
                    "code": "\n".join(f"line {index}" for index in range(2, 11)),
                    "context_start_line": 2,
                    "context_end_line": 10,
                },
            )

    def test_error_codes_start_distinct_diagnostics(self) -> None:
        text = (
            "Demo.lean:2:3: error(lean.unknownIdentifier): Unknown identifier `x`\n"
            "Demo.lean:4:5: error: type mismatch\n"
        )
        parsed = parse_lean_errors(text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["message"], "Unknown identifier `x`")
        self.assertEqual(parsed[1]["line"], 4)

    def test_lake_prefix_recovers_real_errors_and_stops_at_warning(self) -> None:
        # Reduced from MassFormula's real log, previously counted as zero errors.
        text = (
            "error: MassFormula/Defs.lean:63:2: Function expected at\n"
            "  Ideal.ramificationIdx ?m.25\n"
            "but this term has type\n"
            "  ℕ\n"
            "error: MassFormula/Defs.lean:63:24: Application type mismatch\n"
            "  algebraMap\n"
            "warning: MassFormula/Defs.lean:81:0: unused section variables\n"
            "  [ValuativeRel K]\n"
            "error: Lean exited with code 1\n"
        )
        errors = [e for e in parse_lean_errors(text) if e["kind"] == "lean-diagnostic"]
        self.assertEqual(len(errors), 2)
        self.assertEqual(errors[0]["file"], "MassFormula/Defs.lean")
        self.assertEqual(errors[0]["details"], "  Ideal.ramificationIdx ?m.25\nbut this term has type\n  ℕ")
        self.assertEqual(errors[1]["details"], "  algebraMap")

    def test_captured_stdout_fence_ends_message_without_losing_goals(self) -> None:
        # Build-script wrapper used by lean-cvc5; the old parser appended all
        # subsequent Lake progress and failed-target summaries to the last error.
        text = (
            "error: C++ to Lean translation failed:\n\n```stdout\n"
            "PreBuild.lean:81:2: error: unsolved goals\n"
            "case first\nx : Nat\n⊢ x = x\n\n"
            "```\n\n```stderr\n\n```\n"
            "✔ [3/19] Ran cvc5.Init\n"
            "Some required targets logged failures:\n- cvc5/cvc5Enums\n"
            "error: build failed\n\n# finished: 2026-09-22\n# exit_code: 1\n"
        )
        errors = parse_lean_errors(text)
        lean = [e for e in errors if e["kind"] == "lean-diagnostic"]
        self.assertEqual(len(lean), 1)
        self.assertEqual(lean[0]["details"], "case first\nx : Nat\n⊢ x = x")
        self.assertEqual(errors[-1], {"kind": "process-error", "message": "build failed"})

    def test_ansi_hyperlinks_windows_paths_and_deduplication(self) -> None:
        text = (
            "\x1b[31merror:\x1b[0m "
            "\x1b]8;;file:///C:/work/REPL/Main.lean\x1b\\"
            "C:\\work\\.\\REPL\\Main.lean\x1b]8;;\x1b\\:20:0: missing cases\n"
            "  foo\n"
            "trace: .> lean Main.lean\n"
            "C:/work/REPL/Main.lean:20:0: error: missing cases\n  foo\n"
            "✔ [2/3] Built REPL.Main\n"
        )
        errors = parse_lean_errors(text)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["file"], "C:/work/REPL/Main.lean")
        self.assertEqual(errors[0]["column"], 0)
        self.assertEqual(errors[0]["details"], "  foo")

    def test_non_lean_compiler_and_process_errors_are_not_lean_errors(self) -> None:
        text = (
            "error: external command 'git' exited with code 128\n"
            "fatal: could not read from remote repository\n"
            "generated.c:4:2: error: missing header\n"
            "error: source.cpp:1:2: unknown symbol\n"
            "warning: Demo.lean:3:0: declaration uses sorry\n"
            "error: build failed\n"
        )
        errors = parse_lean_errors(text)
        self.assertEqual(len(errors), 5)
        self.assertTrue(all(error["kind"] == "process-error" for error in errors))

    def test_json_diagnostics_preserve_lean_position_and_caption(self) -> None:
        diagnostic = {
            "fileName": ".\\Demo.lean", "pos": {"line": 4, "column": 0},
            "endPos": {"line": 5, "column": 8}, "severity": "error",
            "data": "unsolved goals\nx : Nat\n⊢ x = x\n", "caption": "while elaborating",
        }
        warning = {**diagnostic, "severity": "warning", "data": "ignored"}
        text = "\n".join([json.dumps(diagnostic), json.dumps(warning), "# exit_code: 1"])
        errors = parse_lean_errors(text)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["file"], "Demo.lean")
        self.assertEqual(errors[0]["line"], 4)
        self.assertEqual(errors[0]["column"], 0)
        self.assertEqual(errors[0]["message"], "while elaborating:")
        self.assertEqual(errors[0]["details"], "unsolved goals\nx : Nat\n⊢ x = x")

    def test_unrelated_or_malformed_json_does_not_crash_parser(self) -> None:
        text = '\n'.join([
            '{"severity": [], "fileName": "Demo.lean"}',
            '{"severity": "error", "pos": {"line": null}}',
            '{invalid json',
            'error: Demo.lean:2:0: actual error',
        ])
        self.assertEqual(len(parse_lean_errors(text)), 1)

    def test_ranges_error_codes_and_markdown_inside_messages(self) -> None:
        text = (
            "Demo.lean:2:3-4:8: error(lean.unknownIdentifier): Unknown identifier\n"
            "Example:\n```lean\nexample := x\n```\n"
            "\nNote: Try a different name.\n"
            "info: checking next module\n"
        )
        errors = parse_lean_errors(text)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["line"], 2)
        self.assertEqual(errors[0]["column"], 3)
        self.assertEqual(errors[0]["details"], "Example:\n```lean\nexample := x\n```\n\nNote: Try a different name.")

    def test_source_context_normalizes_dot_paths_and_cannot_escape_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            (root / "Demo.lean").write_text("example : True := by\n  trivial\n", encoding="utf-8")
            errors = parse_lean_errors(
                "error: .\\.\\.\\Demo.lean:1:0: unsolved goals\n"
                "error: ../outside.lean:1:0: other error\n"
            )
            report = enrich_lean_error(errors[0], root)
            self.assertTrue(report["source_found"])
            self.assertEqual(report["file"], "Demo.lean")
            fallback_calls = []
            outside = enrich_lean_error(errors[1], root, source_fallback=lambda path: fallback_calls.append(path))
            self.assertFalse(outside["source_found"])
            self.assertEqual(fallback_calls, [])


if __name__ == "__main__":
    unittest.main()

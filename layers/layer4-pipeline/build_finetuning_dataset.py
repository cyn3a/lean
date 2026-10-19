#!/usr/bin/env python3
"""Combine source-enriched Lean error reports into chat fine-tuning JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_ERRORS = HERE / "parsed-errors"
DEFAULT_OUTPUT = HERE / "lean-errors-finetune.jsonl"
SYSTEM_PROMPT = (
    "You analyze Lean 4 compiler errors caused by migrating a project to a newer "
    "Lean toolchain. Given the repository metadata, target location, and adapted "
    "source context, return one compact JSON object with exactly one field named "
    "diagnostics. Its value must be an array of objects with exactly these fields: "
    "file, line, column, and message."
)
REQUIRED_FIELDS = {
    "file",
    "line",
    "column",
    "message",
    "source_found",
    "error_line",
    "code",
    "context_start_line",
    "context_end_line",
    "repo",
    "old_version",
    "new_version",
    "old_hash",
    "new_hash",
    "pair_index",
    "pair_id",
}
MAX_MESSAGE_CHARS = 12_000


def training_message(message: str) -> tuple[str, bool]:
    """Bound pathological tactic states while retaining every error example."""
    if len(message) <= MAX_MESSAGE_CHARS:
        return message, False
    head_chars = 8_000
    tail_chars = 4_000
    omitted = len(message) - head_chars - tail_chars
    marker = f"\n\n[... {omitted} diagnostic characters omitted for fine-tuning ...]\n\n"
    return message[:head_chars] + marker + message[-tail_chars:], True


def load_report(path: Path) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError(f"Expected one JSON object in {path}")
    missing = sorted(REQUIRED_FIELDS - report.keys())
    if missing:
        raise ValueError(f"Missing fields in {path}: {', '.join(missing)}")
    if not isinstance(report["file"], str) or not isinstance(report["message"], str):
        raise ValueError(f"Invalid file/message types in {path}")
    if type(report["line"]) is not int or type(report["column"]) is not int:
        raise ValueError(f"Invalid line/column types in {path}")
    if type(report["source_found"]) is not bool:
        raise ValueError(f"Invalid source_found type in {path}")
    return report


def request_for(report: dict) -> dict:
    return {
        "task": "Predict the Lean compiler diagnostic for this migration failure.",
        "repo": report["repo"],
        "old_version": report["old_version"],
        "new_version": report["new_version"],
        "old_hash": report["old_hash"],
        "new_hash": report["new_hash"],
        "pair_id": report["pair_id"],
        "file": report["file"],
        "line": report["line"],
        "column": report["column"],
        "source_found": report["source_found"],
        "error_line": report["error_line"],
        "code": report["code"],
        "context_start_line": report["context_start_line"],
        "context_end_line": report["context_end_line"],
    }


def training_example(request: dict, reports: list[dict]) -> tuple[dict, int]:
    diagnostics = []
    truncated_count = 0
    for report in reports:
        message, truncated = training_message(report["message"])
        truncated_count += int(truncated)
        diagnostics.append(
            {
                "file": report["file"],
                "line": report["line"],
                "column": report["column"],
                "message": message,
            }
        )
    answer = {"diagnostics": diagnostics}
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            },
            {
                "role": "assistant",
                "content": json.dumps(answer, ensure_ascii=False, separators=(",", ":")),
            },
        ]
    }, truncated_count


def build_dataset(errors_root: Path, output: Path) -> tuple[int, int, int, int, int]:
    paths = sorted(errors_root.glob("*.json"), key=lambda path: path.name.casefold())
    if not paths:
        raise ValueError(f"No parsed error JSON files found in {errors_root}")
    reports = [load_report(path) for path in paths]
    grouped: dict[str, tuple[dict, list[dict]]] = {}
    for report in reports:
        request = request_for(report)
        key = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in grouped:
            grouped[key] = (request, [])
        grouped[key][1].append(report)
    temporary = output.with_suffix(output.suffix + ".tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    truncated_count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for request, request_reports in grouped.values():
            example, truncated = training_example(request, request_reports)
            truncated_count += truncated
            handle.write(
                json.dumps(example, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
    temporary.replace(output)
    pair_count = len({report["pair_id"] for report in reports})
    repo_count = len({report["repo"] for report in reports})
    return len(grouped), len(reports), pair_count, repo_count, truncated_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--errors-root", type=Path, default=DEFAULT_ERRORS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    examples, diagnostics, pairs, repos, truncated = build_dataset(
        args.errors_root.resolve(), args.output.resolve()
    )
    print(
        f"Wrote {examples} examples containing {diagnostics} diagnostics from "
        f"{pairs} pairs and {repos} repositories "
        f"to {args.output.resolve()} ({truncated} oversized diagnostics truncated)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

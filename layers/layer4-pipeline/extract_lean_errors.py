#!/usr/bin/env python3
"""Extract structured Lean/Lake error diagnostics from a verbose build log."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import posixpath
import re
from typing import Callable


SEVERITY = r"error|warning|information|info|fatal"
LOCATION = r"(?P<file>.+?):(?P<line>\d+):(?P<column>\d+)(?:-\d+:\d+)?:\s*"
LOCATION_DIAGNOSTIC = re.compile(
    rf"^{LOCATION}(?P<severity>{SEVERITY})(?:\([^)]*\))?:\s*(?P<message>.*)$",
    re.IGNORECASE,
)
LAKE_DIAGNOSTIC = re.compile(
    rf"^(?P<severity>{SEVERITY})(?:\([^)]*\))?:\s*{LOCATION}(?P<message>.*)$",
    re.IGNORECASE,
)
BARE_ERROR = re.compile(r"^(?:error|fatal)(?:\([^)]*\))?:\s*(?P<message>.*)$", re.IGNORECASE)
# Lake can color either the prefix or individual terms, and terminals can wrap
# filenames in OSC hyperlinks. Neither belongs to a diagnostic's source/message.
ANSI_ESCAPE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
LOG_BOUNDARY = re.compile(
    r"^(?:"
    r"(?:trace|info|warning|debug):"
    r"|[^\w\s]*\s*\[(?:\d+|\?)/(?:\d+|\?)\]"
    r"|(?:\[\d+/\d+\]\s*)?(?:Building|Compiling|Linking|Computing build jobs)\b"
    r"|Some (?:required )?targets (?:logged failures|failed):"
    r"|Build completed(?: successfully)?"
    r"|# (?:started|cwd|command|finished|exit_code|timeout|toolchain|toolchain_after|root_olean_count):"
    r")",
    re.IGNORECASE,
)
JOB_HEADER = re.compile(r"^[^\w\s]*\s*\[(?:\d+|\?)/(?:\d+|\?)\]")
OPTIONAL_CACHE_JOB = re.compile(r"\(Optional\) Fetching \S+:(?:optBarrel|optRelease)\b")


def required_build_output(text: str) -> str:
    """Exclude Lake's explicitly optional binary-cache jobs, not source jobs.

    A missing binary release is routinely followed by compilation from source.
    Its download errors are not compiler diagnostics or a required-job failure.
    The original log is retained unchanged on disk.
    """
    output = []
    optional_cache = False
    for line in ANSI_ESCAPE.sub('', text).splitlines():
        if JOB_HEADER.match(line):
            optional_cache = bool(OPTIONAL_CACHE_JOB.search(line))
        elif line.startswith(('# ', 'Build completed', 'Some required targets', 'Some targets')):
            optional_cache = False
        if optional_cache:
            continue
        if re.match(r"^(?:trace|warning): building from source; failed to fetch (?:Reservoir build|GitHub release)\b", line):
            continue
        output.append(line)
    return '\n'.join(output)


def _normalized_filename(raw_file: str) -> str:
    return posixpath.normpath(raw_file.strip().strip('"').replace("\\", "/"))


def _json_diagnostic(line: str) -> dict | None:
    """Read Lean's SerialMessage JSON, as emitted by `lean --json`."""
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    position = value.get("pos")
    if (
        not isinstance(value.get("severity"), str)
        or value["severity"] not in {"error", "warning", "information"}
        or not isinstance(value.get("fileName"), str)
        or not isinstance(value.get("data"), str)
        or not isinstance(position, dict)
        or type(position.get("line")) is not int
        or type(position.get("column")) is not int
    ):
        return None
    caption = value.get("caption")
    message = value["data"]
    if isinstance(caption, str) and caption:
        message = f"{caption}:\n{message}"
    return {
        "severity": "information" if value.get("isSilent") else value["severity"],
        "file": value["fileName"],
        "line": position["line"],
        "column": position["column"],
        "message": message,
    }


def _finish(current: dict | None, output: list[dict]) -> None:
    if current is None:
        return
    continuation = current.pop("_continuation", [])
    details = "\n".join(continuation).rstrip()
    if details:
        current["details"] = details
    output.append(current)


def parse_lean_errors(text: str) -> list[dict]:
    errors: list[dict] = []
    current: dict | None = None
    in_captured_output = False
    in_information = False
    for raw_line in required_build_output(text).splitlines():
        line = raw_line.rstrip()
        line = re.sub(r'^(?:error:\s*){2,}', 'error: ', line, flags=re.IGNORECASE)
        # Lake puts severity first; direct Lean output puts it after the location.
        # Test Lake first so a doubled prefix cannot become part of the filename.
        match = LAKE_DIAGNOSTIC.match(line) or LOCATION_DIAGNOSTIC.match(line)
        diagnostic = match.groupdict() if match else _json_diagnostic(line)
        if diagnostic is not None:
            # Linter information may quote pseudo-file parse failures on
            # continuation lines (e.g. linter.style.header:12:3: error: ...).
            if (in_information and diagnostic['severity'].lower() in {'error', 'fatal'}
                    and not diagnostic['file'].lower().endswith('.lean')
                    and not LAKE_DIAGNOSTIC.match(line)):
                continue
            _finish(current, errors)
            current = None
            if diagnostic["severity"].lower() not in {"error", "fatal"}:
                in_information = True
                continue
            in_information = False
            filename = _normalized_filename(diagnostic["file"])
            message = diagnostic["message"]
            # Lake sometimes wraps a complete Lean diagnostic, producing
            # `error: File.lean:1:0: error: ...`. Strip only this nested prefix.
            if match is not None and LAKE_DIAGNOSTIC.match(line):
                message = re.sub(r"^(?:error|fatal)(?:\([^)]*\))?:\s*", "", message)
            message_lines = message.strip().splitlines() or [""]
            current = {
                "kind": "lean-diagnostic" if filename.lower().endswith(".lean") else "process-error",
                "file": filename,
                "line": int(diagnostic["line"]),
                "column": int(diagnostic["column"]),
                "message": message_lines[0],
                "_continuation": message_lines[1:],
            }
            # SerialMessage JSON is a complete message, never a multiline
            # text header. Program output on the next line is unrelated.
            if match is None:
                _finish(current, errors)
                current = None
            continue
        # Some Lake build scripts quote compiler output in stdout/stderr fences.
        # Only these wrapper fences end errors: ordinary Markdown in an error's
        # explanatory text must remain intact.
        if line in {"```stdout", "```stderr"} or (in_captured_output and line == "```"):
            in_captured_output = line != "```"
            _finish(current, errors)
            current = None
            continue
        if LOG_BOUNDARY.match(line):
            _finish(current, errors)
            current = None
            in_information = bool(re.match(r'^(?:info|warning):', line))
            continue
        match = BARE_ERROR.match(line)
        if match:
            _finish(current, errors)
            current = {
                "kind": "process-error",
                "message": match.group("message").strip(),
                "_continuation": [],
            }
            continue
        if current is not None:
            current["_continuation"].append(line)
    _finish(current, errors)

    # Preserve order while suppressing exact duplicate diagnostics.
    unique: list[dict] = []
    seen: set[str] = set()
    for error in errors:
        key = json.dumps(error, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            unique.append(error)
    return unique


def parse_log(path: Path) -> list[dict]:
    return parse_lean_errors(path.read_text(encoding="utf-8", errors="replace"))


def _relative_source_path(raw_file: str, source_root: Path) -> tuple[str, Path | None]:
    """Return a portable display path and a safe source path within source_root."""
    root = source_root.resolve()
    raw_path = Path(raw_file)
    if raw_path.is_absolute():
        try:
            relative = raw_path.resolve(strict=False).relative_to(root)
        except ValueError:
            return raw_file.replace("\\", "/"), None
    else:
        relative = Path(raw_file.replace("\\", "/"))
        candidate = (root / relative).resolve(strict=False)
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return raw_file.replace("\\", "/"), None
    return relative.as_posix(), root / relative


def enrich_lean_error(
    error: dict,
    source_root: Path,
    *,
    context_lines: int = 4,
    source_fallback: Callable[[str], str | None] | None = None,
) -> dict:
    """Add the source line and surrounding context to one Lean diagnostic."""
    display_file, source_path = _relative_source_path(str(error["file"]), source_root)
    source_text: str | None = None
    if source_path is not None and source_path.is_file():
        source_text = source_path.read_text(encoding="utf-8", errors="replace")
    elif source_fallback is not None and source_path is not None:
        source_text = source_fallback(display_file)

    message_parts = [str(error.get("message", "")).strip()]
    details = str(error.get("details", "")).strip("\n")
    if details:
        message_parts.append(details)
    message = "\n".join(part for part in message_parts if part)
    line_number = int(error["line"])
    column_number = int(error["column"])
    lines = source_text.splitlines() if source_text is not None else []
    source_found = 1 <= line_number <= len(lines)
    if source_found:
        start = max(1, line_number - context_lines)
        end = min(len(lines), line_number + context_lines)
        error_line: str | None = lines[line_number - 1]
        code: str | None = "\n".join(lines[start - 1 : end])
        context_start: int | None = start
        context_end: int | None = end
    else:
        error_line = None
        code = None
        context_start = None
        context_end = None

    return {
        "file": display_file,
        "line": line_number,
        "column": column_number,
        "message": message,
        "source_found": source_found,
        "error_line": error_line,
        "code": code,
        "context_start_line": context_start,
        "context_end_line": context_end,
    }


def write_report_atomic(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--old-hash")
    parser.add_argument("--new-hash")
    parser.add_argument(
        "--source-root",
        type=Path,
        help="adapted source tree used to attach source lines and context",
    )
    args = parser.parse_args()
    errors = [error for error in parse_log(args.log) if error.get("kind") == "lean-diagnostic"]
    source_root = (args.source_root or args.log.parent).resolve()
    output_paths: list[Path] = []
    for index, error in enumerate(errors, 1):
        report = enrich_lean_error(error, source_root)
        report.update(
            {
                "repo": args.repo,
                "old_version": args.old_version,
                "new_version": args.new_version,
                "old_hash": args.old_hash,
                "new_hash": args.new_hash,
            }
        )
        output = (
            args.output
            if len(errors) == 1
            else args.output.with_name(
                f"{args.output.stem}__error-{index:04d}{args.output.suffix or '.json'}"
            )
        )
        write_report_atomic(output, report)
        output_paths.append(output)
    print(f"{args.log}: extracted {len(output_paths)} Lean error(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Interpret build evidence conservatively before exporting research results."""

from __future__ import annotations

from pathlib import Path
import re
from extract_lean_errors import required_build_output


RESULT_FORMAT_VERSION = 5
# Adapted-build / native outcomes recorded by the pipeline without running the
# build: Lake < v4.16 with an out-of-date ProofWidgets rebuilds all of Mathlib
# from source, which exceeds the time limit.  Such results are inconclusive and
# are requeued by --retry-build-inconclusive / --retry-native-failures.
PREDICTED_TIMEOUT_OUTCOME = "predicted-build-timeout"
INCONCLUSIVE_OUTCOMES = (
    "adapted-build-incomplete",
    "wrong-toolchain",
    "unverified-toolchain",
    "no-build-targets",
    "adapted-build-infrastructure-failure",
    "diagnostics-with-success-exit",
    "build-failed-without-lean-errors",
    "process-errors-with-success-exit",
    "unverified-build-targets",
    PREDICTED_TIMEOUT_OUTCOME,
)
INTERRUPTED_EXIT_CODES = {124, 130, 137, 143, 3221225786, -1073741510}
EXIT_CODE = re.compile(r"^# exit_code: (-?\d+)\s*$", re.MULTILINE)
CWD = re.compile(r"^# cwd: (.+)$", re.MULTILINE)
NO_TARGETS = re.compile(
    r"^Build completed successfully \(0 jobs\)|^error:.*no (?:default |build )?targets\b",
    re.IGNORECASE | re.MULTILINE,
)
TOOLCHAIN_UPDATE = re.compile(
    r"(?:updating|switching|using) toolchain(?: to)?\s*['\"]([^'\"\r\n]+)",
    re.IGNORECASE,
)
ADAPTED_INFRASTRUCTURE = (
    "pipeline interrupted",
    "command timed out",
    "filename too long",
    "path too long",
    "failed to create file",
    "failed to write '",
    "failed to open output file",
    "permission denied",
    "access is denied",
    "no space left on device",
    "out of memory",
    "external command 'git' exited",
    "external command `git` exited",
    "external command \"git\" exited",
    "rpc failed",
    "early eof",
    "failed to fetch",
    "failed to download",
    "unable to access",
    "could not resolve host",
    "connection timed out",
    "toolchain download",
)


def log_exit_code(path: Path) -> int | None:
    if not path.is_file():
        return None
    matches = EXIT_CODE.findall(path.read_text(encoding="utf-8", errors="replace"))
    return int(matches[-1]) if matches else None


def log_source_root(path: Path, fallback: Path) -> Path:
    if path.is_file():
        match = CWD.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            return Path(match.group(1).strip())
    return fallback


def empty_build(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return bool(NO_TARGETS.search(text))


def has_build_evidence(path: Path) -> bool:
    """Require positive evidence, not merely the absence of '0 jobs'.

    New runs record root-module artifacts. Historical verbose logs can prove
    a module was compiled/replayed; a bare success banner cannot do so.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding='utf-8', errors='replace')
    counts = re.findall(r'^# root_olean_count: (\d+)\s*$', text, re.MULTILINE)
    if counts:
        return int(counts[-1]) > 0
    return bool(re.search(
        r'(?im)^(?:trace: .*[/\\]lean(?:\.exe)? .*\.lean\b'
        r'|.*\[\d+/\d+\]\s*(?:Built|Replayed|Building|Compiling)\s+[^\r\n]+'
        r'|\s*(?:Building|Compiling)\s+\S+\.lean\b)', text
    ))


def immutable_toolchain(version: str) -> bool:
    """Moving aliases cannot describe a reproducible historical environment."""
    tag = version.strip().rsplit(':', 1)[-1]
    return tag.lower() not in {'stable', 'nightly', 'latest', 'master', 'main', ''}


def toolchain_updates(path: Path) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [match.strip() for match in TOOLCHAIN_UPDATE.findall(text)]


def toolchain_mismatch(path: Path, expected: str) -> bool:
    expected = expected.strip()
    if any(value != expected for value in toolchain_updates(path)):
        return True
    if not path.is_file():
        return False
    text = path.read_text(encoding='utf-8', errors='replace')
    recorded = re.findall(r'^# toolchain(?:_after)?: (.*)$', text, re.MULTILINE)
    return any(value.strip() != expected for value in recorded)


def has_toolchain_evidence(path: Path, expected: str) -> bool:
    if not path.is_file() or toolchain_mismatch(path, expected):
        return False
    text = path.read_text(encoding='utf-8', errors='replace')
    if f'# toolchain: {expected}\n' in text and f'# toolchain_after: {expected}\n' in text:
        return True
    # Elan encodes installed release names in compiler executable paths.
    encoded = expected.replace('/', '--').replace(':', '---')
    if ':nightly-' in expected and '/lean4:' in expected:
        encoded = expected.replace('/lean4:', '/lean4-nightly:').replace('/', '--').replace(':', '---')
    return bool(re.search(r'toolchains[/\\]' + re.escape(encoded) + r'[/\\]bin[/\\]lean(?:\.exe)?\b', text, re.I))


def infrastructure_failure(path: Path) -> bool:
    if not path.is_file():
        return False
    text = required_build_output(path.read_text(encoding="utf-8", errors="replace")).casefold()
    return any(pattern in text for pattern in ADAPTED_INFRASTRUCTURE)


def classify_adapted_build(
    exit_code: int | None,
    errors: list[dict],
    build_log: Path,
    *,
    infrastructure_failure: bool = False,
    expected_toolchain: str | None = None,
    cache_log: Path | None = None,
) -> tuple[str, str]:
    """A no-error result requires a finished, successful, nonempty build."""
    diagnostics = [item for item in errors if item.get("kind") == "lean-diagnostic"]
    if exit_code is None or exit_code in INTERRUPTED_EXIT_CODES:
        return "build_inconclusive", "adapted-build-incomplete"
    if expected_toolchain is not None:
        if toolchain_mismatch(build_log, expected_toolchain) or (
            cache_log is not None and toolchain_mismatch(cache_log, expected_toolchain)
        ):
            return 'build_inconclusive', 'wrong-toolchain'
        if not has_toolchain_evidence(build_log, expected_toolchain):
            return 'build_inconclusive', 'unverified-toolchain'
    if empty_build(build_log):
        return "build_inconclusive", "no-build-targets"
    if infrastructure_failure:
        return "build_inconclusive", "adapted-build-infrastructure-failure"
    if diagnostics:
        if exit_code == 0:
            return "build_inconclusive", "diagnostics-with-success-exit"
        return "completed", "build-errors"
    if exit_code != 0:
        return "build_inconclusive", "build-failed-without-lean-errors"
    if any(item.get("kind") == "process-error" for item in errors):
        return "build_inconclusive", "process-errors-with-success-exit"
    if not has_build_evidence(build_log):
        return 'build_inconclusive', 'unverified-build-targets'
    return "completed", "build-succeeded"

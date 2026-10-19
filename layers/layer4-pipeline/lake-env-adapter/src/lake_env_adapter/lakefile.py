from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .errors import ParseError
from .models import DirectDependency


_STRING = r'"(?:\\.|[^"\\])*"'
_NAME = rf'(?:{_STRING}|«[^»]+»|[A-Za-z_][A-Za-z0-9_\'.-]*)'


def find_lakefile(project: Path) -> Path:
    for name in ("lakefile.lean", "lakefile.toml"):
        candidate = project / name
        if candidate.is_file():
            return candidate
    raise ParseError(f"no lakefile.lean or lakefile.toml found in {project}")


def read_toolchain(project: Path) -> str:
    path = project / "lean-toolchain"
    try:
        toolchain = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ParseError(f"toolchain file not found: {path}") from exc
    if not toolchain or "\n" in toolchain or "\r" in toolchain:
        raise ParseError(f"expected exactly one non-empty toolchain in {path}")
    return toolchain


def parse_lakefile(path: Path) -> list[DirectDependency]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".toml":
        return _parse_toml(text, path)
    if path.suffix == ".lean":
        return _parse_lean(text, path)
    raise ParseError(f"unsupported Lake configuration: {path}")


def update_explicit_revisions(
    path: Path, revisions: dict[int, str]
) -> str:
    """Return an edited lakefile, changing only declarations with explicit revs."""
    text = path.read_text(encoding="utf-8")
    if not revisions:
        return text
    if path.suffix == ".toml":
        return _update_toml_revisions(text, revisions, path)
    if path.suffix == ".lean":
        return _update_lean_revisions(text, revisions, path)
    raise ParseError(f"unsupported Lake configuration: {path}")


def _parse_toml(text: str, path: Path) -> list[DirectDependency]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ParseError(f"invalid TOML in {path}: {exc}") from exc
    requires = data.get("require", [])
    if not isinstance(requires, list):
        raise ParseError(f"require must be an array of tables in {path}")
    result: list[DirectDependency] = []
    for index, item in enumerate(requires):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ParseError(f"require block {index + 1} in {path} has no string name")
        name = item["name"]
        scope = item.get("scope", "")
        if not isinstance(scope, str):
            raise ParseError(f"require {name!r} in {path} has a non-string scope")
        rev = item.get("rev")
        version = item.get("version")
        if rev is not None and not isinstance(rev, str):
            raise ParseError(f"require {name!r} in {path} has a non-string rev")
        if version is not None and not isinstance(version, str):
            raise ParseError(f"require {name!r} in {path} has a non-string version")
        subdir = item.get("subDir")
        if subdir is not None and not isinstance(subdir, str):
            raise ParseError(f"require {name!r} in {path} has a non-string subDir")
        if "path" in item:
            source_type = "path"
            url = None
        elif "git" in item:
            source_type = "git"
            git = item["git"]
            if isinstance(git, str):
                url = git
            elif isinstance(git, dict) and isinstance(git.get("url"), str):
                url = git["url"]
            else:
                raise ParseError(f"require {name!r} in {path} has an unsupported git value")
        else:
            source_type = "reservoir"
            url = None
        result.append(
            DirectDependency(
                name=name,
                scope=scope,
                source_type=source_type,
                url=url,
                subdir=subdir,
                declared_rev=rev,
                declared_version=version,
                explicit_rev=rev is not None,
                declaration_index=index,
            )
        )
    _check_duplicates(result, path)
    return result


def _parse_lean(text: str, path: Path) -> list[DirectDependency]:
    statements = _lean_require_statements(text)
    masked = _mask_lean_comments(text)
    result: list[DirectDependency] = []
    for index, (start, _, statement) in enumerate(statements):
        dep = _parse_lean_statement(statement, index, path)
        if _lean_statement_is_conditional(masked, start):
            dep = replace(dep, conditional=True)
        result.append(dep)
    _check_duplicates(result, path)
    return result


_CONDITIONAL_GUARD = re.compile(r"(?:^|\s)(?:meta\s+)?if\b.*\bthen\s*$", re.DOTALL)


def _lean_statement_is_conditional(masked: str, start: int) -> bool:
    """True when the ``require`` at ``start`` follows a ``[meta] if ... then`` guard.

    Lake's DSL allows ``meta if get_config? env = some "dev" then`` on the line
    before a ``require``; such a dependency is a root dependency only when that
    configuration is given on the command line.
    """
    preceding = masked[:start].rstrip()
    if not preceding:
        return False
    last_line = preceding.rsplit("\n", 1)[-1].strip()
    return bool(_CONDITIONAL_GUARD.search(last_line))


def _parse_lean_statement(statement: str, index: int, path: Path) -> DirectDependency:
    clean = _mask_lean_comments(statement)
    header = re.search(
        rf"\brequire\s+(?:(?P<scope>{_NAME})\s*/\s*)?(?P<name>{_NAME})",
        clean,
        re.DOTALL,
    )
    if not header:
        raise ParseError(f"cannot parse require declaration {index + 1} in {path}")
    scope = _decode_name(header.group("scope")) if header.group("scope") else ""
    name = _decode_name(header.group("name"))
    body = clean[header.end() :]

    git_match = re.search(rf"\bfrom\s+git\s+(?P<url>{_STRING})", body, re.DOTALL)
    path_match = re.search(rf"\bfrom\s+(?!git\b)(?P<path>{_STRING})", body, re.DOTALL)
    declared_rev = None
    declared_version = None
    explicit_rev = False
    url = None
    subdir = None

    if git_match:
        source_type = "git"
        url = _decode_string(git_match.group("url"), path)
        after_url = body[git_match.end() :]
        rev_match = re.search(
            rf"@\s*(?:git\s+)?(?P<rev>{_STRING})", after_url, re.DOTALL
        )
        if "@" in after_url and not rev_match:
            raise ParseError(
                f"require {name!r} in {path} uses a computed Git revision; "
                "only string-literal revisions can be changed safely"
            )
        if rev_match:
            declared_rev = _decode_string(rev_match.group("rev"), path)
            explicit_rev = True
            after_rev = after_url[rev_match.end() :]
        else:
            after_rev = after_url
        subdir_match = re.search(rf"/\s*(?P<subdir>{_STRING})", after_rev, re.DOTALL)
        if subdir_match:
            subdir = _decode_string(subdir_match.group("subdir"), path)
    elif path_match:
        source_type = "path"
    elif re.search(r"\bfrom\b", body):
        raise ParseError(
            f"require {name!r} in {path} uses a computed or unsupported source"
        )
    else:
        source_type = "reservoir"
        rev_match = re.search(rf"@\s*git\s+(?P<rev>{_STRING})", body, re.DOTALL)
        version_match = re.search(rf"@\s*(?!git\b)(?P<version>{_STRING})", body, re.DOTALL)
        if rev_match:
            declared_rev = _decode_string(rev_match.group("rev"), path)
            explicit_rev = True
        elif version_match:
            declared_version = _decode_string(version_match.group("version"), path)
        elif "@" in body:
            raise ParseError(
                f"require {name!r} in {path} uses a computed version/revision; "
                "only string literals can be analyzed safely"
            )

    return DirectDependency(
        name=name,
        scope=scope,
        source_type=source_type,
        url=url,
        subdir=subdir,
        declared_rev=declared_rev,
        declared_version=declared_version,
        explicit_rev=explicit_rev,
        declaration_index=index,
    )


def _lean_require_statements(text: str) -> list[tuple[int, int, str]]:
    masked = _mask_lean_comments(text)
    line_spans = list(re.finditer(r".*(?:\r?\n|\Z)", masked))
    result: list[tuple[int, int, str]] = []
    i = 0
    while i < len(line_spans):
        match = line_spans[i]
        line = match.group(0)
        start_match = re.match(r"^(?P<indent>[ \t]*)require\b", line)
        if not start_match:
            i += 1
            continue
        base_indent = _indent_width(start_match.group("indent"))
        start = match.start()
        end = match.end()
        j = i + 1
        while j < len(line_spans):
            next_line = line_spans[j].group(0)
            if not next_line.strip():
                end = line_spans[j].end()
                j += 1
                continue
            indent_text = re.match(r"^[ \t]*", next_line).group(0)  # type: ignore[union-attr]
            if _indent_width(indent_text) <= base_indent:
                break
            end = line_spans[j].end()
            j += 1
        result.append((start, end, text[start:end]))
        i = j
    return result


def _update_toml_revisions(text: str, revisions: dict[int, str], path: Path) -> str:
    block_pattern = r"(?m)^\s*\[\[require\]\]\s*(?:#.*)?$"
    block_starts = [match.start() for match in re.finditer(block_pattern, text)]
    if not block_starts and revisions:
        raise ParseError(f"no [[require]] blocks found in {path}")
    boundaries = block_starts + [len(text)]
    edits: list[tuple[int, int, str]] = []
    for index, revision in revisions.items():
        if index >= len(block_starts):
            raise ParseError(f"require declaration {index + 1} no longer exists in {path}")
        start, end = boundaries[index], boundaries[index + 1]
        block = text[start:end]
        matches = list(re.finditer(r"(?m)^(?P<prefix>\s*rev\s*=\s*)(?P<value>\"(?:\\.|[^\"\\])*\")", block))
        if len(matches) != 1:
            raise ParseError(
                f"require declaration {index + 1} in {path} does not contain exactly one rev"
            )
        match = matches[0]
        edits.append(
            (
                start + match.start("value"),
                start + match.end("value"),
                json.dumps(revision),
            )
        )
    return _apply_text_edits(text, edits)


def _update_lean_revisions(text: str, revisions: dict[int, str], path: Path) -> str:
    statements = _lean_require_statements(text)
    edits: list[tuple[int, int, str]] = []
    for index, revision in revisions.items():
        if index >= len(statements):
            raise ParseError(f"require declaration {index + 1} no longer exists in {path}")
        start, _, statement = statements[index]
        dep = _parse_lean_statement(statement, index, path)
        if not dep.explicit_rev:
            raise ParseError(f"require {dep.name!r} in {path} no longer has an explicit rev")
        masked = _mask_lean_comments(statement)
        header = re.search(
            rf"\brequire\s+(?:(?:{_NAME})\s*/\s*)?(?:{_NAME})", masked, re.DOTALL
        )
        assert header is not None
        body_offset = header.end()
        body = masked[body_offset:]
        git_match = re.search(rf"\bfrom\s+git\s+(?P<url>{_STRING})", body, re.DOTALL)
        if git_match:
            tail_offset = body_offset + git_match.end()
            tail = masked[tail_offset:]
            rev_match = re.search(rf"@\s*(?:git\s+)?(?P<rev>{_STRING})", tail, re.DOTALL)
            offset = tail_offset
        else:
            rev_match = re.search(rf"@\s*git\s+(?P<rev>{_STRING})", body, re.DOTALL)
            offset = body_offset
        if not rev_match:
            raise ParseError(f"could not locate explicit rev for {dep.name!r} in {path}")
        edits.append(
            (
                start + offset + rev_match.start("rev"),
                start + offset + rev_match.end("rev"),
                json.dumps(revision),
            )
        )
    return _apply_text_edits(text, edits)


def _apply_text_edits(text: str, edits: Iterable[tuple[int, int, str]]) -> str:
    result = text
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _mask_lean_comments(text: str) -> str:
    chars = list(text)
    i = 0
    depth = 0
    in_string = False
    escaped = False
    line_comment = False
    while i < len(text):
        if line_comment:
            if text[i] in "\r\n":
                line_comment = False
            else:
                chars[i] = " "
            i += 1
            continue
        if depth:
            if text.startswith("/-", i):
                chars[i] = chars[i + 1] = " "
                depth += 1
                i += 2
            elif text.startswith("-/", i):
                chars[i] = chars[i + 1] = " "
                depth -= 1
                i += 2
            else:
                if text[i] not in "\r\n":
                    chars[i] = " "
                i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif text[i] == "\\":
                escaped = True
            elif text[i] == '"':
                in_string = False
            i += 1
            continue
        if text.startswith("--", i):
            chars[i] = chars[i + 1] = " "
            line_comment = True
            i += 2
        elif text.startswith("/-", i):
            chars[i] = chars[i + 1] = " "
            depth = 1
            i += 2
        elif text[i] == '"':
            in_string = True
            i += 1
        else:
            i += 1
    if depth:
        raise ParseError("unterminated block comment in lakefile.lean")
    if in_string:
        raise ParseError("unterminated string in lakefile.lean")
    return "".join(chars)


def _decode_name(value: str) -> str:
    if value.startswith('"'):
        return _decode_string(value, None)
    if value.startswith("«") and value.endswith("»"):
        return value[1:-1]
    return value


def _decode_string(value: str, path: Path | None) -> str:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        location = f" in {path}" if path else ""
        raise ParseError(f"unsupported Lean string literal{location}: {value}") from exc
    if not isinstance(decoded, str):
        raise ParseError(f"expected a string literal, got {value}")
    return decoded


def _indent_width(value: str) -> int:
    return sum(4 if char == "\t" else 1 for char in value)


def _check_duplicates(dependencies: list[DirectDependency], path: Path) -> None:
    seen: set[tuple[str, str]] = set()
    for dep in dependencies:
        key = (dep.scope.casefold(), dep.name.casefold())
        if key in seen:
            raise ParseError(f"duplicate direct dependency {dep.display_name} in {path}")
        seen.add(key)

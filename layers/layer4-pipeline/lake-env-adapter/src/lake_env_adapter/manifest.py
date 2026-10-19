from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable

from .errors import ManifestError


def load_manifest(project: Path) -> dict[str, Any]:
    path = project / "lake-manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("packages"), list):
        raise ManifestError(f"{path} does not contain a packages array")
    return value


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    _atomic_write(path, text)


def package_payload(package: dict[str, Any]) -> dict[str, Any]:
    """Return the field-bearing object for flat and legacy Lake entries."""
    if "name" in package:
        return package
    for kind in ("git", "path"):
        value = package.get(kind)
        if isinstance(value, dict):
            return value
    return package


def package_name(package: dict[str, Any]) -> str | None:
    value = package_payload(package).get("name")
    return value if isinstance(value, str) else None


def package_scope(package: dict[str, Any]) -> str:
    value = package_payload(package).get("scope", "")
    return value if isinstance(value, str) else ""


def identifier_key(value: str) -> str:
    """Normalize Lake's JSON rendering of quoted Lean identifiers.

    Lake may serialize a dependency declared as ``lean-slides`` as
    ``«lean-slides»``.  Both spellings identify the same package.
    """
    value = value.strip()
    if len(value) >= 2 and value.startswith("«") and value.endswith("»"):
        value = value[1:-1]
    return value.casefold()


def package_revision(package: dict[str, Any]) -> str | None:
    value = package_payload(package).get("rev")
    return value if isinstance(value, str) else None


def package_input_revision(package: dict[str, Any]) -> str | None:
    payload = package_payload(package)
    for key in ("inputRev", "inputRev?"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def package_url(package: dict[str, Any]) -> str | None:
    payload = package_payload(package)
    for key in ("url", "gitUrl"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def package_subdir(package: dict[str, Any]) -> str | None:
    payload = package_payload(package)
    for key in ("subDir", "subDir?"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def package_inherited(package: dict[str, Any]) -> bool:
    return package_payload(package).get("inherited") is True


def manifest_version(manifest: dict[str, Any]) -> tuple[int, int, int]:
    """Return Lake's manifest version as a comparable triple.

    Lake stores the version as a natural number up to ``7`` and as a string such
    as ``"1.1.0"`` from Lean v4.9.0 on; Lake itself treats the integer ``n`` as
    ``0.n.0`` (see ``Manifest.fromJson?`` in ``src/lake/Lake/Load/Manifest.lean``).
    """
    value = manifest.get("version")
    if isinstance(value, bool):
        raise ManifestError(f"unsupported manifest version {value!r}")
    if isinstance(value, int):
        return (0, value, 0)
    if isinstance(value, str):
        parts = value.strip().split(".")
        if parts and all(part.isdigit() for part in parts) and len(parts) <= 3:
            numbers = [int(part) for part in parts] + [0, 0]
            return (numbers[0], numbers[1], numbers[2])
    raise ManifestError(f"unsupported manifest version {value!r}")


# Manifest version 7 (Lean v4.3.0) replaced the nested ``{"git": {...}}`` records
# by flat records carrying a ``type`` field; ``1.1.0`` (Lean v4.10.0) added ``scope``.
FLAT_PACKAGE_VERSION = (0, 7, 0)
SCOPED_PACKAGE_VERSION = (1, 1, 0)


def convert_package(package: dict[str, Any], version: tuple[int, int, int]) -> dict[str, Any]:
    """Return a copy of ``package`` in the record layout of manifest ``version``.

    Records already in the requested layout are copied unchanged.  Lake rejects a
    manifest whose records do not match its ``version`` (``no inductive constructor
    matched`` or ``unknown package entry type``), so every record written to a
    manifest must use that manifest's layout.
    """
    flat = version >= FLAT_PACKAGE_VERSION
    payload = package_payload(package)
    if flat:
        if "name" in package:
            result = copy.deepcopy(package)
            if version >= SCOPED_PACKAGE_VERSION:
                result.setdefault("scope", "")
            return result
        kind = package_type(package)
        result = {"type": kind, "name": payload.get("name")}
        if version >= SCOPED_PACKAGE_VERSION:
            result["scope"] = payload.get("scope", "")
        result["inherited"] = payload.get("inherited") is True
        if kind == "git":
            result["url"] = package_url(package)
            result["rev"] = payload.get("rev")
            result["inputRev"] = package_input_revision(package)
            result["subDir"] = package_subdir(package)
        elif kind == "path":
            result["dir"] = payload.get("dir")
        else:
            raise ManifestError(f"cannot convert package record of unknown type: {package!r}")
        return result
    if "name" not in package:
        return copy.deepcopy(package)
    kind = package_type(package)
    nested: dict[str, Any] = {
        "name": payload.get("name"),
        "opts": {},
        "inherited": payload.get("inherited") is True,
    }
    if kind == "git":
        nested.update(
            {
                "url": package_url(package),
                "rev": payload.get("rev"),
                "inputRev?": package_input_revision(package),
                "subDir?": package_subdir(package),
            }
        )
    elif kind == "path":
        nested["dir"] = payload.get("dir")
    else:
        raise ManifestError(f"cannot convert package record of unknown type: {package!r}")
    return {kind: nested}


def compose_target_manifest(
    base: dict[str, Any],
    replacements: Iterable[tuple[str, str, dict[str, Any]]],
    template: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the manifest handed to the target toolchain's Lake.

    ``base`` is the target's current (old) manifest and ``replacements`` are the
    selected direct records.  When ``template`` (the new project's manifest) is
    known, the result takes the template's header (``version``, ``name``,
    ``lakeDir``, ``packagesDir``) because the target toolchain is the new
    project's toolchain and its Lake only reads that layout; every direct record
    is converted to it, and the template's transitive (``inherited``) records are
    carried over.  Lake never reuses inherited records during ``lake update``,
    but their presence lets ``lake update <transitive>`` leave the selected
    direct records untouched on every Lake version (see ``apply.py``).
    """
    merged = replace_packages(base, replacements)
    if template is None:
        return merged
    version = manifest_version(template)
    direct = [
        convert_package(package, version)
        for package in merged["packages"]
        if isinstance(package, dict) and not package_inherited(package)
    ]
    direct_keys = {identifier_key(package_name(package) or "") for package in direct}
    inherited = [
        copy.deepcopy(package)
        for package in template["packages"]
        if isinstance(package, dict)
        and package_inherited(package)
        and identifier_key(package_name(package) or "") not in direct_keys
    ]
    result = {key: copy.deepcopy(value) for key, value in template.items() if key != "packages"}
    result["packages"] = direct + inherited
    return result


def package_type(package: dict[str, Any]) -> str:
    value = package.get("type")
    if isinstance(value, str):
        return value
    if "git" in package:
        return "git"
    if "path" in package:
        return "path"
    return "unknown"


def with_revision(package: dict[str, Any], revision: str) -> dict[str, Any]:
    """Copy a Git package entry and pin both requested and resolved revisions."""
    result = copy.deepcopy(package)
    payload = package_payload(result)
    payload["rev"] = revision
    if "inputRev?" in payload:
        payload["inputRev?"] = revision
    else:
        payload["inputRev"] = revision
    return result


def with_git_source(
    package: dict[str, Any], url: str | None, subdir: str | None
) -> dict[str, Any]:
    result = copy.deepcopy(package)
    payload = package_payload(result)
    if url:
        if "gitUrl" in payload:
            payload["gitUrl"] = url
        else:
            payload["url"] = url
    if subdir is not None:
        if "subDir?" in payload:
            payload["subDir?"] = subdir
        else:
            payload["subDir"] = subdir
    return result


def as_direct(package: dict[str, Any]) -> dict[str, Any]:
    """Copy a package entry and mark it as a root package's direct dependency."""
    result = copy.deepcopy(package)
    package_payload(result)["inherited"] = False
    return result


def find_package(
    manifest: dict[str, Any], name: str, scope: str = ""
) -> dict[str, Any] | None:
    candidates = [
        package
        for package in manifest["packages"]
        if isinstance(package, dict)
        and identifier_key(package_name(package) or "") == identifier_key(name)
    ]
    if scope:
        scoped = [
            package
            for package in candidates
            if identifier_key(package_scope(package)) == identifier_key(scope)
        ]
        if len(scoped) == 1:
            return scoped[0]
        if len(scoped) > 1:
            raise ManifestError(f"multiple manifest entries match @{scope}/{name}")
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        label = f"@{scope}/{name}" if scope else name
        raise ManifestError(f"multiple manifest entries match {label}; add a scope")
    return None


def replace_packages(
    manifest: dict[str, Any], replacements: Iterable[tuple[str, str, dict[str, Any]]]
) -> dict[str, Any]:
    result = copy.deepcopy(manifest)
    packages = result["packages"]
    for name, scope, replacement in replacements:
        indexes = [
            i
            for i, package in enumerate(packages)
            if isinstance(package, dict)
            and identifier_key(package_name(package) or "") == identifier_key(name)
            and (not scope or identifier_key(package_scope(package)) == identifier_key(scope))
        ]
        if not indexes and scope:
            indexes = [
                i
                for i, package in enumerate(packages)
                if isinstance(package, dict)
                and identifier_key(package_name(package) or "") == identifier_key(name)
            ]
        if len(indexes) > 1:
            raise ManifestError(f"cannot replace ambiguous package {name}")
        if indexes:
            packages[indexes[0]] = copy.deepcopy(replacement)
        else:
            packages.append(copy.deepcopy(replacement))
    return result


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.lakeva.tmp")
    temporary.write_text(content, encoding="utf-8", newline="")
    temporary.replace(path)

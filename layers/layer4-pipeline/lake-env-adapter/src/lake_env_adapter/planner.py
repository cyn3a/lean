from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ManifestError, ResolutionError
from .git_history import GitHistoryResolver
from .lakefile import find_lakefile, parse_lakefile, read_toolchain
from .manifest import (
    as_direct,
    find_package,
    load_manifest,
    package_revision,
    package_scope,
    package_subdir,
    package_type,
    package_url,
    with_git_source,
    with_revision,
)
from .models import Decision, DirectDependency, Plan
from .reservoir import ReservoirClient


class Planner:
    def __init__(
        self,
        reservoir: ReservoirClient | None = None,
        history: GitHistoryResolver | None = None,
    ) -> None:
        self.reservoir = reservoir or ReservoirClient()
        self.history = history or GitHistoryResolver()

    def build(
        self,
        old_project: Path,
        new_project: Path,
        target_project: Path,
        *,
        target_toolchain: str | None = None,
    ) -> Plan:
        old_project = old_project.resolve()
        new_project = new_project.resolve()
        target_project = target_project.resolve()
        old_lakefile = find_lakefile(old_project)
        target_lakefile = find_lakefile(target_project)
        old_dependencies = parse_lakefile(old_lakefile)
        target_dependencies = parse_lakefile(target_lakefile)
        target_by_key = {_dep_key(dep): dep for dep in target_dependencies}
        old_keys = {_dep_key(dep) for dep in old_dependencies}
        if old_keys != set(target_by_key):
            missing = sorted(_display_key(key) for key in old_keys - set(target_by_key))
            extra = sorted(_display_key(key) for key in set(target_by_key) - old_keys)
            details = []
            if missing:
                details.append(f"missing from target: {', '.join(missing)}")
            if extra:
                details.append(f"extra in target: {', '.join(extra)}")
            raise ManifestError(
                "target direct dependencies differ from the old project (" + "; ".join(details) + ")"
            )

        old_manifest = load_manifest(old_project)
        new_manifest = load_manifest(new_project)
        load_manifest(target_project)  # Validate before doing network work.
        # The new project defines the destination Lean version.  The target is usually a
        # copy of the old checkout, so reading its toolchain would select old revisions.
        toolchain = target_toolchain or read_toolchain(new_project)
        decisions: list[Decision] = []
        for old_dep in old_dependencies:
            target_dep = target_by_key[_dep_key(old_dep)]
            dep = replace(
                old_dep,
                declaration_index=target_dep.declaration_index,
                explicit_rev=target_dep.explicit_rev,
            )
            decisions.append(
                self._resolve_dependency(dep, old_dep, old_manifest, new_manifest, toolchain)
            )

        return Plan(
            schema_version=1,
            created_at=datetime.now(timezone.utc).isoformat(),
            old_project=str(old_project),
            new_project=str(new_project),
            target_project=str(target_project),
            target_toolchain=toolchain,
            lakefile_name=target_lakefile.name,
            target_hashes={
                target_lakefile.name: sha256_file(target_lakefile),
                "lake-manifest.json": sha256_file(target_project / "lake-manifest.json"),
                "lean-toolchain": sha256_file(target_project / "lean-toolchain"),
            },
            decisions=decisions,
            manifest_template=copy.deepcopy(new_manifest),
        )

    def _resolve_dependency(
        self,
        dep: DirectDependency,
        source_dep: DirectDependency,
        old_manifest: dict[str, Any],
        new_manifest: dict[str, Any],
        target_toolchain: str,
    ) -> Decision:
        old_package = find_package(old_manifest, source_dep.name, source_dep.scope)
        new_package = find_package(new_manifest, source_dep.name, source_dep.scope)
        if new_package is not None:
            revision = package_revision(new_package)
            if package_type(new_package) == "git" and not revision:
                return Decision(
                    dep,
                    "unsupported",
                    "none",
                    reason="the matching new-manifest Git entry has no resolved rev",
                )
            return Decision(
                dep,
                "selected",
                "new-manifest",
                revision=revision,
                package=as_direct(new_package),
                evidence=["matching direct dependency found in the new lake-manifest"],
            )

        if source_dep.source_type == "path" or (
            old_package is not None and package_type(old_package) == "path"
        ):
            if old_package is None:
                return Decision(
                    dep,
                    "unresolved",
                    "none",
                    reason="local path dependency has no old manifest entry",
                )
            return Decision(
                dep,
                "local-preserved",
                "old-manifest",
                package=as_direct(old_package),
                evidence=["local path dependencies have no remotely selectable revision"],
            )

        if old_package is None:
            return Decision(
                dep,
                "unresolved",
                "none",
                reason="dependency is absent from both the old and new manifests",
            )

        evidence = ["dependency is absent from the new lake-manifest"]
        identities = _reservoir_identities(source_dep, old_package)
        repository_url = source_dep.url or package_url(old_package)
        subdir = source_dep.subdir or package_subdir(old_package)
        reservoir_errors: list[str] = []

        for owner, name in identities:
            try:
                selection = self.reservoir.select_exact_build(owner, name, target_toolchain)
            except ResolutionError as exc:
                reservoir_errors.append(str(exc))
                continue
            if selection is not None:
                selected = with_revision(as_direct(old_package), selection.revision)
                selected = with_git_source(
                    selected,
                    selection.repository_url or repository_url,
                    selection.subdir if selection.subdir is not None else subdir,
                )
                detail = (
                    f"Reservoir records a successful exact-toolchain build for {selection.identity}"
                )
                if selection.run_at:
                    detail += f" at {selection.run_at}"
                return Decision(
                    dep,
                    "selected",
                    "reservoir",
                    revision=selection.revision,
                    package=selected,
                    evidence=[*evidence, detail, "the commit's lean-toolchain matches exactly"],
                )

        if repository_url is None:
            for owner, name in identities:
                try:
                    repository_url, discovered_subdir = self.reservoir.package_source(owner, name)
                except Exception as exc:  # The history fallback should still be attempted when possible.
                    reservoir_errors.append(str(exc))
                    continue
                if repository_url:
                    subdir = subdir or discovered_subdir
                    break

        if repository_url:
            try:
                selection = self.history.find_exact_toolchain(
                    repository_url, target_toolchain, subdir
                )
            except ResolutionError as exc:
                evidence.append(f"Git history search failed: {exc}")
            else:
                if selection is not None:
                    selected = with_git_source(as_direct(old_package), repository_url, subdir)
                    selected = with_revision(selected, selection.revision)
                    return Decision(
                        dep,
                        "selected",
                        "git-history",
                        revision=selection.revision,
                        package=selected,
                        evidence=[
                            *evidence,
                            f"Git history: {selection.reason}",
                            "the selected commit's lean-toolchain matches exactly",
                        ],
                    )
                evidence.append("no Git history boundary or tip has the exact target toolchain")
        else:
            evidence.append("no Git repository URL could be determined")

        evidence.extend(f"Reservoir: {message}" for message in reservoir_errors)
        return Decision(
            dep,
            "unresolved",
            "none",
            evidence=evidence,
            reason="no eligible revision was found under the exact-toolchain protocol",
        )


def save_plan(plan: Plan, path: Path) -> None:
    content = json.dumps(plan.to_dict(), indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(f".{path.name}.lakeva.tmp")
    temporary.write_text(content, encoding="utf-8", newline="")
    temporary.replace(path)


def load_plan(path: Path) -> Plan:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"plan not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid plan JSON in {path}: {exc}") from exc
    try:
        return Plan.from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(f"invalid plan in {path}: {exc}") from exc


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dep_key(dep: DirectDependency) -> tuple[str, str]:
    return dep.scope.casefold(), dep.name.casefold()


def _display_key(key: tuple[str, str]) -> str:
    return f"@{key[0]}/{key[1]}" if key[0] else key[1]


def _reservoir_identities(
    dep: DirectDependency, old_package: dict[str, Any]
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def add(owner: str, name: str) -> None:
        key = (owner, name)
        if owner and name and (owner.casefold(), name.casefold()) not in {
            (a.casefold(), b.casefold()) for a, b in result
        }:
            result.append(key)

    if dep.scope:
        add(dep.scope, dep.name)
    old_scope = package_scope(old_package)
    if old_scope:
        add(old_scope, dep.name)
    url = dep.url or package_url(old_package)
    parsed = _github_identity(url) if url else None
    if parsed:
        owner, repository = parsed
        add(owner, dep.name)
        add(owner, repository)
    return result


def _github_identity(url: str) -> tuple[str, str] | None:
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    else:
        parsed = urlparse(url)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            return None
        path = parsed.path
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    repository = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return parts[0], repository

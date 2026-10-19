from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .errors import LakeVAError, StalePlanError
from .lakefile import parse_lakefile, update_explicit_revisions
from .manifest import (
    compose_target_manifest,
    find_package,
    load_manifest,
    package_inherited,
    package_name,
    package_revision,
    write_manifest,
)
from .models import Plan
from .planner import sha256_file


@dataclass(frozen=True)
class ApplyResult:
    target: Path
    backup: Path | None
    lake_updated: bool
    selected_count: int
    unresolved_count: int


def apply_plan(
    plan: Plan,
    target: Path | None = None,
    *,
    allow_unresolved: bool = False,
    force: bool = False,
    run_update: bool = True,
    lake_command: str = "lake",
    backup: bool = True,
) -> ApplyResult:
    target = (target or Path(plan.target_project)).resolve()
    lakefile = target / plan.lakefile_name
    manifest_path = target / "lake-manifest.json"
    toolchain_path = target / "lean-toolchain"
    unresolved = plan.unresolved
    if unresolved and not allow_unresolved:
        names = ", ".join(decision.dependency.display_name for decision in unresolved)
        raise LakeVAError(
            f"plan has unresolved dependencies: {names}; review the plan or pass --allow-unresolved"
        )

    if not force:
        current_hashes = {
            plan.lakefile_name: sha256_file(lakefile),
            "lake-manifest.json": sha256_file(manifest_path),
            "lean-toolchain": sha256_file(toolchain_path),
        }
        changed = [
            name
            for name, expected in plan.target_hashes.items()
            if current_hashes.get(name) != expected
        ]
        if changed:
            raise StalePlanError(
                "target changed since planning: " + ", ".join(changed) + "; create a new plan"
            )

    current_dependencies = parse_lakefile(lakefile)
    by_index = {dep.declaration_index: dep for dep in current_dependencies}
    revisions: dict[int, str] = {}
    replacements: list[tuple[str, str, dict]] = []
    for decision in plan.decisions:
        if not decision.applicable:
            continue
        dep = decision.dependency
        current = by_index.get(dep.declaration_index)
        if current is None or current.name.casefold() != dep.name.casefold():
            raise StalePlanError(
                f"direct dependency declaration for {dep.display_name} moved; create a new plan"
            )
        assert decision.package is not None
        replacements.append((dep.name, dep.scope, decision.package))
        if current.explicit_rev and decision.revision:
            revisions[current.declaration_index] = decision.revision

    original_manifest_text = manifest_path.read_text(encoding="utf-8")
    original_lakefile_text = lakefile.read_text(encoding="utf-8")
    original_toolchain_text = toolchain_path.read_text(encoding="utf-8")
    manifest = compose_target_manifest(
        load_manifest(target), replacements, plan.manifest_template
    )
    update_arguments = _lake_update_arguments(plan, manifest, by_index)
    lakefile_text = update_explicit_revisions(lakefile, revisions)
    backup_dir = _create_backup(target, lakefile, manifest_path, toolchain_path) if backup else None

    try:
        _atomic_write(toolchain_path, plan.target_toolchain + "\n")
        _atomic_write(lakefile, lakefile_text)
        write_manifest(manifest_path, manifest)
        if run_update:
            _run_lake_update(target, lake_command, update_arguments, plan.target_toolchain)
            _restore_target_toolchain(toolchain_path, plan.target_toolchain)
            _verify_selected_revisions(plan, target)
    except Exception:
        # Restore only files owned by this operation. Lake's package checkout/cache is left intact.
        _atomic_write(lakefile, original_lakefile_text)
        _atomic_write(manifest_path, original_manifest_text)
        _atomic_write(toolchain_path, original_toolchain_text)
        raise

    return ApplyResult(
        target=target,
        backup=backup_dir,
        lake_updated=run_update,
        selected_count=len(replacements),
        unresolved_count=len(unresolved),
    )


def _lake_update_arguments(plan: Plan, manifest: dict, by_index: dict) -> list[str]:
    """Package names passed to ``lake update`` so Lake keeps the selected records.

    A bare ``lake update`` re-materializes *every* root dependency from the
    lakefile's ``require`` (``@ "master"`` means the current branch tip) and
    ignores the manifest entirely; only ``lake update <pkg>`` reuses the manifest
    records of the other root dependencies (``Workspace.updateAndMaterialize`` in
    Lake's ``Load/Main.lean`` up to v4.8 and ``reuseManifest`` in
    ``Load/Resolve.lean`` afterwards).  Inherited (transitive) records are never
    reused, so naming one re-resolves transitives while every selected direct
    record stays pinned.  Lake v4.34+ additionally requires the name to be a root
    dependency or a record of the manifest; an inherited record satisfies that.
    Without an inherited record, a direct dependency whose lakefile ``rev`` was
    rewritten to the selected revision is named instead: Lake then re-resolves
    it from the lakefile to the same commit.
    """
    for package in manifest["packages"]:
        name = package_name(package) if isinstance(package, dict) else None
        if name and package_inherited(package):
            return [_lake_name_argument(name)]
    selected = [d for d in plan.decisions if d.applicable and d.revision]
    for decision in selected:
        current = by_index.get(decision.dependency.declaration_index)
        # A `meta if` dependency is not a root dependency unless its
        # configuration is passed, so Lake could not update it by name.
        if current is not None and current.explicit_rev and not current.conditional:
            return [_lake_name_argument(decision.dependency.name)]
    if selected:
        print(
            "warning: no transitive manifest record and no explicit lakefile revision; "
            "a bare `lake update` may replace the selected direct revisions",
            file=sys.stderr,
        )
    return []


def _lake_name_argument(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name.startswith("«") and name.endswith("»"):
        name = name[1:-1]
    return name


def _run_lake_update(
    target: Path,
    lake_command: str,
    arguments: list[str] | None = None,
    target_toolchain: str | None = None,
) -> None:
    arguments = list(arguments or [])
    command = [lake_command, "update", "--keep-toolchain", *arguments]
    # Pin Elan to the exact target toolchain so the Lake that resolves the
    # manifest does not depend on the `lean-toolchain` file (Lake forwards the
    # pin to the processes it spawns).  A hook that selects a toolchain
    # explicitly is not affected: Mathlib's 2024 `post_update` hook copies
    # Mathlib's own `lean-toolchain` over the root's and then runs
    # `elan run --install <that toolchain> lake exe cache get`; the manifest is
    # already saved by then and `lake exe` never rewrites it, so only the
    # toolchain file needs restoring afterwards (`_restore_target_toolchain`).
    environment = dict(os.environ)
    if target_toolchain:
        environment["ELAN_TOOLCHAIN"] = target_toolchain
    print(f"info: running {' '.join(command)} (selected direct records are reused from the manifest)")
    try:
        result = subprocess.run(
            command,
            cwd=target,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise LakeVAError(f"Lake executable not found: {lake_command}") from exc
    if result.returncode == 0:
        _replay_process_output(result)
        return

    detail = f"{result.stdout}\n{result.stderr}".casefold()
    unsupported = "keep-toolchain" in detail and any(
        marker in detail
        for marker in ("unknown long option", "unknown option", "unrecognized option")
    )
    if not unsupported:
        _replay_process_output(result)
        raise LakeVAError(f"lake update --keep-toolchain failed with exit {result.returncode}")

    print(
        "info: this Lake version predates --keep-toolchain; retrying with `lake update` "
        "and verifying the exact toolchain afterwards"
    )
    fallback = subprocess.run(
        [lake_command, "update", *arguments],
        cwd=target,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _replay_process_output(fallback)
    if fallback.returncode:
        raise LakeVAError(f"lake update failed with exit {fallback.returncode}")


def _restore_target_toolchain(toolchain_path: Path, target_toolchain: str) -> None:
    """Put the exact target toolchain back if a post-update hook replaced it.

    Lake itself keeps the toolchain (``--keep-toolchain``; Lake versions before
    it have no toolchain update at all), but a dependency's post-update hook may
    rewrite ``lean-toolchain``: Mathlib's 2024 hook copies Mathlib's own
    toolchain over the root's (and runs its cache fetch under that toolchain
    via ``elan run``).  The manifest was already resolved and written
    by the target toolchain's Lake before the hooks ran (``updateAndMaterialize``
    in Lake's ``Load/Resolve.lean``), so restoring the file keeps the exact
    toolchain the protocol demands; when the hook's toolchain differs the
    pinned dependency revision simply predates or postdates the target
    toolchain, which the build decides.
    """
    try:
        actual = toolchain_path.read_text(encoding="utf-8").lstrip("\ufeff").strip()
    except FileNotFoundError:
        actual = None
    if actual == target_toolchain:
        return
    print(
        "warning: a dependency's post-update hook replaced lean-toolchain with "
        f"{actual!r} (Mathlib's hook copies its own toolchain); restoring the exact "
        f"target toolchain {target_toolchain!r}",
        file=sys.stderr,
    )
    _atomic_write(toolchain_path, target_toolchain + "\n")


def _replay_process_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)


def _verify_selected_revisions(plan: Plan, target: Path) -> None:
    resolved = load_manifest(target)
    drift: list[str] = []
    for decision in plan.decisions:
        if not decision.applicable or not decision.revision:
            continue
        package = find_package(
            resolved, decision.dependency.name, decision.dependency.scope
        )
        actual = package_revision(package) if package else None
        if package is None and decision.dependency.conditional:
            # Declared under `meta if ... then`: not a root dependency for this
            # `lake update`, so Lake dropped the record rather than changing it.
            print(
                f"info: {decision.dependency.display_name} is a conditional (`meta if`) "
                "dependency and is absent from the resolved manifest; not a drift"
            )
            continue
        if actual != decision.revision:
            drift.append(
                f"{decision.dependency.display_name}: selected {decision.revision}, got {actual or 'missing'}"
            )
    if drift:
        raise LakeVAError(
            "Lake changed manually selected direct revisions; the operation was rolled back:\n"
            + "\n".join(f"  - {item}" for item in drift)
            + "\nPin these dependencies explicitly in the target lakefile before retrying."
        )


def _create_backup(
    target: Path, lakefile: Path, manifest: Path, toolchain: Path
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = target / ".lakeva-backup" / f"{stamp}-{uuid4().hex[:8]}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for source in (lakefile, manifest, toolchain):
        if source.is_file():
            shutil.copy2(source, backup_dir / source.name)
    return backup_dir


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="")
    os.replace(temporary, path)

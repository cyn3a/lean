from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class DirectDependency:
    name: str
    scope: str = ""
    source_type: Literal["git", "reservoir", "path", "unknown"] = "unknown"
    url: str | None = None
    subdir: str | None = None
    declared_rev: str | None = None
    declared_version: str | None = None
    explicit_rev: bool = False
    declaration_index: int = 0
    # Declared under a Lake `meta if ... then` guard (for example doc-gen4 behind
    # `get_config? env = some "dev"`).  Lake only treats it as a root dependency
    # when that configuration is passed, so `lake update` drops its manifest
    # record without that being a revision change.
    conditional: bool = False

    @property
    def display_name(self) -> str:
        return f"@{self.scope}/{self.name}" if self.scope else self.name

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DirectDependency:
        return cls(**value)


@dataclass
class Decision:
    dependency: DirectDependency
    status: Literal[
        "selected", "local-preserved", "unresolved", "unsupported"
    ]
    source: Literal[
        "new-manifest", "reservoir", "git-history", "old-manifest", "none"
    ]
    revision: str | None = None
    package: dict[str, Any] | None = None
    evidence: list[str] = field(default_factory=list)
    reason: str | None = None

    @property
    def applicable(self) -> bool:
        return self.status in {"selected", "local-preserved"} and self.package is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dependency"] = self.dependency.to_dict()
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Decision:
        data = dict(value)
        data["dependency"] = DirectDependency.from_dict(data["dependency"])
        return cls(**data)


@dataclass
class Plan:
    schema_version: int
    created_at: str
    old_project: str
    new_project: str
    target_project: str
    target_toolchain: str
    lakefile_name: str
    target_hashes: dict[str, str | None]
    decisions: list[Decision]
    # The new project's manifest: it defines the manifest layout the target
    # toolchain's Lake reads and its transitive records.  Optional so that plans
    # written by earlier versions still load (they then keep the old layout).
    manifest_template: dict[str, Any] | None = None

    @property
    def unresolved(self) -> list[Decision]:
        return [d for d in self.decisions if not d.applicable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "old_project": self.old_project,
            "new_project": self.new_project,
            "target_project": self.target_project,
            "target_toolchain": self.target_toolchain,
            "lakefile_name": self.lakefile_name,
            "target_hashes": self.target_hashes,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "manifest_template": self.manifest_template,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Plan:
        if value.get("schema_version") != 1:
            raise ValueError(f"unsupported plan schema: {value.get('schema_version')!r}")
        data = dict(value)
        data["decisions"] = [Decision.from_dict(item) for item in data["decisions"]]
        return cls(**data)

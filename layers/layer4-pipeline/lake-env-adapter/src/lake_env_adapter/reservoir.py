from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .errors import ResolutionError


JsonTransport = Callable[[str], Any]
TextTransport = Callable[[str], str]


@dataclass(frozen=True)
class ReservoirSelection:
    revision: str
    repository_url: str | None
    subdir: str | None
    identity: str
    run_at: str | None


class ReservoirClient:
    def __init__(
        self,
        base_url: str = "https://reservoir.lean-lang.org/api/v1",
        *,
        timeout: float = 20.0,
        json_transport: JsonTransport | None = None,
        text_transport: TextTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._json_transport = json_transport or self._get_json
        self._text_transport = text_transport or self._get_text

    def select_exact_build(
        self, owner: str, name: str, target_toolchain: str
    ) -> ReservoirSelection | None:
        owner_q, name_q = quote(owner, safe=""), quote(name, safe="")
        root = f"{self.base_url}/packages/{owner_q}/{name_q}"
        try:
            metadata = self._json_transport(root)
            build_document = self._json_transport(f"{root}/builds")
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise ResolutionError(f"Reservoir request failed ({exc.code}) for @{owner}/{name}") from exc
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise ResolutionError(f"Reservoir request failed for @{owner}/{name}: {exc}") from exc

        if isinstance(metadata, dict) and isinstance(metadata.get("data"), dict):
            metadata = metadata["data"]
        if not isinstance(metadata, dict):
            raise ResolutionError(f"Reservoir returned invalid metadata for @{owner}/{name}")
        builds = build_document.get("data", []) if isinstance(build_document, dict) else []
        if not isinstance(builds, list):
            raise ResolutionError(f"Reservoir returned invalid builds for @{owner}/{name}")

        source = _github_source(metadata)
        repository_url = _source_url(source)
        subdir = source.get("subDir") if source and isinstance(source.get("subDir"), str) else None
        matching = [
            build
            for build in builds
            if isinstance(build, dict)
            and build.get("built") is True
            and build.get("toolchain") == target_toolchain
            and isinstance(build.get("revision"), str)
        ]
        matching.sort(key=lambda item: str(item.get("runAt", "")), reverse=True)
        for build in matching:
            revision = build["revision"]
            if source is None:
                continue
            if self._verify_toolchain(source, revision, subdir, target_toolchain):
                return ReservoirSelection(
                    revision=revision,
                    repository_url=repository_url,
                    subdir=subdir,
                    identity=f"@{owner}/{name}",
                    run_at=build.get("runAt") if isinstance(build.get("runAt"), str) else None,
                )
        return None

    def package_source(self, owner: str, name: str) -> tuple[str | None, str | None]:
        owner_q, name_q = quote(owner, safe=""), quote(name, safe="")
        metadata = self._json_transport(
            f"{self.base_url}/packages/{owner_q}/{name_q}"
        )
        if isinstance(metadata, dict) and isinstance(metadata.get("data"), dict):
            metadata = metadata["data"]
        source = _github_source(metadata if isinstance(metadata, dict) else {})
        if source is None:
            return None, None
        subdir = source.get("subDir") if isinstance(source.get("subDir"), str) else None
        return _source_url(source), subdir

    def _verify_toolchain(
        self,
        source: dict[str, Any],
        revision: str,
        subdir: str | None,
        target_toolchain: str,
    ) -> bool:
        full_name = source.get("fullName")
        if not isinstance(full_name, str) or "/" not in full_name:
            return False
        path = f"{subdir.strip('/')}/lean-toolchain" if subdir else "lean-toolchain"
        url = f"https://raw.githubusercontent.com/{full_name}/{quote(revision, safe='')}/{path}"
        try:
            return self._text_transport(url).strip() == target_toolchain
        except (HTTPError, URLError, OSError, UnicodeError):
            return False

    def _get_json(self, url: str) -> Any:
        return json.loads(self._request(url).decode("utf-8"))

    def _get_text(self, url: str) -> str:
        return self._request(url).decode("utf-8")

    def _request(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "lake-env-adapter/0.2"})
        with urlopen(request, timeout=self.timeout) as response:
            return response.read()


def _github_source(metadata: dict[str, Any]) -> dict[str, Any] | None:
    sources = metadata.get("sources", [])
    if not isinstance(sources, list):
        return None
    for source in sources:
        if isinstance(source, dict) and source.get("host") == "github":
            return source
    return None


def _source_url(source: dict[str, Any] | None) -> str | None:
    if source is None:
        return None
    for key in ("gitUrl", "repoUrl"):
        value = source.get(key)
        if isinstance(value, str):
            if key == "repoUrl" and value.startswith("https://github.com/"):
                return value if value.endswith(".git") else f"{value}.git"
            return value
    full_name = source.get("fullName")
    if isinstance(full_name, str):
        return f"https://github.com/{full_name}.git"
    return None

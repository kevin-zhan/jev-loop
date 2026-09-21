"""Project-scoped bundle discovery for the Jev Bundle Specification.

The single discovery location is ``<project_root>/.agents/jev-bundle/<name>/`` with a
``bundle.json`` manifest and a ``BUNDLE.md`` instruction file.  Discovery is deliberately
inert: it reads JSON and filesystem metadata only.  It never imports or executes bundle
code, never installs a dependency, never writes inside the bundle and never makes a network
request.  Names are discovered inside one explicitly selected project — a bundle is not
looked up by walking parent directories or other repositories.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import (
    BUNDLE_NAME_PATTERN,
    STANDARD_SCHEMA_VERSION,
    BundleManifest,
    ManifestError,
    load_manifest,
)

Json = Any

AGENTS_DIRNAME = ".agents"
BUNDLE_ROOT_DIRNAME = "jev-bundle"
MANIFEST_FILENAME = "bundle.json"
INSTRUCTIONS_FILENAME = "BUNDLE.md"

STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_OUTSIDE_TRUST_ROOT = "outside_trust_root"


@dataclass(frozen=True)
class DiscoveryEntry:
    """One entry under the discovery root, valid or not.

    Invalid entries stay visible with their problems instead of disappearing, so a name
    lookup never silently falls through to something else.
    """

    name: str
    directory: Path
    manifest_path: Path
    status: str
    problems: tuple[str, ...] = ()
    manifest: BundleManifest | None = None
    provenance: str = ""

    @property
    def valid(self) -> bool:
        return self.status == STATUS_OK

    def to_json(self) -> dict[str, Json]:
        return {
            "name": self.name,
            "status": self.status,
            "provenance": self.provenance,
            "manifest_path": str(self.manifest_path),
            "problems": list(self.problems),
            "manifest": self.manifest.to_json() if self.manifest is not None else None,
        }


def discovery_root(project_root: Path) -> Path:
    return Path(project_root) / AGENTS_DIRNAME / BUNDLE_ROOT_DIRNAME


def discover_bundles(project_root: Path) -> tuple[DiscoveryEntry, ...]:
    """List every entry in the project discovery root, sorted by directory name."""
    root = discovery_root(project_root)
    if not root.is_dir():
        return ()
    project = Path(project_root).resolve()
    entries: list[DiscoveryEntry] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.name.startswith("."):
            continue  # hidden entries are not bundle directories
        if not child.is_dir():
            continue  # loose files in the discovery root are not bundles
        entries.append(_inspect_entry(child, project=project))
    return tuple(entries)


def find_bundle(project_root: Path, name: str) -> DiscoveryEntry | None:
    """Look one bundle name up inside the project root.  No other scope is searched."""
    for entry in discover_bundles(project_root):
        if entry.name == name:
            return entry
    return None


def _inspect_entry(directory: Path, *, project: Path) -> DiscoveryEntry:
    name = directory.name
    manifest_path = directory / MANIFEST_FILENAME
    provenance = f"{AGENTS_DIRNAME}/{BUNDLE_ROOT_DIRNAME}/{name}/{MANIFEST_FILENAME}"
    problems: list[str] = []

    resolved_directory = directory.resolve()
    if not _inside(resolved_directory, project):
        return DiscoveryEntry(
            name=name,
            directory=directory,
            manifest_path=manifest_path,
            status=STATUS_OUTSIDE_TRUST_ROOT,
            problems=(f"bundle directory resolves outside the project root: {resolved_directory}",),
            provenance=provenance,
        )
    if not BUNDLE_NAME_PATTERN.match(name) or len(name) > 64:
        problems.append(
            f"directory name {name!r} is not a valid bundle name (lowercase letters, digits, single hyphens)"
        )
    if not manifest_path.is_file():
        problems.append(f"missing {MANIFEST_FILENAME}")
        return DiscoveryEntry(
            name=name,
            directory=directory,
            manifest_path=manifest_path,
            status=STATUS_INVALID,
            problems=tuple(problems),
            provenance=provenance,
        )
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as error:
        problems.append(str(error))
        return DiscoveryEntry(
            name=name,
            directory=directory,
            manifest_path=manifest_path,
            status=STATUS_INVALID,
            problems=tuple(problems),
            provenance=provenance,
        )

    if manifest.schema_version != STANDARD_SCHEMA_VERSION:
        problems.append(
            "schema_version 1 is the legacy manifest format and is not a standard-location bundle; "
            "run it by explicit path or migrate it to schema_version 2"
        )
    else:
        if manifest.name != name:
            problems.append(f"manifest name {manifest.name!r} does not match directory name {name!r}")
        if not (directory / INSTRUCTIONS_FILENAME).is_file():
            problems.append(f"missing {INSTRUCTIONS_FILENAME}")

    return DiscoveryEntry(
        name=name,
        directory=directory,
        manifest_path=manifest_path,
        status=STATUS_INVALID if problems else STATUS_OK,
        problems=tuple(problems),
        manifest=manifest,
        provenance=provenance,
    )


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def summary(entries: tuple[DiscoveryEntry, ...]) -> Mapping[str, int]:
    counts = {STATUS_OK: 0, STATUS_INVALID: 0, STATUS_OUTSIDE_TRUST_ROOT: 0}
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
    return counts

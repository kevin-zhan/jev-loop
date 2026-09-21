"""One resolver for bundle references, shared by the host service, the CLI and the doctor.

A reference is one of:

``diagnostic``
    The built-in contract probe.  Reserved: a discovered bundle cannot shadow it.
``project:<name>``
    An unambiguous name lookup inside ``<project_root>/.agents/jev-bundle/``.
``<name>``
    Convenience form of ``project:<name>``.  A bare reference that is *also* an existing
    file relative to the project root is reported as ambiguous instead of silently
    replacing the legacy path semantics; use ``./path/bundle.json`` or ``project:<name>``.
``<path>``
    Legacy path form: absolute, or a relative path containing a separator or ending in
    ``.json``.  Relative paths resolve against the project root.

Every form is re-checked against the same trust root: the resolved manifest must live
inside the project root, and a bundle directory that resolves outside it (for example
through a symlink) is refused.  A manifest v2 ``python_path`` must additionally stay inside
its own bundle directory, so a safely placed manifest cannot import code from elsewhere.
Resolution reads JSON only: no import, no execution, no dependency install, no network.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .discovery import (
    MANIFEST_FILENAME,
    DiscoveryEntry,
    discovery_root,
    find_bundle,
)
from .manifest import (
    BUNDLE_NAME_PATTERN,
    STANDARD_SCHEMA_VERSION,
    BundleManifest,
    ManifestError,
    load_manifest,
)

DIAGNOSTIC_REF = "diagnostic"
PROJECT_SCHEME = "project"

KIND_DIAGNOSTIC = "diagnostic"
KIND_NAME = "name"
KIND_PATH = "path"


class BundleRefError(ValueError):
    """A bundle reference cannot be resolved unambiguously inside the project root."""


@dataclass(frozen=True)
class ResolvedBundle:
    kind: str
    ref: str
    manifest_path: Path | None
    manifest: BundleManifest | None = None
    name: str | None = None
    provenance: str = ""

    @property
    def is_diagnostic(self) -> bool:
        return self.kind == KIND_DIAGNOSTIC

    @property
    def is_standard(self) -> bool:
        return self.manifest is not None and self.manifest.schema_version == STANDARD_SCHEMA_VERSION

    @property
    def scaffold(self) -> bool:
        return self.manifest is not None and self.manifest.scaffold

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "name": self.name,
            "provenance": self.provenance,
            "manifest_path": str(self.manifest_path) if self.manifest_path is not None else None,
            "manifest": self.manifest.to_json() if self.manifest is not None else None,
        }


def _echo(reference: str) -> str:
    """Echo only a reference that is recognisably a name, scheme or path.

    Arbitrary input (a pasted credential, a stray argument) must never travel back into an
    error message, log or transcript, so anything unrecognised is described generically.
    """
    candidate = reference.strip()
    if len(candidate) > 200:
        return "the given bundle reference"
    if candidate == DIAGNOSTIC_REF or _looks_like_path(candidate) or BUNDLE_NAME_PATTERN.match(candidate):
        return repr(candidate)
    if candidate.startswith(f"{PROJECT_SCHEME}:"):
        return repr(candidate)
    return "the given bundle reference"


def _echo_scheme(scheme: str) -> str:
    return repr(scheme) if re.fullmatch(r"[a-z][a-z0-9-]*", scheme) else "the given scheme"


def project_root_of(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def resolve_bundle_ref(ref: str, project_root: Path | str) -> ResolvedBundle:
    """Resolve one reference, enforcing the trust root for every form."""
    project = project_root_of(project_root)
    if not isinstance(ref, str) or not ref.strip():
        raise BundleRefError(
            "bundle reference is required: 'diagnostic', 'project:<name>', a discovered bundle name, "
            "or a manifest path inside the project root (for example "
            f"'{discovery_root(project)}/<name>/{MANIFEST_FILENAME}')"
        )
    reference = ref.strip()

    if reference == DIAGNOSTIC_REF:
        return ResolvedBundle(kind=KIND_DIAGNOSTIC, ref=reference, manifest_path=None, provenance=DIAGNOSTIC_REF)

    if ":" in reference and not _looks_like_path(reference):
        scheme, _, remainder = reference.partition(":")
        if scheme != PROJECT_SCHEME:
            raise BundleRefError(
                f"unsupported bundle reference scheme {scheme!r}; supported schemes are 'project:<name>' "
                f"and the built-in {DIAGNOSTIC_REF!r}"
            )
        return _resolve_name(remainder.strip(), project, ref=reference)

    if _looks_like_path(reference):
        return _resolve_path(reference, project, ref=reference)

    # Convenience form: a bare name.  Legacy manifests used to be addressed by a bare
    # relative path, so a reference that is *both* is refused as ambiguous instead of
    # silently changing what it means.
    entry = find_bundle(project, reference)
    legacy_candidate = (project / reference).resolve()
    legacy_is_file = _inside(legacy_candidate, project) and legacy_candidate.is_file()
    if legacy_is_file and entry is not None:
        raise BundleRefError(
            f"{_echo(reference)} is ambiguous: it is both a file relative to the project root "
            f"({legacy_candidate}) and a bundle name. Use './{reference}' for the file or "
            f"'project:{reference}' for the bundle name."
        )
    if legacy_is_file:
        return _resolve_path(reference, project, ref=reference)
    if entry is not None:
        _require_project_root(entry.manifest_path, project, ref=reference)
        return _from_entry(entry, ref=reference)
    if not BUNDLE_NAME_PATTERN.match(reference):
        raise BundleRefError(
            f"{_echo(reference)} is not a valid bundle name (lowercase letters, digits and single hyphens) "
            "and is not a path inside the project root"
        )
    raise BundleRefError(_unknown_name_message(project, reference))


def _resolve_name(name: str, project: Path, *, ref: str) -> ResolvedBundle:
    if not name:
        raise BundleRefError("'project:' must be followed by a bundle name")
    if not BUNDLE_NAME_PATTERN.match(name) or len(name) > 64:
        raise BundleRefError(
            f"{_echo(name)} is not a valid bundle name (lowercase letters, digits and single hyphens)"
        )
    entry = find_bundle(project, name)
    if entry is None:
        raise BundleRefError(_unknown_name_message(project, name))
    _require_project_root(entry.manifest_path, project, ref=ref)
    return _from_entry(entry, ref=ref)


def _from_entry(entry: DiscoveryEntry, *, ref: str) -> ResolvedBundle:
    if entry.valid and entry.manifest is not None:
        _require_python_path(entry.manifest)
        return ResolvedBundle(
            kind=KIND_NAME,
            ref=ref,
            manifest_path=entry.manifest_path,
            manifest=entry.manifest,
            name=entry.name,
            provenance=entry.provenance,
        )
    details = "; ".join(entry.problems) or "the entry is not usable"
    raise BundleRefError(f"bundle {entry.name!r} in {entry.provenance} is not usable: {details}")


def _resolve_path(reference: str, project: Path, *, ref: str) -> ResolvedBundle:
    candidate = Path(reference).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    resolved = candidate.resolve()
    if not _inside(resolved, project):
        raise BundleRefError(f"bundle manifest must be inside the project root {project}: {_echo(reference)}")
    if not resolved.is_file():
        hint = ""
        directory_manifest = resolved / MANIFEST_FILENAME
        if directory_manifest.is_file():
            hint = f" (did you mean '{directory_manifest}'?)"
        raise BundleRefError(f"bundle manifest does not exist: {resolved}{hint}")
    try:
        manifest = load_manifest(resolved)
    except ManifestError as error:
        raise BundleRefError(str(error)) from error
    if manifest.schema_version == STANDARD_SCHEMA_VERSION:
        _require_python_path(manifest)
    return ResolvedBundle(
        kind=KIND_PATH,
        ref=ref,
        manifest_path=resolved,
        manifest=manifest,
        name=manifest.name,
        provenance=str(resolved),
    )


def _require_python_path(manifest: BundleManifest) -> None:
    """A v2 bundle may import only from inside its own directory."""
    python_dir = manifest.python_dir
    bundle_dir = manifest.directory.resolve()
    if python_dir != bundle_dir and not python_dir.is_relative_to(bundle_dir):
        raise BundleRefError(
            f"bundle {manifest.path} declares python_path {manifest.python_path!r} which resolves outside "
            f"the bundle directory {bundle_dir}; a manifest v2 bundle must be self-contained"
        )


def _require_project_root(manifest_path: Path, project: Path, *, ref: str) -> None:
    resolved = manifest_path.resolve()
    if not _inside(resolved, project):
        raise BundleRefError(
            f"bundle {_echo(ref)} resolves outside the project root {project}: {resolved}"
        )


def _looks_like_path(reference: str) -> bool:
    if os.sep in reference or (os.altsep and os.altsep in reference):
        return True
    if reference.startswith("~") or reference.startswith("./") or reference.startswith("../"):
        return True
    return reference.endswith(".json")


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _unknown_name_message(project: Path, name: str) -> str:
    from .discovery import discover_bundles

    entries = discover_bundles(project)
    root = discovery_root(project)
    hint = ""
    directory_manifest = (project / name / MANIFEST_FILENAME).resolve()
    if _inside(directory_manifest, project) and directory_manifest.is_file():
        hint = f"; did you mean the explicit path '{directory_manifest}'?"
    if not entries:
        return (
            f"no bundle named {name!r}: {root} has no bundle directories "
            f"(run 'jev-loop bundle list --project-root {project}')" + hint
        )
    valid = [entry.name for entry in entries if entry.valid]
    invalid = [f"{entry.name} ({'; '.join(entry.problems)})" for entry in entries if not entry.valid]
    detail = [f"available bundles: {valid}" if valid else "no valid bundles"]
    if invalid:
        detail.append(f"unusable entries: {invalid}")
    return f"no bundle named {name!r} in {root}; " + "; ".join(detail) + hint

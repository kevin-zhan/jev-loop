"""Static conformance validation for a bundle, shared by the CLI, the doctor and the RPC.

``validate_bundle`` reads files only.  It never imports or executes bundle code, never
installs a dependency, never reads the environment for credential values and never makes a
network request.  What it reports is therefore deliberately split:

``errors``
    Enforced contract violations: an unsupported or malformed manifest, a missing
    required field, an unknown field, a directory/name mismatch, a missing ``BUNDLE.md``,
    a ``python_path`` that is missing or escapes the bundle, a declared schema that cannot
    be enforced, config defaults that contradict the declared config schema, credential
    environment entries that are not variable names.
``warnings``
    Valid but incomplete or risky: a recommended metadata field left out, a module file the
    entrypoint may not find, a scaffold that still has to be completed, or a scan that could not
    cover every file within its budget.
``advisory``
    Declarations that are recorded but **enforced nowhere**: they do not grant permissions,
    do not create resource claims, and do not guarantee that a stop succeeds or that a run
    was verified.  The real enforcement is the host runtime (resource keys, owner, lease)
    and the bundle's own verifier.
``not_checked``
    Facts a static read cannot establish: the controller/factory shape (checked only when
    the host loads the bundle), runtime behaviour, dependency availability, credential
    presence, and any live network or device interaction.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schema import SchemaValidationError, validate_declared_defaults
from . import scaffold as scaffold_module
from .discovery import INSTRUCTIONS_FILENAME, MANIFEST_FILENAME, discovery_root
from .manifest import (
    BUNDLE_NAME_PATTERN,
    READ_CHUNK_BYTES,
    STANDARD_SCHEMA_VERSION,
    BundleManifest,
    FileAccessError,
    ManifestError,
    load_manifest,
    require_no_follow_support,
)
from .resolve import BundleRefError, ResolvedBundle, resolve_bundle_ref

Json = Any

REPORT_SCHEMA_VERSION = 1
MAX_SCANNED_FILE_BYTES = 512 * 1024
MAX_SCANNED_FILES = 400
MAX_SCAN_ENTRIES = 4_000
MAX_SCAN_TOTAL_BYTES = 4 * 1024 * 1024
MAX_SCAN_DEPTH = 32
SKIPPED_DIRECTORY_NAMES = frozenset({"__pycache__", "node_modules", ".git", ".venv", ".mypy_cache", ".ruff_cache"})

ADVISORY_NOTE = (
    "advisory declaration: it does not grant a permission, does not create a resource claim and does not "
    "guarantee that a stop succeeds or that the run was verified"
)
NOT_CHECKED_ENTRIES = (
    ("factory_and_controller", "the factory/controller shape is checked only when the host loads the bundle"),
    ("runtime_semantics", "cognition, guard, guard-cycle and stop behaviour require a real managed run"),
    ("resource_exclusivity", "resource claims are enforced by the host at run time, not by this report"),
    ("dependency_availability", "declared dependencies are never installed or resolved by this repository"),
    ("credential_presence", "credential variable values are never read; only their names are declared"),
    ("network_and_device", "no network request, browser or device interaction is performed"),
)


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def validate_ref(ref: str, project_root: Path | str) -> dict[str, Json]:
    """Resolve and validate one reference; resolution failures become report errors."""
    try:
        resolved = resolve_bundle_ref(ref, project_root)
    except BundleRefError as error:
        return _report(
            ref=ref,
            resolved=None,
            errors=[Finding("unresolved_reference", str(error))],
            warnings=[],
            advisory=[],
        )
    return validate_resolved(resolved)


def validate_resolved(resolved: ResolvedBundle) -> dict[str, Json]:
    """Validate an already-resolved bundle with the same rules as :func:`validate_ref`.

    The host uses this so a run start and a static validation can never disagree: both consume
    one resolution and one set of findings.
    """
    ref = resolved.ref
    if resolved.is_diagnostic:
        return _report(
            ref=ref,
            resolved=resolved,
            errors=[],
            warnings=[],
            advisory=[Finding("builtin", "the built-in diagnostic bundle is a contract probe, not a task adapter")],
            notable=(
                "diagnostic",
                "the built-in diagnostic bundle is verified by the host test suite, not by this report",
            ),
        )
    return _report(ref=ref, resolved=resolved, **_inspect(resolved))


def validate_manifest_path(manifest_path: Path, *, project_root: Path | None = None) -> dict[str, Json]:
    """Validate a manifest by path (used by the doctor, which may not know the project root)."""
    path = Path(manifest_path).resolve()
    try:
        manifest = load_manifest(path)
    except ManifestError as error:
        return _report(
            ref=str(path),
            resolved=None,
            errors=[Finding("unparseable_manifest", str(error))],
            warnings=[],
            advisory=[],
        )
    resolved = ResolvedBundle(
        kind="path",
        ref=str(path),
        manifest_path=path,
        manifest=manifest,
        name=manifest.name,
        provenance=str(path),
    )
    findings = _inspect(resolved, project_root=project_root)
    return _report(ref=str(path), resolved=resolved, **findings)

def _inspect(resolved: ResolvedBundle, *, project_root: Path | None = None) -> dict[str, list[Finding]]:
    manifest = resolved.manifest
    assert manifest is not None  # callers only pass resolved manifests
    errors: list[Finding] = []
    warnings: list[Finding] = []
    advisory: list[Finding] = []

    if manifest.schema_version == STANDARD_SCHEMA_VERSION:
        _inspect_standard(
            manifest,
            resolved=resolved,
            project_root=project_root,
            errors=errors,
            warnings=warnings,
            advisory=advisory,
        )
        advisory.extend(_advisory_declarations(manifest))
    else:
        _inspect_legacy(manifest, resolved=resolved, errors=errors, warnings=warnings, advisory=advisory)

    _inspect_scaffold_markers(manifest, errors=errors, warnings=warnings)
    return {"errors": errors, "warnings": warnings, "advisory": advisory}


def _inspect_standard(
    manifest: BundleManifest,
    *,
    resolved: ResolvedBundle,
    project_root: Path | None,
    errors: list[Finding],
    warnings: list[Finding],
    advisory: list[Finding],
) -> None:
    directory = manifest.directory
    # The v2 contract applies however the bundle was addressed: a path reference must not be a
    # way around "the manifest name equals its directory name" (legacy v1 is not retrofitted).
    if manifest.name != directory.name:
        errors.append(
            Finding(
                "name_directory_mismatch",
                f"manifest name {manifest.name!r} must equal the directory name {directory.name!r}",
            )
        )
    if not (directory / INSTRUCTIONS_FILENAME).is_file():
        errors.append(
            Finding("missing_instructions", f"{INSTRUCTIONS_FILENAME} is required next to {MANIFEST_FILENAME}")
        )
    elif not _inside(directory / INSTRUCTIONS_FILENAME, directory):
        errors.append(
            Finding(
                "instructions_outside_bundle",
                f"{INSTRUCTIONS_FILENAME} resolves outside the bundle directory; the instructions an agent "
                "reads must belong to this bundle",
            )
        )

    python_dir = manifest.python_dir
    if not python_dir.is_dir():
        errors.append(
            Finding("missing_python_path", f"python_path {manifest.python_path!r} is not a directory: {python_dir}")
        )
    else:
        bundle_dir = directory.resolve()
        if python_dir != bundle_dir and not python_dir.is_relative_to(bundle_dir):
            errors.append(
                Finding(
                    "python_path_escapes_bundle",
                    f"python_path {manifest.python_path!r} resolves to {python_dir}, outside {bundle_dir}",
                )
            )

    if manifest.config_schema is not None and manifest.config:
        try:
            validate_declared_defaults(manifest.config, manifest.config_schema, path="config")
        except SchemaValidationError as error:
            errors.append(
                Finding("config_defaults_invalid", f"declared config defaults are not valid: {error}")
            )

    if manifest.scaffold:
        warnings.append(
            Finding(
                "unfinished_scaffold",
                "scaffold is true: this bundle only demonstrates the plumbing. Replace the placeholder controller "
                "with the real environment/verifier work, run the offline author tests, then set scaffold to false. "
                "The host refuses name/path starts while scaffold is true.",
            )
        )

    _warn_missing_recommended(manifest, warnings=warnings)
    if manifest.name is not None and not BUNDLE_NAME_PATTERN.match(manifest.name):
        errors.append(Finding("invalid_name", f"name {manifest.name!r} is not a valid bundle name"))
    _warn_entrypoint_module(manifest, warnings=warnings)

    if project_root is not None:
        root = discovery_root(project_root)
        if resolved.kind == "path" and manifest.schema_version == STANDARD_SCHEMA_VERSION:
            if not manifest.path.resolve().is_relative_to(root):
                advisory.append(
                    Finding(
                        "outside_discovery_root",
                        f"this bundle lives outside {root}; it runs by explicit path but will not be listed by "
                        f"'jev-loop bundle list'",
                    )
                )


def _inspect_legacy(
    manifest: BundleManifest,
    *,
    resolved: ResolvedBundle,
    errors: list[Finding],
    warnings: list[Finding],
    advisory: list[Finding],
) -> None:
    warnings.append(
        Finding(
            "legacy_manifest",
            "schema_version 1 is the legacy manifest format: no metadata, typed input contract or instructions "
            "file is declared. It keeps running unchanged; declare schema_version 2 to adopt the specification.",
        )
    )
    python_dir = manifest.python_dir
    if not python_dir.is_dir():
        errors.append(
            Finding(
                "missing_python_path",
                f"python_path {manifest.python_path!r} is not a directory: {python_dir}; the host refuses to load "
                "this bundle",
            )
        )
    advisory.append(
        Finding(
            "legacy_contract",
            f"{resolved.ref} has no declared input/config contract, so the host cannot reject a malformed task "
            "before starting a worker",
        )
    )


def _advisory_declarations(manifest: BundleManifest) -> list[Finding]:
    advisory: list[Finding] = []
    for key in manifest.authorizations:
        advisory.append(Finding("authorizations", f"{key!r} is declared but not enforced: {ADVISORY_NOTE}"))
    for key in manifest.resources.get("keys") or ():
        advisory.append(
            Finding(
                "resources.keys",
                f"{key!r} is a declared expectation only; a run creates the real claim when the caller passes "
                f"that key in resource_keys. {ADVISORY_NOTE}",
            )
        )
    for key in manifest.credential_env_names():
        advisory.append(
            Finding(
                "dependencies.credential_env",
                f"{key} is a variable name the bundle may read from the process environment; it is never read, "
                f"validated or installed by the host. {ADVISORY_NOTE}",
            )
        )
    for key in manifest.dependencies.get("python") or ():
        advisory.append(
            Finding(
                "dependencies.python",
                f"{key!r} is declared only and is never installed, resolved or verified by this repository. "
                f"{ADVISORY_NOTE}",
            )
        )
    if manifest.stop:
        advisory.append(
            Finding(
                "stop",
                "stop.grace_seconds/release describe the bundle's intent; the enforced bound is the host's "
                f"stop_grace_seconds and the controller's resources_released confirmation. {ADVISORY_NOTE}",
            )
        )
    if manifest.verification:
        advisory.append(
            Finding(
                "verification",
                "verification.declares who verifies; the verifier's real independence is not established by this "
                f"report. {ADVISORY_NOTE}",
            )
        )
    if manifest.output:
        advisory.append(
            Finding(
                "output",
                "output.schema/evidence describe the bundle's result shape; the host only bounds the size of the "
                f"controller output it records. {ADVISORY_NOTE}",
            )
        )
    return advisory


def _warn_missing_recommended(manifest: BundleManifest, *, warnings: list[Finding]) -> None:
    from .manifest import V2_RECOMMENDED_FIELDS

    missing = [key for key in V2_RECOMMENDED_FIELDS if key not in manifest.raw]
    if missing:
        warnings.append(
            Finding("incomplete_metadata", f"recommended manifest fields are not declared: {sorted(missing)}")
        )


def _warn_entrypoint_module(manifest: BundleManifest, *, warnings: list[Finding]) -> None:
    module = manifest.entrypoint.split(":", 1)[0]
    if not manifest.python_dir.is_dir():
        return
    head = module.split(".", 1)[0]
    if not (manifest.python_dir / f"{head}.py").exists() and not (manifest.python_dir / head).is_dir():
        warnings.append(
            Finding(
                "entrypoint_module_not_found",
                f"entrypoint module {head!r} was not found as {head}.py or {head}/ inside "
                f"{manifest.python_dir}; this is a heuristic because a module may be provided by an installed "
                "package",
            )
        )


def _inspect_scaffold_markers(manifest: BundleManifest, *, errors: list[Finding], warnings: list[Finding]) -> None:
    """Only unrendered *machine* template tokens are errors; prose TODOs are not.

    The scan is bounded on purpose and states its own coverage: it walks the bundle directory
    without following symlinks, prunes cache/hidden directories, never materialises the whole
    tree first, and stops at a file/entry/byte budget.  When the budget is reached the report
    says the scan was incomplete instead of claiming a complete check.
    """
    tokens = scaffold_module.template_tokens()
    if not tokens:
        return
    scan = scan_bundle_files(manifest.directory)
    for item in scan.files:
        text = item.text()  # the bytes captured by the snapshot; nothing is reopened
        found = sorted(token for token in tokens if token in text)
        if found:
            errors.append(
                Finding(
                    "unrendered_template_token",
                    f"{item.relative} still contains template token(s) {found}; "
                    "render the scaffold with 'jev-loop bundle init' instead of editing a template in place",
                )
            )
    if scan.read_failures:
        warnings.append(
            Finding(
                "token_scan_incomplete",
                f"the template-token scan could not read {len(scan.read_failures)} file(s) "
                f"({', '.join(scan.read_failures[:3])}); those files were not checked",
            )
        )
    if scan.incomplete:
        warnings.append(
            Finding(
                "token_scan_incomplete",
                f"the template-token scan stopped after {len(scan.files)} files / {scan.entries} entries / "
                f"{scan.bytes_read} bytes ({scan.reason}); files outside that budget were not scanned",
            )
        )
    if scan.symlinks_skipped:
        warnings.append(
            Finding(
                "symlinks_not_scanned",
                f"{scan.symlinks_skipped} symlink(s) inside the bundle were not followed and not scanned; "
                "a symlink is never read outside the bundle directory",
            )
        )


@dataclass(frozen=True)
class ScanFile:
    """One regular file captured by the bounded scan, with the bytes actually read."""

    relative: str
    payload: bytes
    size_at_open: int

    def text(self) -> str:
        return self.payload.decode("utf-8", errors="replace")


@dataclass
class ScanResult:
    """A bounded snapshot: the enumeration stopped at its budget and every payload is capped.

    Nothing here is re-opened later.  A caller that needs the content of a scanned file uses
    ``files[i].payload`` / ``text()``, so a path that was replaced after the scan cannot turn into
    a second, unbounded read — and the fingerprint is computed from the same bytes the token scan
    saw.
    """

    files: tuple[ScanFile, ...] = ()
    entries: int = 0
    bytes_read: int = 0
    symlinks_skipped: int = 0
    incomplete: bool = False
    reason: str = ""
    read_failures: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True only when the enumeration finished inside its budget *and* every read succeeded."""
        return not self.incomplete and not self.read_failures

    @property
    def coverage(self) -> str:
        """A one-line statement of what this scan actually covered."""
        if self.read_failures:
            return (
                f"partial: {len(self.read_failures)} file(s) could not be read within the scan policy "
                f"({', '.join(self.read_failures[:3])}); no complete fingerprint is available"
            )
        if self.incomplete:
            return (
                f"partial: {len(self.files)} files / {self.bytes_read} bytes scanned, stopped at {self.reason}; "
                f"{self.symlinks_skipped} symlink(s) skipped"
            )
        return (
            f"complete within the scan policy: {len(self.files)} regular files / {self.bytes_read} bytes, "
            f"hidden and cache directories pruned, {self.symlinks_skipped} symlink(s) skipped, "
            f"nothing outside the bundle read"
        )


def _scandir(directory_descriptor: int):
    """The single enumeration point: scandir on a **pinned directory descriptor**.

    Enumerating through the descriptor (rather than a path) is what makes the walk immune to a
    queued directory being replaced by a symlink: there is no path to re-resolve later.
    """
    return os.scandir(directory_descriptor)


def _open_component_at(directory_descriptor: int, name: str) -> int:
    """Open one path component relative to its pinned parent, following nothing.

    This is the seam the root pinning walks with; a component that has become a symlink raises
    (ELOOP), which is what makes an ancestor substitution fail closed instead of redirecting the
    whole scan outside the bundle.
    """
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_descriptor)


def _pin_directory(path: Path) -> int:
    """Pin an absolute path by opening every component with no-follow flags.

    The path is taken **as given**: it is the caller's canonical path (the resolver canonicalizes
    normal caller paths, and a run's manifest path has already been resolved and containment
    checked).  Re-resolving here would defeat the whole check — a root or ancestor replaced by an
    outside symlink *before* this call would simply be blessed by the new resolution, and the walk
    would then open the outside target.  Only a lexical absolute conversion is applied to a
    relative path (``os.path.abspath`` performs no symlink resolution).

    Walking from the filesystem root with ``dir_fd`` and ``O_DIRECTORY | O_NOFOLLOW`` verifies every
    component against its pinned parent, so a substituted component fails closed.  The returned
    descriptor owns one open directory; the caller closes it.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(os.path.abspath(candidate))  # lexical only: no symlink resolution
    parts = [part for part in candidate.parts if part not in {os.sep, ""}]
    descriptor = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            next_descriptor = _open_component_at(descriptor, part)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory_at(directory_descriptor: int, name: str) -> int:
    """Open a child directory relative to its pinned parent, refusing a symlink or non-directory."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_descriptor)


def _open_file_at(directory_descriptor: int, name: str) -> int:
    """Open a child file relative to its pinned parent; no symlink is followed and a FIFO cannot block."""
    return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_descriptor)


def _read_bounded(
    descriptor: int,
    limit: int,
    *,
    chunk: int = READ_CHUNK_BYTES,
    charge: Callable[[int], None] | None = None,
) -> bytes:
    """Read at most ``limit`` bytes, charging each successful ``os.read`` immediately.

    ``charge`` is called with the byte count of every chunk *as it is read*, before anything else
    can fail.  That is what keeps the aggregate budget honest for a partial read: if a later
    ``os.read`` raises (for example EIO), the bytes already read have been charged and cannot be
    re-spent by the next file.  There is no one-byte overflow probe here — the post-read ``fstat``
    shows whether the file held more or less than what was read — so no probe byte exists to
    account for, and the declared bound stays exact.
    """
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        piece = os.read(descriptor, min(remaining, chunk))
        if not piece:
            break
        if charge is not None:
            charge(len(piece))
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


@dataclass
class _ScanState:
    files: list[ScanFile] = field(default_factory=list)
    entries: int = 0
    bytes_read: int = 0
    budget: int = 0
    symlinks_skipped: int = 0
    incomplete: bool = False
    reason: str = ""
    read_failures: list[str] = field(default_factory=list)
    stop: bool = False

    def fail(self, relative: str, why: str) -> None:
        self.read_failures.append(f"{relative} ({why})")
        self.incomplete = True

    def halt(self, reason: str) -> None:
        self.incomplete = True
        self.reason = reason
        self.stop = True


def scan_bundle_files(directory: Path) -> ScanResult:
    """Snapshot the regular files inside ``directory`` with hard traversal and read budgets.

    The walk is **descriptor-anchored**, which is what makes the guarantee real rather than
    best-effort at the path level:

    - the canonical bundle root is opened once and pinned; every directory is enumerated with
      ``os.scandir(<pinned fd>)`` and every child is opened *relative to that descriptor* with
      ``O_NOFOLLOW`` (``O_DIRECTORY`` for directories, ``O_NONBLOCK`` for files).  A queued
      directory or file that is swapped for a symlink after enumeration is therefore refused,
      not followed: there is no later path resolution to subvert;
    - each opened child's ``(st_dev, st_ino)`` is compared with the identity of the enumerated
      entry, so a same-size replacement by a *different* inode is reported rather than read;
    - the traversal is a depth-bounded DFS (``MAX_SCAN_DEPTH``) holding one descriptor per level,
      and every descriptor and iterator is closed on success and on error;
    - every attempted read is charged to the total budget, capped by both the per-file cap and the
      remaining budget, and the walk stops when the budget is exhausted.  Actual bytes read can
      never exceed ``MAX_SCAN_TOTAL_BYTES``;
    - after each read the descriptor is ``fstat``-ed again and compared with the enumerated entry,
      so a same-length modification or a growth during the read is reported.  This is **best-effort
      change detection, not an atomic snapshot**: a writer can still race the checks.

    Any read failure, identity change, non-regular file, exhausted budget or depth/entry limit
    marks the snapshot incomplete, so a caller never sees a complete-looking partial result.
    Hidden and cache directories are pruned by name before any open.  On a platform without the
    required no-follow primitives the snapshot fails closed: it is reported incomplete with a
    reason instead of silently opening without those flags.
    """
    try:
        require_no_follow_support()
    except FileAccessError as error:
        return ScanResult(incomplete=True, reason=str(error))
    try:
        # Component-by-component, no-follow pinning of the path exactly as the caller gave it (the
        # resolver already canonicalized it): neither a substituted ancestor nor a substituted root
        # can redirect the walk outside the bundle, and nothing is re-resolved to bless a swap.
        root_descriptor = _pin_directory(Path(directory))
    except (OSError, FileAccessError):
        return ScanResult(incomplete=True, reason="the bundle directory could not be pinned")
    state = _ScanState(budget=MAX_SCAN_TOTAL_BYTES)
    try:
        _walk_directory(root_descriptor, "", 0, state)
    finally:
        os.close(root_descriptor)

    state.files.sort(key=lambda item: item.relative)
    return ScanResult(
        files=tuple(state.files),
        entries=state.entries,
        bytes_read=state.bytes_read,
        symlinks_skipped=state.symlinks_skipped,
        incomplete=state.incomplete,
        reason=state.reason,
        read_failures=tuple(sorted(state.read_failures)),
    )


def _walk_directory(directory_descriptor: int, prefix: str, depth: int, state: _ScanState) -> None:
    if state.stop:
        return
    if depth > MAX_SCAN_DEPTH:
        state.halt(f"directory depth limit {MAX_SCAN_DEPTH} reached")
        return
    try:
        iterator = _scandir(directory_descriptor)
    except OSError:
        state.halt("a directory could not be listed")
        return
    try:
        for entry in iterator:
            if state.stop:
                break
            state.entries += 1
            if state.entries > MAX_SCAN_ENTRIES:
                state.halt(f"entry budget {MAX_SCAN_ENTRIES} reached")
                break
            name = entry.name
            if name.startswith(".") or name in SKIPPED_DIRECTORY_NAMES:
                continue
            relative = f"{prefix}{name}"
            # The dirent type comes from the directory enumeration; stat is authoritative.  A
            # disagreement means the entry changed type between the two (for example a directory
            # replaced by a symlink), which is reported as partial coverage rather than silently
            # skipped.  Platforms without a usable dirent type report neither flag, so an ordinary
            # symlink is simply skipped.
            enumerated_as_entry = entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                state.fail(relative, "not readable")
                continue
            if stat.S_ISLNK(info.st_mode):
                state.symlinks_skipped += 1
                if enumerated_as_entry:
                    state.fail(relative, "changed type during the scan")
                continue
            if stat.S_ISDIR(info.st_mode):
                _descend(directory_descriptor, name, info, relative, depth, state)
                continue
            if not stat.S_ISREG(info.st_mode):
                # A FIFO or device: no content to read, and opening it could block.
                state.fail(relative, "not a regular file")
                continue
            _collect_file(directory_descriptor, name, info, relative, state)
    except OSError:
        # An error raised while the iterator is being consumed must not escape as a raw OSError.
        state.halt("a directory could not be read to completion")
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


def _descend(
    directory_descriptor: int,
    name: str,
    info: os.stat_result,
    relative: str,
    depth: int,
    state: _ScanState,
) -> None:
    """Open a child directory relative to its parent and recurse, closing the descriptor always."""
    try:
        child_descriptor = _open_directory_at(directory_descriptor, name)
    except OSError:
        # Includes the swap case: the entry is now a symlink (ELOOP) or no longer a directory.
        state.symlinks_skipped += 1
        state.fail(relative, "is not a directory any more")
        return
    try:
        opened = os.fstat(child_descriptor)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            state.symlinks_skipped += 1
            state.fail(relative, "changed while it was being opened")
            return
        _walk_directory(child_descriptor, f"{relative}/", depth + 1, state)
    finally:
        os.close(child_descriptor)


def _collect_file(
    directory_descriptor: int,
    name: str,
    info: os.stat_result,
    relative: str,
    state: _ScanState,
) -> None:
    if info.st_size > MAX_SCANNED_FILE_BYTES:
        state.fail(relative, "over the per-file budget")
        return
    if len(state.files) >= MAX_SCANNED_FILES:
        state.halt(f"file budget {MAX_SCANNED_FILES} reached")
        return
    if state.budget <= 0:
        state.halt("total read budget exhausted")
        return
    limit = min(MAX_SCANNED_FILE_BYTES, state.budget)
    try:
        descriptor = _open_file_at(directory_descriptor, name)
    except OSError:
        # Includes the swap case: the entry is now a symlink, or is no longer a regular file.
        state.fail(relative, "could not be opened as a regular file")
        return
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            state.fail(relative, "changed while it was being opened")
            return
        def charge(count: int) -> None:
            """Charge a chunk the moment it was read, so a later failure cannot hide it."""
            state.bytes_read += count
            state.budget -= count

        try:
            payload = _read_bounded(descriptor, limit, charge=charge)
        except OSError:
            # Chunks read before the failure were already charged by the callback.
            state.fail(relative, "could not be read")
            return
        try:
            after = os.fstat(descriptor)
        except OSError:
            state.fail(relative, "could not be checked after reading")
            return
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            state.fail(relative, "was replaced while it was being read")
            return
        if after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
            # Best-effort detection of a same-length write or a growth during the read.
            state.fail(relative, "changed while it was being read")
            return
        if len(payload) != info.st_size:
            state.fail(relative, "changed while it was being read")
            return
        if len(payload) == limit and info.st_size > limit:
            state.fail(relative, "the total read budget was reached mid-file")
            return
        state.files.append(ScanFile(relative=relative, payload=payload, size_at_open=info.st_size))
    finally:
        os.close(descriptor)


def _relative_name(path, directory: Path) -> str:
    try:
        return Path(path).relative_to(directory).as_posix()
    except ValueError:
        return Path(path).name


def fingerprint(scan: ScanResult) -> str | None:
    """A content fingerprint over exactly the files the bounded scan covered.

    Returns ``None`` when the scan was incomplete, so a caller can never present a partial read as
    a complete picture of the bundle.  Hidden and cache directories are never part of the scan, so
    a private file inside the bundle is neither read nor hashed.
    """
    if not scan.complete:
        return None
    digest = hashlib.sha256()
    for item in scan.files:
        digest.update(item.relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.payload).digest())
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    root_resolved = root.resolve()
    return resolved == root_resolved or resolved.is_relative_to(root_resolved)


def _report(
    *,
    ref: str,
    resolved: ResolvedBundle | None,
    errors: list[Finding],
    warnings: list[Finding],
    advisory: list[Finding],
    notable: tuple[str, str] | None = None,
) -> dict[str, Json]:
    not_checked = [{"item": item, "detail": detail} for item, detail in NOT_CHECKED_ENTRIES]
    if notable is not None:
        not_checked.insert(0, {"item": notable[0], "detail": notable[1]})
    checks: list[dict[str, Json]] = []
    for name, status, findings in (
        ("errors", "fail", errors),
        ("warnings", "warn", warnings),
    ):
        for finding in findings:
            checks.append({"name": name, "status": status, "code": finding.code, "detail": finding.detail})
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "static-validation",
        "ref": ref,
        "ok": not errors,
        "enforced": len(errors) == 0,
        "errors": [finding.to_json() for finding in errors],
        "warnings": [finding.to_json() for finding in warnings],
        "advisory": [finding.to_json() for finding in advisory],
        "not_checked": not_checked,
        "checks": checks,
        "resolved": resolved.to_json() if resolved is not None else None,
        "manifest": resolved.manifest.to_json() if resolved is not None and resolved.manifest else None,
    }


def manifest_summary(manifest: BundleManifest) -> Mapping[str, Json]:
    """The stable subset ``jev-loop bundle show`` prints."""
    return {
        "name": manifest.name,
        "version": manifest.version,
        "schema_version": manifest.schema_version,
        "description": manifest.description,
        "when_to_use": manifest.when_to_use,
        "entrypoint": manifest.entrypoint,
        "python_path": manifest.python_path,
        "scaffold": manifest.scaffold,
        "inputs_schema": manifest.inputs_schema,
        "config_schema": manifest.config_schema,
        "output": manifest.output or None,
        "runtime": manifest.runtime or None,
        "dependencies": manifest.dependencies or None,
        "authorizations": list(manifest.authorizations),
        "resources": manifest.resources or None,
        "stop": manifest.stop or None,
        "verification": manifest.verification or None,
        "instructions": str(manifest.directory / INSTRUCTIONS_FILENAME)
        if (manifest.directory / INSTRUCTIONS_FILENAME).is_file()
        else None,
        "bundle_root": str(manifest.directory),
        "discovery_relative": _discovery_relative(manifest),
    }


def _discovery_relative(manifest: BundleManifest) -> str | None:
    """The conventional location, or ``None`` when the bundle is not addressed that way.

    A bundle run by explicit path may live anywhere inside its project root, so claiming a
    discovery-relative path for it would be wrong.
    """
    directory = manifest.directory
    if directory.parent.name != "jev-bundle" or directory.parent.parent.name != ".agents":
        return None
    return f".agents/jev-bundle/{directory.name}/{MANIFEST_FILENAME}"

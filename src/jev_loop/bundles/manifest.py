"""The single manifest implementation shared by the host, the CLI and the doctor.

A bundle manifest is declarative JSON data.  Loading one reads a bounded file and validates
its shape; it never imports bundle code, executes anything, installs a dependency or makes
a network request.  Two formats are understood:

``schema_version: 1`` (legacy)
    ``entrypoint`` (``module:function``), optional ``python_path`` (default ``.``) and
    optional ``config``.  Unknown fields stay tolerated because existing manifests are not
    rewritten by this repository.

``schema_version: 2`` (Jev Bundle Specification, manifest v2)
    A closed field set with bundle metadata, typed input/config contracts and advisory
    declarations.  Unknown fields are rejected instead of being silently ignored.

An unsupported, wrong-typed, missing or explicitly ``null`` ``schema_version`` fails before
any import; there is no default that could silently mask a version.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schema import SchemaValidationError, validate_definition

Json = Any

SUPPORTED_SCHEMA_VERSIONS = (1, 2)
LEGACY_SCHEMA_VERSION = 1
STANDARD_SCHEMA_VERSION = 2
MAX_MANIFEST_BYTES = 256 * 1024
MAX_MANIFEST_DEPTH = 16

BUNDLE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?!-))*[a-z0-9]$|^[a-z0-9]$")
BUNDLE_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
ENTRYPOINT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
CREDENTIAL_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
MAX_NAME_CHARS = 64
MAX_VERSION_CHARS = 64
MAX_TEXT_CHARS = 1024

V1_FIELDS = frozenset({"schema_version", "entrypoint", "python_path", "config"})
V2_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "version",
        "description",
        "when_to_use",
        "entrypoint",
        "python_path",
        "config",
        "inputs_schema",
        "config_schema",
        "output",
        "runtime",
        "dependencies",
        "authorizations",
        "resources",
        "stop",
        "verification",
        "scaffold",
    }
)
V2_REQUIRED_FIELDS = ("schema_version", "name", "version", "description", "entrypoint")
V2_RECOMMENDED_FIELDS = (
    "when_to_use",
    "inputs_schema",
    "output",
    "dependencies",
    "authorizations",
    "resources",
    "stop",
    "verification",
)

READ_CHUNK_BYTES = 64 * 1024
MIN_STOP_GRACE_SECONDS = 0.1
MAX_STOP_GRACE_SECONDS = 30.0


class ManifestError(ValueError):
    """The manifest is unreadable, unsupported or violates the declared format."""


class FileAccessError(OSError):
    """A path could not be used as a bounded regular-file read.

    Raised for a symlink, a non-regular file (a FIFO or device, which must never block a read) or
    a file whose identity changed between the check and the open.
    """


def require_no_follow_support() -> None:
    """Refuse to scan when the platform cannot promise a no-follow open.

    The flags are never silently replaced with zero: without ``O_NOFOLLOW``, ``O_DIRECTORY`` and
    ``dir_fd`` support a bounded snapshot cannot keep its promise, so the caller is told instead.
    """
    missing = [
        name
        for name, present in (
            ("O_NOFOLLOW", hasattr(os, "O_NOFOLLOW")),
            ("O_DIRECTORY", hasattr(os, "O_DIRECTORY")),
            ("O_NONBLOCK", hasattr(os, "O_NONBLOCK")),
            ("open(..., dir_fd=...)", os.open in os.supports_dir_fd),
            ("stat(..., dir_fd=...)", os.stat in os.supports_dir_fd),
            ("os.scandir", hasattr(os, "scandir")),
        )
        if not present
    ]
    if missing:
        raise FileAccessError(f"this platform lacks the no-follow primitives a bounded scan needs: {missing}")


def open_regular_file(path: Path) -> int:
    """Open a regular file for reading without following a symlink, and return the descriptor.

    ``lstat`` first (so a FIFO or device is refused before any open could block), then
    ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` and an ``fstat`` re-check on the descriptor, so a path
    that was swapped between the two checks cannot be followed or block.  This protects the final
    path component; a traversal that must also protect intermediate directories opens children
    relative to a pinned directory descriptor instead.  The caller owns the descriptor.
    """
    require_no_follow_support()
    path = Path(path)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise FileAccessError(f"refusing to follow a symlink: {path.name}")
    if not stat.S_ISREG(info.st_mode):
        raise FileAccessError(f"not a regular file: {path.name}")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileAccessError(f"not a regular file after open: {path.name}")
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise FileAccessError(f"the file changed while it was being opened: {path.name}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_bounded(descriptor: int, limit: int, *, chunk: int = READ_CHUNK_BYTES) -> tuple[bytes, bool]:
    """Read at most ``limit`` bytes and report whether more data was available.

    ``(payload, truncated)``: ``truncated`` is true when the file holds more than ``limit`` bytes,
    which is how a file that grew after its size was checked is detected instead of being read
    without a bound.
    """
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        piece = os.read(descriptor, min(remaining, chunk))
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    payload = b"".join(chunks)
    return payload, len(payload) > limit


@dataclass(frozen=True)
class BundleManifest:
    """A parsed manifest plus the paths derived from it."""

    path: Path
    schema_version: int
    entrypoint: str
    python_path: str
    name: str | None = None
    version: str | None = None
    description: str | None = None
    when_to_use: str | None = None
    config: dict[str, Json] = field(default_factory=dict)
    inputs_schema: dict[str, Json] | None = None
    config_schema: dict[str, Json] | None = None
    output: dict[str, Json] = field(default_factory=dict)
    runtime: dict[str, Json] = field(default_factory=dict)
    dependencies: dict[str, Json] = field(default_factory=dict)
    authorizations: tuple[str, ...] = ()
    resources: dict[str, Json] = field(default_factory=dict)
    stop: dict[str, Json] = field(default_factory=dict)
    verification: dict[str, Json] = field(default_factory=dict)
    scaffold: bool = False
    raw: dict[str, Json] = field(default_factory=dict)

    @property
    def is_standard(self) -> bool:
        """``True`` for manifest v2, the Jev Bundle Specification format."""
        return self.schema_version == STANDARD_SCHEMA_VERSION

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def python_dir(self) -> Path:
        """The directory a v2 manifest may import from, resolved and bounded."""
        return (self.directory / self.python_path).resolve()

    def credential_env_names(self) -> tuple[str, ...]:
        """Credential *variable names* a bundle reads, never any value."""
        return tuple(str(item) for item in self.dependencies.get("credential_env") or ())

    def to_json(self) -> dict[str, Json]:
        return {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "entrypoint": self.entrypoint,
            "python_path": self.python_path,
            "scaffold": self.scaffold,
            "credential_env": list(self.credential_env_names()),
        }


def load_manifest(path: Path) -> BundleManifest:
    """Read and validate one manifest file.  Never imports or executes bundle code.

    The read is bounded and the file must be a regular file: the size is checked from ``stat``
    *and* the read itself is capped, so a file that grows between the two (or a path that is not
    a regular file at all, such as a FIFO or a device, which would otherwise block) cannot turn
    this into an unbounded or blocking read.  Errors are reported with their class name only —
    never the file's content, and never a chained exception carrying it.
    """
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        raise ManifestError(f"cannot read bundle manifest {path}: it is not readable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ManifestError(f"bundle manifest {path} is not a regular file")
    if info.st_size > MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"bundle manifest is {info.st_size} bytes, over the {MAX_MANIFEST_BYTES} byte limit"
        )
    try:
        descriptor = open_regular_file(path)
    except OSError:
        raise ManifestError(f"cannot read bundle manifest {path}: it is not readable") from None
    try:
        payload, truncated = read_bounded(descriptor, MAX_MANIFEST_BYTES)
    except OSError:
        raise ManifestError(f"cannot read bundle manifest {path}: it is not readable") from None
    finally:
        os.close(descriptor)
    if truncated:
        raise ManifestError(f"bundle manifest is over the {MAX_MANIFEST_BYTES} byte limit")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except RecursionError:
        # A deeply nested document overflows the decoder's stack: report it, never leak it.
        raise ManifestError(f"bundle manifest {path} nests too deeply to parse") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ManifestError(f"bundle manifest {path} is not valid UTF-8 JSON") from None
    except ValueError:
        raise ManifestError(f"bundle manifest {path} could not be parsed") from None
    return parse_manifest(raw, path=path)


def parse_manifest(raw: Json, *, path: Path | None = None) -> BundleManifest:
    """Validate an already-parsed manifest object."""
    location = Path(path) if path is not None else Path("<memory>")
    if not isinstance(raw, Mapping):
        raise ManifestError(f"bundle manifest {location} must be a JSON object")
    _check_depth(raw, location)
    _reject_non_finite(raw, location)

    if "schema_version" not in raw:
        raise ManifestError(f"bundle manifest {location} is missing schema_version")
    version = raw["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestError(
            f"bundle manifest {location} schema_version must be the integer 1 or 2, got {_type_name(version)}"
        )
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError(
            f"bundle manifest {location} declares unsupported schema_version {version}; "
            f"supported versions are {list(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    entrypoint = raw.get("entrypoint")
    if version == LEGACY_SCHEMA_VERSION:
        if not isinstance(entrypoint, str) or not ENTRYPOINT_PATTERN.fullmatch(entrypoint):
            raise ManifestError(
                f"bundle manifest {location} entrypoint must be shaped 'module:function' with dotted module names"
            )
    else:
        for key in V2_REQUIRED_FIELDS:
            if key not in raw:
                raise ManifestError(f"bundle manifest {location} is missing required field {key!r}")
        if not isinstance(entrypoint, str) or not ENTRYPOINT_PATTERN.fullmatch(entrypoint):
            raise ManifestError(
                f"bundle manifest {location} entrypoint must be shaped 'module:function' with dotted module names"
            )

    python_path = raw.get("python_path", ".")
    if not isinstance(python_path, str) or not python_path:
        raise ManifestError(f"bundle manifest {location} python_path must be a non-empty string")
    if python_path != python_path.strip():
        raise ManifestError(f"bundle manifest {location} python_path must not have surrounding whitespace")

    config = raw.get("config", {})
    if not isinstance(config, Mapping):
        raise ManifestError(f"bundle manifest {location} config must be a JSON object")

    if version == LEGACY_SCHEMA_VERSION:
        return BundleManifest(
            path=location,
            schema_version=version,
            entrypoint=entrypoint,
            python_path=python_path,
            config=dict(config),
            raw=dict(raw),
        )
    return _parse_standard(raw, location=location, entrypoint=entrypoint, python_path=python_path, config=config)


def _parse_standard(
    raw: Mapping[str, Json],
    *,
    location: Path,
    entrypoint: str,
    python_path: str,
    config: Mapping[str, Json],
) -> BundleManifest:
    unknown = sorted(str(key) for key in raw if key not in V2_FIELDS)
    if unknown:
        raise ManifestError(
            f"bundle manifest {location} has unknown fields {unknown} for schema_version 2; "
            "remove them or correct the version"
        )

    name = _require_identity(raw["name"], location, "name", maximum=MAX_NAME_CHARS)
    if not BUNDLE_NAME_PATTERN.fullmatch(name) or "--" in name:
        raise ManifestError(
            f"bundle manifest {location} name {name!r} must be lowercase letters, digits and single hyphens"
        )
    version_text = _require_identity(raw["version"], location, "version", maximum=MAX_VERSION_CHARS)
    if not BUNDLE_VERSION_PATTERN.fullmatch(version_text):
        raise ManifestError(f"bundle manifest {location} version {version_text!r} must be a simple version token")
    description = _require_text(raw["description"], location, "description", maximum=MAX_TEXT_CHARS)
    when_to_use = _optional_text(raw, location, "when_to_use", maximum=MAX_TEXT_CHARS)

    inputs_schema = _declared_schema(raw, location, "inputs_schema")
    config_schema = _declared_schema(raw, location, "config_schema")

    output = _optional_object(raw, location, "output", allowed={"schema", "evidence", "description"})
    if "schema" in output:
        declared = _optional_schema(output["schema"], location, "output.schema")
        if declared is None:
            raise ManifestError(
                f"bundle manifest {location} output.schema must be an object, not null; omit it instead"
            )
        output["schema"] = declared
    for key in ("evidence", "description"):
        if key in output:
            output[key] = _require_text(output[key], location, f"output.{key}", maximum=MAX_TEXT_CHARS)

    runtime = _optional_object(
        raw,
        location,
        "runtime",
        allowed={"python", "profile", "requires_network", "notes"},
    )
    for key in ("python", "profile", "notes"):
        if key in runtime:
            runtime[key] = _require_text(runtime[key], location, f"runtime.{key}", maximum=MAX_TEXT_CHARS)
    if "requires_network" in runtime and not isinstance(runtime["requires_network"], bool):
        raise ManifestError(f"bundle manifest {location} runtime.requires_network must be a boolean")

    dependencies = _optional_object(
        raw, location, "dependencies", allowed={"python", "system", "credential_env"}
    )
    for key in ("python", "system"):
        if key in dependencies:
            dependencies[key] = _string_list(dependencies[key], location, f"dependencies.{key}")
    credential_env = _string_list(
        dependencies.get("credential_env", []), location, "dependencies.credential_env"
    )
    for position, item in enumerate(credential_env, start=1):
        # The offending entry is deliberately not echoed: a caller may have pasted a real
        # credential here, and this contract only accepts variable names — it cannot tell what
        # any other string is, so it must not copy one into an error, a log or a transcript.
        if not CREDENTIAL_ENV_PATTERN.fullmatch(item):
            raise ManifestError(
                f"bundle manifest {location} dependencies.credential_env entry {position} is not an "
                "environment variable name (uppercase letters, digits and underscores, starting with a "
                "letter); the entry itself is not echoed"
            )
    dependencies["credential_env"] = credential_env

    authorizations = _string_list(raw.get("authorizations", []), location, "authorizations")

    resources = _optional_object(
        raw, location, "resources", allowed={"keys", "exclusive", "notes"}
    )
    resources["keys"] = _string_list(resources.get("keys", []), location, "resources.keys")
    if "exclusive" in resources and not isinstance(resources["exclusive"], bool):
        raise ManifestError(f"bundle manifest {location} resources.exclusive must be a boolean")
    if "notes" in resources:
        resources["notes"] = _require_text(resources["notes"], location, "resources.notes", maximum=MAX_TEXT_CHARS)

    stop = _optional_object(
        raw, location, "stop", allowed={"grace_seconds", "release", "notes"}
    )
    if "grace_seconds" in stop:
        seconds = stop["grace_seconds"]
        # Compare before converting: a huge JSON integer must fail the bound check cleanly
        # instead of raising OverflowError inside float().
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ManifestError(
                f"bundle manifest {location} stop.grace_seconds must be a number between 0.1 and 30"
            )
        if isinstance(seconds, float) and not math.isfinite(seconds):
            raise ManifestError(
                f"bundle manifest {location} stop.grace_seconds must be a number between 0.1 and 30"
            )
        if seconds < MIN_STOP_GRACE_SECONDS or seconds > MAX_STOP_GRACE_SECONDS:
            raise ManifestError(
                f"bundle manifest {location} stop.grace_seconds must be a number between 0.1 and 30"
            )
        stop["grace_seconds"] = float(seconds)
    for key in ("release", "notes"):
        if key in stop:
            stop[key] = _require_text(stop[key], location, f"stop.{key}", maximum=MAX_TEXT_CHARS)

    verification = _optional_object(
        raw, location, "verification", allowed={"independent", "evidence", "notes"}
    )
    if "independent" in verification and not isinstance(verification["independent"], bool):
        raise ManifestError(f"bundle manifest {location} verification.independent must be a boolean")
    for key in ("evidence", "notes"):
        if key in verification:
            verification[key] = _require_text(
                verification[key], location, f"verification.{key}", maximum=MAX_TEXT_CHARS
            )

    scaffold = raw.get("scaffold", False)
    if not isinstance(scaffold, bool):
        raise ManifestError(f"bundle manifest {location} scaffold must be a boolean")

    return BundleManifest(
        path=location,
        schema_version=STANDARD_SCHEMA_VERSION,
        name=name,
        version=version_text,
        description=description,
        when_to_use=when_to_use,
        entrypoint=entrypoint,
        python_path=python_path,
        config=dict(config),
        inputs_schema=inputs_schema,
        config_schema=config_schema,
        output=output,
        runtime=runtime,
        dependencies=dependencies,
        authorizations=tuple(authorizations),
        resources=resources,
        stop=stop,
        verification=verification,
        scaffold=scaffold,
        raw=dict(raw),
    )


def _declared_schema(raw: Mapping[str, Json], location: Path, label: str) -> dict[str, Json] | None:
    """Absent means "not declared"; present with null is an error, because a null contract is not a contract."""
    if label not in raw:
        return None
    return _optional_schema(raw[label], location, label)


def _optional_schema(value: Json, location: Path, label: str) -> dict[str, Json] | None:
    """An absent schema differs from an explicit ``null``; a declared schema must be enforceable."""
    if value is None:
        raise ManifestError(f"bundle manifest {location} {label} must be an object, not null; omit it instead")
    if not isinstance(value, Mapping):
        raise ManifestError(f"bundle manifest {location} {label} must be an object")
    try:
        validate_definition(value)
    except SchemaValidationError as error:
        raise ManifestError(f"bundle manifest {location} {label} is not a supported schema: {error}") from error
    return dict(value)


def _optional_object(
    raw: Mapping[str, Json],
    location: Path,
    label: str,
    *,
    allowed: frozenset[str] | set[str],
) -> dict[str, Json]:
    """An omitted optional object is empty; a *present* one must be an object of the declared type.

    ``null`` is not the same as omission: a manifest that spells out ``"output": null`` has
    declared nothing usable, and the v2 contract refuses it instead of inventing an empty object.
    """
    if label not in raw:
        return {}
    value = raw[label]
    if not isinstance(value, Mapping):
        raise ManifestError(
            f"bundle manifest {location} {label} must be an object, not {_type_name(value)}"
        )
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ManifestError(
            f"bundle manifest {location} {label} has unknown fields {unknown}; allowed fields are {sorted(allowed)}"
        )
    return dict(value)


def _require_text(value: Json, location: Path, label: str, *, maximum: int) -> str:
    """A prose field: non-empty, and never silently normalised.

    Surrounding whitespace is rejected rather than trimmed, so the value a reader sees is the
    value that was declared (the machine-readable schema states the same rule).
    """
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"bundle manifest {location} {label} must be a non-empty string")
    if value != value.strip():
        raise ManifestError(f"bundle manifest {location} {label} must not have surrounding whitespace")
    if len(value) > maximum:
        raise ManifestError(f"bundle manifest {location} {label} exceeds {maximum} characters")
    return value


def _require_identity(value: Json, location: Path, label: str, *, maximum: int) -> str:
    """Identity fields (name, version): never normalised into a different valid value.

    A trailing newline, a tab or any other surrounding whitespace is an error rather than being
    silently trimmed into something that happens to match — otherwise two different manifests
    could collapse onto one identity, or an invalid one could be accepted as valid.
    """
    if not isinstance(value, str) or not value:
        raise ManifestError(f"bundle manifest {location} {label} must be a non-empty string")
    if value != value.strip():
        raise ManifestError(
            f"bundle manifest {location} {label} must not have surrounding whitespace"
        )
    if len(value) > maximum:
        raise ManifestError(f"bundle manifest {location} {label} exceeds {maximum} characters")
    return value


def _optional_text(
    raw: Mapping[str, Json],
    location: Path,
    label: str,
    *,
    maximum: int,
) -> str | None:
    """An omitted optional text is absent; a present one must be a non-empty string."""
    if label not in raw:
        return None
    return _require_text(raw[label], location, label, maximum=maximum)


def _string_list(value: Json, location: Path, label: str) -> list[str]:
    """A list of exact strings: an entry is never silently trimmed into a different value."""
    if not isinstance(value, list):
        raise ManifestError(f"bundle manifest {location} {label} must be an array of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ManifestError(f"bundle manifest {location} {label} must contain non-empty strings")
        if item != item.strip():
            raise ManifestError(
                f"bundle manifest {location} {label} entries must not have surrounding whitespace"
            )
        if len(item) > MAX_TEXT_CHARS:
            raise ManifestError(
                f"bundle manifest {location} {label} entries must stay under {MAX_TEXT_CHARS} characters"
            )
        if item not in items:
            items.append(item)
    return items


def _check_depth(value: Json, location: Path, *, _depth: int = 0) -> None:
    if _depth > MAX_MANIFEST_DEPTH:
        raise ManifestError(f"bundle manifest {location} nests deeper than {MAX_MANIFEST_DEPTH} levels")
    if isinstance(value, Mapping):
        for item in value.values():
            _check_depth(item, location, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, location, _depth=_depth + 1)


def _reject_non_finite(value: Json, location: Path, *, _depth: int = 0) -> None:
    if _depth > 64:
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ManifestError(f"bundle manifest {location} must contain only finite numbers")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item, location, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item, location, _depth=_depth + 1)


def _type_name(value: Json) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__

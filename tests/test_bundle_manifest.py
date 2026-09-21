"""Manifest rules for the Jev Bundle Specification (manifest v2) and legacy v1."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jev_loop.bundles.manifest import (
    MAX_MANIFEST_BYTES,
    STANDARD_SCHEMA_VERSION,
    BundleManifest,
    ManifestError,
    load_manifest,
    parse_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
VALID_EXAMPLE = REPO_ROOT / "spec" / "bundle-examples" / "manifest-v2-valid.json"
LEGACY_EXAMPLE = REPO_ROOT / "spec" / "bundle-examples" / "manifest-v1-legacy.json"


def standard(**overrides) -> dict:
    manifest = {
        "schema_version": 2,
        "name": "example-notes",
        "version": "0.1.0",
        "description": "an example bundle",
        "entrypoint": "bundle_controller:build",
    }
    manifest.update(overrides)
    return manifest


def test_legacy_v1_manifest_is_accepted_verbatim() -> None:
    manifest = parse_manifest(json.loads(LEGACY_EXAMPLE.read_text(encoding="utf-8")))
    assert manifest.schema_version == 1
    assert manifest.is_standard is False
    assert manifest.name is None
    assert manifest.entrypoint == "controller:build"
    assert manifest.config == {}


def test_v1_tolerates_fields_this_specification_reserves_for_v2() -> None:
    manifest = parse_manifest({"schema_version": 1, "entrypoint": "controller:build", "notes": "custom"})
    assert manifest.schema_version == 1


def test_standard_example_parses_into_the_declared_contract() -> None:
    manifest = load_manifest(VALID_EXAMPLE)
    assert isinstance(manifest, BundleManifest)
    assert manifest.is_standard is True
    assert manifest.name == "example-notes"
    assert manifest.version == "0.3.1"
    assert manifest.credential_env_names() == ("TYPESAFE_API_KEY",)
    assert manifest.inputs_schema is not None and manifest.inputs_schema["required"] == ["topic"]
    assert manifest.config == {"max_steps": 8, "mode": "draft"}
    assert manifest.scaffold is False


def test_a_directory_without_the_manifest_fails_before_any_import(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="cannot read bundle manifest"):
        load_manifest(tmp_path / "missing.json")


def test_an_oversized_manifest_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(standard(description="x" * 100)) + " " * (300 * 1024), encoding="utf-8")
    with pytest.raises(ManifestError, match="over the"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"entrypoint": "controller:build"}, "missing schema_version"),
        ({"schema_version": None, "entrypoint": "c:b"}, "must be the integer 1 or 2"),
        ({"schema_version": "2", "entrypoint": "c:b"}, "must be the integer 1 or 2"),
        ({"schema_version": True, "entrypoint": "c:b"}, "must be the integer 1 or 2"),
        ({"schema_version": 2.0, "entrypoint": "c:b"}, "must be the integer 1 or 2"),
        ({"schema_version": 3, "entrypoint": "c:b"}, "unsupported schema_version 3"),
        ({"schema_version": 0, "entrypoint": "c:b"}, "unsupported schema_version 0"),
    ],
)
def test_the_manifest_version_is_never_defaulted_or_coerced(payload: dict, expected: str) -> None:
    with pytest.raises(ManifestError, match=expected):
        parse_manifest(payload)


@pytest.mark.parametrize(
    "entrypoint",
    ["controller", ":build", "controller:", "1module:build", "controller:build:extra", "controller.build-x:go"],
)
def test_the_entrypoint_must_be_module_colon_function(entrypoint: str) -> None:
    with pytest.raises(ManifestError, match="entrypoint"):
        parse_manifest(standard(entrypoint=entrypoint))


def test_a_dotted_entrypoint_module_is_valid() -> None:
    assert parse_manifest(standard(entrypoint="pkg.controller:build")).entrypoint == "pkg.controller:build"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("name", "Upper", "must be lowercase"),
        ("name", "double--hyphen", "must be lowercase"),
        ("name", "-leading", "must be lowercase"),
        ("name", "trailing-", "must be lowercase"),
        ("name", "a" * 65, "exceeds 64 characters"),
        ("version", "1.0.0 beta", "simple version token"),
        ("description", "  ", "must be a non-empty string"),
    ],
)
def test_v2_metadata_is_enforced(field: str, value: object, expected: str) -> None:
    with pytest.raises(ManifestError, match=expected):
        parse_manifest(standard(**{field: value}))


def test_v2_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ManifestError, match=r"unknown fields \['entry_point'\]"):
        parse_manifest(standard(entry_point="x"))


@pytest.mark.parametrize("field", ["name", "version", "description", "entrypoint"])
def test_v2_required_fields_are_named_in_the_error(field: str) -> None:
    payload = standard()
    payload.pop(field)
    with pytest.raises(ManifestError, match=f"missing required field '{field}'"):
        parse_manifest(payload)


def test_an_omitted_schema_differs_from_an_explicit_null() -> None:
    assert parse_manifest(standard()).inputs_schema is None
    with pytest.raises(ManifestError, match="inputs_schema must be an object, not null"):
        parse_manifest(standard(inputs_schema=None))
    with pytest.raises(ManifestError, match="config_schema must be an object, not null"):
        parse_manifest(standard(config_schema=None))
    with pytest.raises(ManifestError, match="output.schema must be an object, not null"):
        parse_manifest(standard(output={"schema": None}))


def test_a_declared_schema_must_be_enforceable() -> None:
    with pytest.raises(ManifestError, match="not a supported schema"):
        parse_manifest(standard(inputs_schema={"oneOf": [{"type": "string"}]}))
    with pytest.raises(ManifestError, match="not a supported schema"):
        parse_manifest(
            standard(inputs_schema={"type": "object", "additionalProperties": {"type": "string"}})
        )


def test_credential_environment_entries_are_variable_names_only() -> None:
    assert parse_manifest(standard(dependencies={"credential_env": ["MY_API_KEY"]})).credential_env_names() == (
        "MY_API_KEY",
    )
    for invalid in ("my_key", "KEY=value", "KEY: value", "sk-live-abcdefghijklmnopqrstuvwxyz", "1KEY"):
        with pytest.raises(ManifestError) as caught:
            parse_manifest(standard(dependencies={"credential_env": [invalid]}))
        message = str(caught.value)
        assert "is not an environment variable name" in message
        # The offending entry is never echoed: it could be a real credential a caller pasted.
        assert invalid not in message, (invalid, message)
        assert "entry 1" in message
    # Surrounding whitespace is refused outright instead of being trimmed into a valid name.
    with pytest.raises(ManifestError, match="must not have surrounding whitespace") as whitespace:
        parse_manifest(standard(dependencies={"credential_env": ["KEY\n"]}))
    assert "KEY" not in str(whitespace.value).replace("whitespace", "")


def test_deeply_nested_manifests_and_schemas_are_refused() -> None:
    nested: dict = {"leaf": True}
    for _ in range(24):
        nested = {"next": nested}
    with pytest.raises(ManifestError, match="nests deeper"):
        parse_manifest(standard(config=nested))

    schema: dict = {"type": "object"}
    current = schema
    for _ in range(24):
        current["properties"] = {"next": {"type": "object"}}
        current = current["properties"]["next"]
    with pytest.raises(ManifestError, match="nests deeper"):
        parse_manifest(standard(inputs_schema=schema))


def test_non_finite_numbers_are_refused() -> None:
    with pytest.raises(ManifestError, match="only finite numbers"):
        parse_manifest(standard(config={"timeout": float("inf")}))
    with pytest.raises(ManifestError, match="only finite numbers"):
        parse_manifest(standard(config={"timeout": float("nan")}))


def test_unknown_v2_sub_object_fields_are_rejected() -> None:
    with pytest.raises(ManifestError, match="runtime has unknown fields"):
        parse_manifest(standard(runtime={"python": ">=3.12", "cpus": 2}))
    with pytest.raises(ManifestError, match="stop.grace_seconds must be a number between"):
        parse_manifest(standard(stop={"grace_seconds": 60}))
    with pytest.raises(ManifestError, match="scaffold must be a boolean"):
        parse_manifest(standard(scaffold="yes"))


def test_config_must_be_an_object() -> None:
    with pytest.raises(ManifestError, match="config must be a JSON object"):
        parse_manifest(standard(config=[1, 2]))


def test_python_path_must_be_a_non_empty_string() -> None:
    with pytest.raises(ManifestError, match="python_path must be a non-empty string"):
        parse_manifest(standard(python_path=""))
    with pytest.raises(ManifestError, match="python_path must not have surrounding whitespace"):
        parse_manifest(standard(python_path="  "))
    assert parse_manifest(standard()).python_path == "."
    assert parse_manifest(standard()).schema_version == STANDARD_SCHEMA_VERSION


def test_identity_fields_are_matched_exactly_and_never_normalised() -> None:
    """A trailing newline must fail, not be trimmed into a different valid identity."""
    for payload, expected in (
        (standard(name="good-name\n"), "name must not have surrounding whitespace"),
        (standard(version="1.0.0\n"), "version must not have surrounding whitespace"),
        (standard(name="good-name "), "name must not have surrounding whitespace"),
        (standard(entrypoint="bundle_controller:build\n"), "entrypoint must be shaped"),
        (standard(name="good-name\t"), "name must not have surrounding whitespace"),
    ):
        with pytest.raises(ManifestError, match=expected):
            parse_manifest(payload)
    # Prose fields are exact too: whitespace is rejected, never trimmed into a different value.
    with pytest.raises(ManifestError, match="description must not have surrounding whitespace"):
        parse_manifest(standard(description="  a bundle  "))
    with pytest.raises(ManifestError, match="when_to_use must not have surrounding whitespace"):
        parse_manifest(standard(when_to_use="  somewhere  "))
    assert parse_manifest(standard(description="a bundle")).description == "a bundle"
    # And list entries are exact too.
    with pytest.raises(ManifestError, match="entries must not have surrounding whitespace"):
        parse_manifest(standard(authorizations=["notes.write\n"]))


def test_the_manifest_read_is_bounded_and_refuses_non_regular_files(tmp_path: Path) -> None:
    import os

    directory = tmp_path / "oversized"
    directory.mkdir()
    manifest = directory / "bundle.json"
    manifest.write_text(json.dumps(standard(name="oversized")), encoding="utf-8")
    with manifest.open("ab") as handle:
        handle.write(b" " * (MAX_MANIFEST_BYTES + 10))
    with pytest.raises(ManifestError, match="over the"):
        load_manifest(manifest)

    fifo = directory / "fifo.json"
    os.mkfifo(fifo)
    # A non-regular file must be refused immediately instead of blocking the read.
    with pytest.raises(ManifestError, match="not a regular file"):
        load_manifest(fifo)


def test_unreadable_and_unparseable_manifests_report_no_content(tmp_path: Path) -> None:
    sentinel = "MANIFEST-SENTINEL-DO-NOT-ECHO"

    directory = tmp_path / "broken"
    directory.mkdir()
    broken = directory / "bundle.json"
    broken.write_text("{" + sentinel, encoding="utf-8")
    with pytest.raises(ManifestError) as caught:
        load_manifest(broken)
    assert sentinel not in str(caught.value)
    # `from None` marks the original exception as suppressed: no chained content reaches a report.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True

    deep = directory / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(ManifestError, match="nests too deeply"):
        load_manifest(deep)

    binary = directory / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00" + sentinel.encode())
    with pytest.raises(ManifestError, match="not valid UTF-8 JSON") as invalid:
        load_manifest(binary)
    assert sentinel not in str(invalid.value)


def test_a_missing_manifest_reports_no_filesystem_detail(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as caught:
        load_manifest(tmp_path / "absent.json")
    assert "not readable" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_a_huge_grace_seconds_is_refused_without_an_overflow() -> None:
    """A huge JSON integer must be a clean ManifestError, never an OverflowError."""
    for value in (10**400, 10**30, -10**400):
        with pytest.raises(ManifestError, match="stop.grace_seconds must be a number between 0.1 and 30"):
            parse_manifest(standard(stop={"grace_seconds": value}))
    # In-range values keep working, including exact ints and floats.
    assert parse_manifest(standard(stop={"grace_seconds": 3})).stop["grace_seconds"] == 3.0
    assert parse_manifest(standard(stop={"grace_seconds": 0.1})).stop["grace_seconds"] == 0.1
    with pytest.raises(ManifestError, match="stop.grace_seconds must be a number between"):
        parse_manifest(standard(stop={"grace_seconds": 30.0001}))
    # A non-finite number is refused even earlier, by the manifest-wide finite-number rule.
    with pytest.raises(ManifestError, match="only finite numbers"):
        parse_manifest(standard(stop={"grace_seconds": float("inf")}))


def test_the_huge_grace_seconds_error_is_safe_through_the_cli_and_the_doctor(tmp_path: Path) -> None:
    import subprocess
    import sys

    directory = tmp_path / "huge-grace"
    directory.mkdir()
    manifest = directory / "bundle.json"
    manifest.write_text(
        '{"schema_version": 2, "name": "huge-grace", "version": "1.0.0", "description": "d",'
        ' "entrypoint": "bundle_controller:build", "stop": {"grace_seconds": 1' + "0" * 400 + "}}",
        encoding="utf-8",
    )
    (directory / "BUNDLE.md").write_text("# x\n", encoding="utf-8")

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(item for item in sys.path if item)
    for argv in (
        [sys.executable, "-m", "jev_loop.cli", "bundle", "validate", str(manifest), "--json",
         "--project-root", str(tmp_path)],
        [sys.executable, "-m", "jev_loop.doctor", "--offline", "--json", "--bundle", str(manifest)],
    ):
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, env=environment)
        combined = completed.stdout + completed.stderr
        assert "OverflowError" not in combined, argv
        assert "Traceback" not in combined, argv
        assert "grace_seconds" in combined, argv


def test_the_removed_credential_heuristic_is_really_gone() -> None:
    """The warning that interpolated the suspected value must not exist anywhere."""
    source = (Path(__file__).resolve().parent.parent / "src" / "jev_loop" / "bundles" / "validate.py").read_text(
        encoding="utf-8"
    )
    assert "credential_env_looks_like_value" not in source
    assert "may be a pasted value" not in source

    # A name that would have tripped the old heuristic is now simply valid metadata.
    manifest = parse_manifest(standard(dependencies={"credential_env": ["TYPESAFEAPIKEY0123456789"]}))
    assert manifest.credential_env_names() == ("TYPESAFEAPIKEY0123456789",)

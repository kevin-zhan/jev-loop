"""Static validation: enforced, warning, advisory and not-checked are kept apart."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jev_loop.bundles.validate import validate_manifest_path, validate_ref

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_BUNDLE = REPO_ROOT / ".agents" / "jev-bundle" / "offline-switchboard"
EXAMPLE_MANIFEST = REPO_ROOT / "examples" / "bundles" / "switchboard" / "bundle.json"


def codes(findings: list[dict]) -> set[str]:
    return {finding["code"] for finding in findings}


def test_the_reference_bundle_validates_without_errors_or_warnings() -> None:
    report = validate_ref("project:offline-switchboard", REPO_ROOT)
    assert report["ok"] is True
    assert report["errors"] == [] and report["warnings"] == []
    assert report["mode"] == "static-validation"
    assert report["manifest"]["name"] == "offline-switchboard"
    assert report["manifest"]["schema_version"] == 2


def test_advisory_declarations_say_what_they_do_not_do() -> None:
    report = validate_ref("project:offline-switchboard", REPO_ROOT)
    assert report["advisory"], "the reference declares advisory metadata"
    assert all("does not grant a permission" in item["detail"] for item in report["advisory"])
    assert {"stop", "verification", "output"} <= codes(report["advisory"])


def test_runtime_semantics_and_the_factory_shape_are_never_claimed() -> None:
    report = validate_ref("project:offline-switchboard", REPO_ROOT)
    items = {entry["item"] for entry in report["not_checked"]}
    assert {
        "factory_and_controller",
        "runtime_semantics",
        "resource_exclusivity",
        "dependency_availability",
        "credential_presence",
        "network_and_device",
    } <= items


def test_the_diagnostic_bundle_is_reported_as_a_probe(tmp_path: Path) -> None:
    report = validate_ref("diagnostic", REPO_ROOT)
    assert report["ok"] is True
    assert {entry["item"] for entry in report["not_checked"]} >= {"diagnostic"}


def test_a_legacy_manifest_validates_by_path_with_an_explicit_advisory(tmp_path: Path) -> None:
    report = validate_manifest_path(EXAMPLE_MANIFEST, project_root=REPO_ROOT)
    assert report["ok"] is True
    assert "legacy_manifest" in codes(report["warnings"])
    assert "legacy_contract" in codes(report["advisory"])


def test_an_unresolvable_reference_becomes_a_report_error(tmp_path: Path) -> None:
    report = validate_ref("project:not-there", tmp_path)
    assert report["ok"] is False
    assert codes(report["errors"]) == {"unresolved_reference"}
    assert report["resolved"] is None


def test_an_unparseable_manifest_becomes_a_report_error(tmp_path: Path) -> None:
    manifest = tmp_path / "bundle.json"
    manifest.write_text("{broken", encoding="utf-8")
    report = validate_manifest_path(manifest)
    assert report["ok"] is False and codes(report["errors"]) == {"unparseable_manifest"}


def make_bundle(project: Path, name: str, manifest: dict, *, instructions: bool = True) -> Path:
    directory = project / ".agents" / "jev-bundle" / name
    directory.mkdir(parents=True)
    (directory / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    if instructions:
        (directory / "BUNDLE.md").write_text("# x\n", encoding="utf-8")
    return directory


def standard(name: str, **overrides) -> dict:
    return {
        "schema_version": 2,
        "name": name,
        "version": "0.1.0",
        "description": "validation fixture",
        "entrypoint": "bundle_controller:build",
        **overrides,
    }


def test_missing_instructions_is_an_error(tmp_path: Path) -> None:
    directory = make_bundle(tmp_path, "no-instructions", standard("no-instructions"), instructions=False)
    report = validate_ref("project:no-instructions", tmp_path)
    assert report["ok"] is False
    assert "missing BUNDLE.md" in report["errors"][0]["detail"]

    by_path = validate_manifest_path(directory / "bundle.json", project_root=tmp_path)
    assert "missing_instructions" in codes(by_path["errors"])


def test_a_missing_recommended_field_is_only_a_warning(tmp_path: Path) -> None:
    make_bundle(tmp_path, "sparse", standard("sparse"))
    report = validate_ref("project:sparse", tmp_path)
    assert report["ok"] is True
    assert "incomplete_metadata" in codes(report["warnings"])


def test_config_defaults_that_contradict_the_declared_schema_are_an_error(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        "bad-defaults",
        standard(
            "bad-defaults",
            config={"max_steps": 999},
            config_schema={"type": "object", "properties": {"max_steps": {"type": "integer", "maximum": 64}}},
        ),
    )
    report = validate_ref("project:bad-defaults", tmp_path)
    assert "config_defaults_invalid" in codes(report["errors"])


def test_a_scaffold_validates_with_a_warning_that_says_starts_are_refused(tmp_path: Path) -> None:
    make_bundle(tmp_path, "unfinished", standard("unfinished", scaffold=True))
    report = validate_ref("project:unfinished", tmp_path)
    assert report["ok"] is True
    warning = next(item for item in report["warnings"] if item["code"] == "unfinished_scaffold")
    assert "refuses name/path starts" in warning["detail"]


def test_unrendered_template_tokens_are_errors_but_prose_todos_are_not(tmp_path: Path) -> None:
    make_bundle(tmp_path, "rendered", standard("rendered", description="TODO: finish the adapter"))
    report = validate_ref("project:rendered", tmp_path)
    assert report["ok"] is True, report["errors"]

    make_bundle(tmp_path, "unrendered", standard("unrendered", description="__BUNDLE_NAME__ bundle"))
    unrendered = validate_ref("project:unrendered", tmp_path)
    assert "unrendered_template_token" in codes(unrendered["errors"])


def test_a_declared_variable_name_is_metadata_not_a_suspicion(tmp_path: Path) -> None:
    """The old "looks like a pasted value" heuristic is gone: it printed the suspected value.

    A syntactically valid name stays metadata (the advisory entry names it as a declaration).  What
    must never happen is a *diagnostic* message echoing a suspected value.
    """
    name = "TYPESAFEAPIKEY0123456789"
    make_bundle(tmp_path, "suspect-key", standard("suspect-key", dependencies={"credential_env": [name]}))
    report = validate_ref("project:suspect-key", tmp_path)

    assert "credential_env_looks_like_value" not in codes(report["warnings"])
    for key in ("warnings", "errors"):
        assert name not in json.dumps(report[key]), key
    declared = [item for item in report["advisory"] if item["code"] == "dependencies.credential_env"]
    assert declared and name in declared[0]["detail"], "a valid declared name is legitimate metadata"


def test_an_entrypoint_module_that_cannot_be_found_warns(tmp_path: Path) -> None:
    make_bundle(tmp_path, "no-module", standard("no-module", entrypoint="absent_module:build"))
    report = validate_ref("project:no-module", tmp_path)
    assert report["ok"] is True
    assert "entrypoint_module_not_found" in codes(report["warnings"])


def test_a_python_path_escaping_the_bundle_is_an_error_even_by_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    directory = project / "external-bundle"
    directory.mkdir(parents=True)
    (directory / "bundle.json").write_text(json.dumps(standard("external-bundle", python_path="..")), encoding="utf-8")
    (directory / "BUNDLE.md").write_text("# x\n", encoding="utf-8")
    report = validate_manifest_path(directory / "bundle.json", project_root=project)
    assert "python_path_escapes_bundle" in codes(report["errors"])


def test_validation_reports_are_free_of_credential_looking_values(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        "declares-key",
        standard("declares-key", dependencies={"credential_env": ["TYPESAFE_API_KEY"]}),
    )
    report = validate_ref("project:declares-key", tmp_path)
    rendered = json.dumps(report)
    assert "TYPESAFE_API_KEY" in rendered  # the name is the declaration
    credential_advisory = next(
        item["detail"] for item in report["advisory"] if item["code"] == "dependencies.credential_env"
    )
    assert "=" not in credential_advisory

# --- bounded, symlink-safe token scan (the static gate must never read outside the bundle) --------


def _standard_bundle(project: Path, name: str, **overrides) -> Path:
    return make_bundle(project, name, standard(name, **overrides))


def test_the_token_scan_never_reads_a_file_outside_the_bundle(tmp_path: Path) -> None:
    """A symlink inside the bundle must not become a read of a private file elsewhere."""
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("__BUNDLE_NAME__ credential-material-do-not-read\n", encoding="utf-8")
    directory = _standard_bundle(tmp_path, "linked-tokens")
    (directory / "notes.txt").symlink_to(secret)
    (directory / "linked-dir").symlink_to(tmp_path)

    report = validate_ref("project:linked-tokens", tmp_path)
    # The sentinel token lives only in the external file: if the scan followed the link it would be
    # reported as an unrendered token (and the file would have been read).
    assert report["ok"] is True, report["errors"]
    assert "unrendered_template_token" not in codes(report["errors"])
    assert "symlinks_not_scanned" in codes(report["warnings"])


def test_a_required_instruction_file_may_not_point_outside_the_bundle(tmp_path: Path) -> None:
    outside = tmp_path / "outside-instructions.md"
    outside.write_text("# not mine\n", encoding="utf-8")
    directory = _standard_bundle(tmp_path, "linked-instructions")
    (directory / "BUNDLE.md").unlink()
    (directory / "BUNDLE.md").symlink_to(outside)

    report = validate_ref("project:linked-instructions", tmp_path)
    assert "instructions_outside_bundle" in codes(report["errors"])


def test_the_scan_is_bounded_and_says_so_instead_of_claiming_completeness(tmp_path: Path) -> None:
    """A huge tree must not be walked unbounded, and the report must admit the gap."""
    from jev_loop.bundles import validate as validate_module

    directory = _standard_bundle(tmp_path, "huge-bundle")
    big = directory / "many"
    big.mkdir()
    for index in range(validate_module.MAX_SCAN_ENTRIES + 50):
        (big / f"file-{index}.txt").write_text("plain\n", encoding="utf-8")

    report = validate_ref("project:huge-bundle", tmp_path)
    assert report["ok"] is True, report["errors"]
    incomplete = next((item for item in report["warnings"] if item["code"] == "token_scan_incomplete"), None)
    assert incomplete is not None, report["warnings"]
    assert any(
        phrase in incomplete["detail"] for phrase in ("entry budget", "read budget", "file budget")
    ), incomplete["detail"]
    assert report["mode"] == "static-validation"
    assert report["not_checked"], "an incomplete scan must still publish what it did not check"


def test_hidden_and_cache_directories_are_pruned_before_descending(tmp_path: Path) -> None:
    directory = _standard_bundle(tmp_path, "pruned")
    for name in (".git", "__pycache__", "node_modules", ".venv"):
        nested = directory / name / "deep"
        nested.mkdir(parents=True)
        (nested / "token.txt").write_text("__BUNDLE_NAME__\n", encoding="utf-8")

    report = validate_ref("project:pruned", tmp_path)
    assert report["ok"] is True, report["errors"]
    assert "unrendered_template_token" not in codes(report["errors"])


def test_a_token_in_an_ordinary_file_is_still_found(tmp_path: Path) -> None:
    directory = _standard_bundle(tmp_path, "token-here")
    (directory / "notes.txt").write_text("name: __BUNDLE_NAME__\n", encoding="utf-8")
    report = validate_ref("project:token-here", tmp_path)
    assert "unrendered_template_token" in codes(report["errors"])


# --- config defaults are checked per declared field, not as a complete config --------------------


def test_partial_defaults_pass_the_static_check_when_the_caller_supplies_the_rest(tmp_path: Path) -> None:
    _standard_bundle(
        tmp_path,
        "partial-defaults",
        config={"a": 1},
        config_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    report = validate_ref("project:partial-defaults", tmp_path)
    assert report["ok"] is True, report["errors"]


def test_declared_defaults_are_still_checked_field_by_field(tmp_path: Path) -> None:
    _standard_bundle(
        tmp_path,
        "bad-default-type",
        config={"a": "not-an-integer"},
        config_schema={"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
    )
    report = validate_ref("project:bad-default-type", tmp_path)
    assert "config_defaults_invalid" in codes(report["errors"])


def test_an_undeclared_default_key_is_still_refused_statically(tmp_path: Path) -> None:
    _standard_bundle(
        tmp_path,
        "extra-default",
        config={"a": 1, "surprise": 2},
        config_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        },
    )
    report = validate_ref("project:extra-default", tmp_path)
    assert "config_defaults_invalid" in codes(report["errors"])


def test_a_declared_object_shape_rejects_a_non_object_value(tmp_path: Path) -> None:
    from jev_loop.schema import SchemaValidationError, validate_declared_defaults

    with pytest.raises(SchemaValidationError, match="must have type object"):
        validate_declared_defaults([1], {"properties": {"a": {"type": "integer"}}})
    validate_declared_defaults({"a": 1}, {"properties": {"a": {"type": "integer"}}})


def test_a_v2_manifest_must_match_its_directory_even_by_explicit_path(tmp_path: Path) -> None:
    """The name/directory contract is not bypassed by addressing the manifest by path."""
    project = tmp_path / "project"
    directory = project / "some-directory"
    directory.mkdir(parents=True)
    (directory / "bundle.json").write_text(
        json.dumps(standard("a-different-name")), encoding="utf-8"
    )
    (directory / "BUNDLE.md").write_text("# x\n", encoding="utf-8")

    report = validate_manifest_path(directory / "bundle.json", project_root=project)
    assert "name_directory_mismatch" in codes(report["errors"])
    assert report["ok"] is False

    # Matching name and directory stays valid however it is addressed.
    (directory / "bundle.json").write_text(json.dumps(standard("some-directory")), encoding="utf-8")
    assert validate_manifest_path(directory / "bundle.json", project_root=project)["ok"] is True


def test_legacy_v1_is_not_retrofitted_with_the_name_contract(tmp_path: Path) -> None:
    project = tmp_path / "project"
    directory = project / "legacy-dir"
    directory.mkdir(parents=True)
    (directory / "bundle.json").write_text(
        json.dumps({"schema_version": 1, "entrypoint": "controller:build", "python_path": "."}),
        encoding="utf-8",
    )
    report = validate_manifest_path(directory / "bundle.json", project_root=project)
    assert report["ok"] is True
    assert "name_directory_mismatch" not in codes(report["errors"])


def test_no_suspected_credential_value_is_echoed_anywhere_in_a_report(tmp_path: Path) -> None:
    """A pasted credential must not appear in a report, an error or a warning."""
    sentinel = "sk-live-SENTINEL-DO-NOT-ECHO-0123456789"
    project = tmp_path / "project"
    directory = project / ".agents" / "jev-bundle" / "leaky"
    directory.mkdir(parents=True)
    (directory / "bundle.json").write_text(
        json.dumps(standard("leaky", dependencies={"credential_env": [sentinel, "KEY=value"]})),
        encoding="utf-8",
    )
    (directory / "BUNDLE.md").write_text("# x\n", encoding="utf-8")

    report = validate_ref("project:leaky", project)
    rendered = json.dumps(report)
    assert report["ok"] is False
    assert sentinel not in rendered and "KEY=value" not in rendered
    # The failing position is named, the value never is (the first failure stops the parse).
    assert "entry 1" in rendered
    # The old "looks like a pasted value" heuristic is gone: it printed the suspected value.
    assert "credential_env_looks_like_value" not in rendered

    # A later position is named too, still without echoing the value.
    (directory / "bundle.json").write_text(
        json.dumps(standard("leaky", dependencies={"credential_env": ["FINE_KEY", sentinel]})),
        encoding="utf-8",
    )
    later = json.dumps(validate_ref("project:leaky", project))
    assert "entry 2" in later and sentinel not in later


def test_a_credential_looking_name_is_not_printed_by_the_doctor_or_the_cli(tmp_path: Path) -> None:
    import subprocess
    import sys

    sentinel = "sk-live-SENTINEL-DO-NOT-ECHO-0123456789"
    project = tmp_path / "project"
    directory = project / ".agents" / "jev-bundle" / "leaky-cli"
    directory.mkdir(parents=True)
    (directory / "bundle.json").write_text(
        json.dumps(standard("leaky-cli", dependencies={"credential_env": [sentinel]})), encoding="utf-8"
    )
    (directory / "BUNDLE.md").write_text("# x\n", encoding="utf-8")

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(item for item in sys.path if item)
    for argv in (
        [sys.executable, "-m", "jev_loop.cli", "bundle", "validate", "project:leaky-cli",
         "--project-root", str(project), "--json"],
        [sys.executable, "-m", "jev_loop.doctor", "--offline", "--json", "--bundle",
         str(directory / "bundle.json")],
    ):
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False, env=environment
        )
        assert sentinel not in completed.stdout + completed.stderr, argv
        assert "Traceback" not in completed.stdout + completed.stderr, argv

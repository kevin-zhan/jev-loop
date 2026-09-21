"""Reference resolution: one resolver, one trust root, no silent path-semantics change."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jev_loop.bundles.discovery import discovery_root
from jev_loop.bundles.resolve import (
    KIND_DIAGNOSTIC,
    KIND_NAME,
    KIND_PATH,
    BundleRefError,
    resolve_bundle_ref,
)

VALID = {
    "schema_version": 2,
    "name": "{name}",
    "version": "0.1.0",
    "description": "a resolver fixture",
    "entrypoint": "bundle_controller:build",
}

LEGACY = {"schema_version": 1, "entrypoint": "controller:build"}


def make_bundle(project: Path, name: str, *, manifest: dict | None = None, instructions: bool = True) -> Path:
    directory = discovery_root(project) / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bundle.json").write_text(
        json.dumps(manifest if manifest is not None else json.loads(json.dumps(VALID).replace("{name}", name))),
        encoding="utf-8",
    )
    if instructions:
        (directory / "BUNDLE.md").write_text(f"# {name}\n", encoding="utf-8")
    return directory / "bundle.json"


def test_the_builtin_diagnostic_bundle_is_reserved(tmp_path: Path) -> None:
    make_bundle(tmp_path, "diagnostic")
    resolved = resolve_bundle_ref("diagnostic", tmp_path)
    assert resolved.kind == KIND_DIAGNOSTIC and resolved.is_diagnostic
    assert resolved.manifest_path is None


def test_names_resolve_inside_the_explicit_project_only(tmp_path: Path) -> None:
    make_bundle(tmp_path, "alpha")
    resolved = resolve_bundle_ref("project:alpha", tmp_path)
    assert resolved.kind == KIND_NAME and resolved.name == "alpha"
    assert resolved.provenance == ".agents/jev-bundle/alpha/bundle.json"
    assert resolved.is_standard is True

    bare = resolve_bundle_ref("alpha", tmp_path)
    assert bare.manifest_path == resolved.manifest_path


def test_a_parent_project_is_not_searched(tmp_path: Path) -> None:
    make_bundle(tmp_path, "alpha")
    child = tmp_path / "nested" / "work"
    child.mkdir(parents=True)
    with pytest.raises(BundleRefError, match="no bundle named 'alpha'"):
        resolve_bundle_ref("alpha", child)


def test_a_bare_name_that_is_also_a_relative_file_is_ambiguous(tmp_path: Path) -> None:
    make_bundle(tmp_path, "alpha")
    (tmp_path / "alpha").write_text(json.dumps(LEGACY), encoding="utf-8")
    with pytest.raises(BundleRefError, match="ambiguous") as caught:
        resolve_bundle_ref("alpha", tmp_path)
    message = str(caught.value)
    assert "Use './alpha' for the file" in message
    assert "''alpha''" not in message
    assert "project:alpha" in message
    assert resolve_bundle_ref("project:alpha", tmp_path).kind == KIND_NAME
    assert resolve_bundle_ref("./alpha", tmp_path).kind == KIND_PATH


def test_legacy_path_semantics_are_unchanged_for_explicit_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "legacy").mkdir(parents=True)
    manifest = project / "legacy" / "bundle.json"
    manifest.write_text(json.dumps(LEGACY), encoding="utf-8")

    relative = resolve_bundle_ref("legacy/bundle.json", project)
    assert relative.kind == KIND_PATH and relative.manifest_path == manifest.resolve()
    assert resolve_bundle_ref(str(manifest), project).manifest_path == manifest.resolve()
    assert resolve_bundle_ref("./legacy/bundle.json", project).manifest_path == manifest.resolve()
    assert relative.is_standard is False


def test_a_manifest_outside_the_project_root_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(LEGACY), encoding="utf-8")

    with pytest.raises(BundleRefError, match="must be inside the project root"):
        resolve_bundle_ref(str(outside), project)
    with pytest.raises(BundleRefError, match="must be inside the project root"):
        resolve_bundle_ref("../outside.json", project)


def test_a_symlinked_manifest_that_escapes_the_project_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(LEGACY), encoding="utf-8")
    link = project / "linked.json"
    os.symlink(outside, link)

    with pytest.raises(BundleRefError, match="must be inside the project root"):
        resolve_bundle_ref("linked.json", project)


def test_a_missing_path_names_the_directory_candidate(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "some-bundle").mkdir(parents=True)
    (project / "some-bundle" / "bundle.json").write_text(json.dumps(LEGACY), encoding="utf-8")
    with pytest.raises(BundleRefError, match=r"did you mean the explicit path '.*bundle\.json'"):
        resolve_bundle_ref("some-bundle", project)


@pytest.mark.parametrize("reference", ["user:alpha", "global:alpha", "other:thing", "project:Uppercase", "project:"])
def test_unsupported_schemes_and_invalid_names_are_refused(tmp_path: Path, reference: str) -> None:
    with pytest.raises(BundleRefError):
        resolve_bundle_ref(reference, tmp_path)


def test_an_unusable_entry_is_named_with_its_problems(tmp_path: Path) -> None:
    make_bundle(tmp_path, "broken", instructions=False)
    with pytest.raises(BundleRefError, match="is not usable: missing BUNDLE.md"):
        resolve_bundle_ref("broken", tmp_path)


def test_a_v2_python_path_may_not_escape_its_bundle_directory(tmp_path: Path) -> None:
    make_bundle(tmp_path, "escaping", manifest={**json.loads(json.dumps(VALID).replace("{name}", "escaping")),
                                                "python_path": ".."})
    with pytest.raises(BundleRefError, match="outside the bundle directory"):
        resolve_bundle_ref("escaping", tmp_path)


def test_a_v2_manifest_keeps_its_bundle_bounded_python_path(tmp_path: Path) -> None:
    directory = make_bundle(tmp_path, "nested-python", manifest={
        **json.loads(json.dumps(VALID).replace("{name}", "nested-python")), "python_path": "src"
    })
    (directory.parent / "src").mkdir()
    assert resolve_bundle_ref("nested-python", tmp_path).manifest.python_dir == (directory.parent / "src").resolve()


def test_an_unsupported_version_fails_before_anything_runs(tmp_path: Path) -> None:
    make_bundle(tmp_path, "future", manifest={"schema_version": 3, "entrypoint": "c:b", "name": "future",
                                              "version": "1", "description": "future"})
    with pytest.raises(BundleRefError, match="unsupported schema_version 3"):
        resolve_bundle_ref("future", tmp_path)

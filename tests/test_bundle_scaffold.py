"""Scaffolding: no-clobber, portable templates, and an honest scaffold marker."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jev_loop.bundles.discovery import discovery_root
from jev_loop.bundles.scaffold import (
    ScaffoldError,
    create_bundle,
    template_tokens,
)
from jev_loop.bundles.validate import validate_ref


def test_the_packaged_template_is_readable_and_carries_machine_tokens() -> None:
    tokens = template_tokens()
    assert "__BUNDLE_NAME__" in tokens
    assert all(token.startswith("__") and token.endswith("__") for token in tokens)


def test_create_writes_the_expected_files_without_placeholders(tmp_path: Path) -> None:
    target = tmp_path / "project" / ".agents" / "jev-bundle" / "fresh-bundle"
    created = create_bundle(target, "fresh-bundle", description="a generated scaffold")
    names = sorted(path.name for path in created.files)
    assert names == ["BUNDLE.md", "bundle.json", "bundle_controller.py", "test_bundle_offline.py"]
    assert created.created_directories[0] == tmp_path / "project"

    manifest = json.loads((target / "bundle.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "fresh-bundle"
    assert manifest["scaffold"] is True
    assert manifest["version"] == "0.1.0"

    for path in created.files:
        text = path.read_text(encoding="utf-8")
        assert "__BUNDLE_NAME__" not in text and "__BUNDLE_DESCRIPTION__" not in text
        assert "fresh-bundle" in text or path.name.startswith("test_")


def test_create_never_writes_an_absolute_author_path(tmp_path: Path) -> None:
    target = tmp_path / "b" / "portable-bundle"
    created = create_bundle(target, "portable-bundle")
    for path in created.files:
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text and "/home/" not in text
        assert str(tmp_path) not in text


def test_create_refuses_an_existing_target_directory_and_changes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("user content\n", encoding="utf-8")
    with pytest.raises(ScaffoldError, match="refusing to create a bundle in"):
        create_bundle(target, "existing")
    assert [path.name for path in target.iterdir()] == ["keep.txt"]

    # A new directory inside an existing parent is the normal --dir case and stays allowed.
    nested = target / "child"
    create_bundle(nested, "child")
    assert (nested / "bundle.json").is_file()


def test_only_whitelisted_complete_templates_are_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray directory in the package must never be offered or used as a template."""
    from jev_loop.bundles import scaffold as scaffold_module
    from jev_loop.bundles.scaffold import TEMPLATE_NAMES, template_directory

    assert TEMPLATE_NAMES == ("basic",)
    assert template_directory("basic").is_dir()
    for rejected in ("__pycache__", "nope", "../templates", ".", "basic/../basic"):
        with pytest.raises(ScaffoldError, match="unknown template"):
            template_directory(rejected)
        with pytest.raises(ScaffoldError, match="unknown template"):
            create_bundle(tmp_path / f"x-{rejected.replace('/', '_')}", "rejected-name", template=rejected)
        assert not (tmp_path / f"x-{rejected.replace('/', '_')}").exists()

    incomplete = tmp_path / "incomplete-template"
    (incomplete / "empty-template").mkdir(parents=True)
    monkeypatch.setattr(scaffold_module, "TEMPLATE_NAMES", ("empty-template",))
    monkeypatch.setattr(scaffold_module.resources, "files", lambda _package: incomplete)
    with pytest.raises(ScaffoldError, match="incomplete"):
        scaffold_module.template_directory("empty-template")


def test_the_whitelist_comes_from_the_packaged_template_set() -> None:
    from jev_loop.bundles.scaffold import REQUIRED_TEMPLATE_FILES, TEMPLATE_NAMES, template_directory

    for name in TEMPLATE_NAMES:
        directory = template_directory(name)
        for required in REQUIRED_TEMPLATE_FILES:
            assert (directory / required).is_file(), f"{name} is missing {required}"


def test_create_refuses_an_invalid_name(tmp_path: Path) -> None:
    for name in ("Upper", "double--hyphen", "-lead", "", "with/slash"):
        with pytest.raises(ScaffoldError):
            create_bundle(tmp_path / "x", name)


def test_a_cleanup_failure_leaves_no_partial_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "tree" / ".agents" / "jev-bundle" / "exploding"
    from jev_loop.bundles import scaffold as scaffold_module

    original = scaffold_module._render

    def explode(text: str, **kwargs: str) -> str:
        if "bundle_controller.py" in kwargs.get("name", "") or kwargs.get("name") == "exploding":
            raise RuntimeError("render failure")
        return original(text, **kwargs)

    monkeypatch.setattr(scaffold_module, "_render", explode)
    with pytest.raises(ScaffoldError, match="nothing was left behind"):
        create_bundle(target, "exploding")
    assert not target.exists()
    assert not (tmp_path / "tree").exists()


def test_the_generated_scaffold_validates_and_starts_nowhere(tmp_path: Path) -> None:
    project = tmp_path / "project"
    create_bundle(discovery_root(project) / "generated-bundle", "generated-bundle")
    report = validate_ref("project:generated-bundle", project)
    assert report["ok"] is True, report["errors"]
    assert any(item["code"] == "unfinished_scaffold" for item in report["warnings"])


def test_the_generated_author_tests_run_offline_with_the_standard_library(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle = discovery_root(project) / "generated-bundle"
    create_bundle(bundle, "generated-bundle")
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=bundle,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "OK" in completed.stderr

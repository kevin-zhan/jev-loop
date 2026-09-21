"""Discovery of project bundles: provenance, classification and inertness."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from jev_loop.bundles.discovery import (
    STATUS_INVALID,
    STATUS_OK,
    STATUS_OUTSIDE_TRUST_ROOT,
    discover_bundles,
    discovery_root,
    find_bundle,
)

MANIFEST = {
    "schema_version": 2,
    "name": "{name}",
    "version": "0.1.0",
    "description": "a discovery fixture",
    "entrypoint": "bundle_controller:build",
}

LEGACY = {"schema_version": 1, "entrypoint": "controller:build"}


def write_bundle(project: Path, name: str, *, manifest: dict | None = None, instructions: bool = True,
                 directory: str | None = None) -> Path:
    root = discovery_root(project) / (directory or name)
    root.mkdir(parents=True, exist_ok=True)
    payload = manifest if manifest is not None else json.loads(json.dumps(MANIFEST).replace("{name}", name))
    (root / "bundle.json").write_text(json.dumps(payload), encoding="utf-8")
    if instructions:
        (root / "BUNDLE.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


def test_no_discovery_root_is_an_empty_result_not_an_error(tmp_path: Path) -> None:
    assert discover_bundles(tmp_path) == ()
    assert find_bundle(tmp_path, "anything") is None


def test_a_complete_entry_is_discovered_with_its_provenance(tmp_path: Path) -> None:
    write_bundle(tmp_path, "offline-switchboard")
    entries = discover_bundles(tmp_path)
    assert [entry.name for entry in entries] == ["offline-switchboard"]
    entry = entries[0]
    assert entry.status == STATUS_OK and entry.valid
    assert entry.provenance == ".agents/jev-bundle/offline-switchboard/bundle.json"
    assert entry.manifest is not None and entry.manifest.name == "offline-switchboard"
    assert entry.to_json()["manifest"]["version"] == "0.1.0"


def test_entries_are_sorted_and_hidden_entries_are_ignored(tmp_path: Path) -> None:
    write_bundle(tmp_path, "zulu")
    write_bundle(tmp_path, "alpha")
    (discovery_root(tmp_path) / ".DS_Store").write_text("", encoding="utf-8")
    (discovery_root(tmp_path) / "notes.md").write_text("loose file\n", encoding="utf-8")
    (discovery_root(tmp_path) / ".hidden").mkdir()
    assert [entry.name for entry in discover_bundles(tmp_path)] == ["alpha", "zulu"]


def test_every_kind_of_broken_entry_stays_visible_with_a_reason(tmp_path: Path) -> None:
    write_bundle(tmp_path, "missing-instructions", instructions=False)
    write_bundle(tmp_path, "name-mismatch", directory="name-mismatch",
                 manifest={**{k: v for k, v in MANIFEST.items() if k != "name"}, "name": "something-else"})
    write_bundle(tmp_path, "legacy", manifest=LEGACY)
    broken = discovery_root(tmp_path) / "broken-json"
    broken.mkdir(parents=True)
    (broken / "bundle.json").write_text("{not json", encoding="utf-8")
    (broken / "BUNDLE.md").write_text("# x\n", encoding="utf-8")
    (discovery_root(tmp_path) / "empty-dir").mkdir()
    (discovery_root(tmp_path) / "Bad-Name").mkdir()

    entries = {entry.name: entry for entry in discover_bundles(tmp_path)}
    assert entries["missing-instructions"].problems[0].startswith("missing BUNDLE.md")
    assert "does not match directory name" in entries["name-mismatch"].problems[0]
    assert "legacy manifest format" in entries["legacy"].problems[0]
    assert "is not valid UTF-8 JSON" in entries["broken-json"].problems[0]
    assert entries["empty-dir"].problems == ("missing bundle.json",)
    assert "not a valid bundle name" in entries["Bad-Name"].problems[0]
    assert all(entry.status == STATUS_INVALID for name, entry in entries.items())


def test_a_bundle_directory_that_resolves_outside_the_project_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-bundle"
    outside.mkdir(exist_ok=True)
    (outside / "bundle.json").write_text(json.dumps({**MANIFEST, "name": "escape"}), encoding="utf-8")
    (outside / "BUNDLE.md").write_text("# escape\n", encoding="utf-8")
    link = discovery_root(tmp_path) / "escape"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)

    (entry,) = discover_bundles(tmp_path)
    assert entry.status == STATUS_OUTSIDE_TRUST_ROOT
    assert "outside the project root" in entry.problems[0]
    assert find_bundle(tmp_path, "escape") is not None  # visible, but not startable


def test_discovery_reads_only_the_manifest_and_never_puts_the_bundle_on_sys_path(tmp_path: Path) -> None:
    bundle = write_bundle(tmp_path, "no-imports")
    (bundle / "bundle_controller.py").write_text("raise AssertionError('must not be imported')\n", encoding="utf-8")
    before_modules = set(sys.modules)
    before_path = list(sys.path)
    entries = discover_bundles(tmp_path)
    assert entries[0].valid
    assert set(sys.modules) == before_modules or all(
        getattr(sys.modules[name], "__file__", "") is None
        or not str(sys.modules[name].__file__).startswith(str(bundle))
        for name in set(sys.modules) - before_modules
    )
    assert sys.path == before_path
    assert not (bundle / "__pycache__").exists()


def test_an_unknown_name_reports_what_is_available(tmp_path: Path) -> None:
    write_bundle(tmp_path, "alpha")
    write_bundle(tmp_path, "broken", instructions=False)
    from jev_loop.bundles.resolve import BundleRefError, resolve_bundle_ref

    try:
        resolve_bundle_ref("missing", tmp_path)
    except BundleRefError as error:
        message = str(error)
    else:  # pragma: no cover - the resolver must refuse an unknown name
        raise AssertionError("an unknown bundle name must be refused")
    assert "alpha" in message and "broken" in message
    assert "missing BUNDLE.md" in message

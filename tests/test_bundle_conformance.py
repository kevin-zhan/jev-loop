"""Conformance: static contract, inert-discovery proof and the optional author tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from jev_loop.bundles.conformance import conformance_report, run_probe, run_tests
from jev_loop.bundles.scaffold import create_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_BUNDLE = REPO_ROOT / ".agents" / "jev-bundle" / "offline-switchboard"


def test_the_reference_bundle_passes_conformance_with_its_author_tests() -> None:
    report = conformance_report("project:offline-switchboard", REPO_ROOT, run_tests_flag=True)
    assert report["ok"] is True, json.dumps(report, indent=2)
    assert report["static"]["ok"] is True
    assert report["side_effects"]["ok"] is True
    assert report["author_tests"]["status"] == "passed"
    assert report["author_tests"]["exit_code"] == 0
    assert report["runtime_semantics"]["status"] == "not_checked"
    assert report["bundle_digest"]


def test_conformance_does_not_claim_bundle_code_was_never_imported_by_the_whole_process() -> None:
    report = conformance_report("project:offline-switchboard", REPO_ROOT)
    scope = report["side_effects"]["scope"]
    # The probe is itself a child process; what it establishes is scoped to what the bundle code
    # did inside it, and that claim must be stated rather than implied.
    assert "one isolated probe subprocess" in scope
    assert "does not claim that the interpreter imported nothing at all" in scope
    assert "regular files inside the bundle" in scope


def test_the_probe_detects_network_and_import_side_effects(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "side-effects"
    create_bundle(bundle, "side-effects")
    (bundle / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": "side-effects",
                "version": "0.1.0",
                "description": "a bundle whose manifest loading must stay inert",
                "entrypoint": "bundle_controller:build",
                "config": {},
            }
        ),
        encoding="utf-8",
    )
    (bundle / "BUNDLE.md").write_text("# side-effects\n", encoding="utf-8")

    clean = run_probe("project:side-effects", project)
    assert clean["ok"] is True, clean
    assert clean["observed"]["imported_from_bundle"] == []
    assert clean["observed"]["audit_events"] == []
    assert clean["observed"]["files_changed"] == []
    assert clean["observed"]["bundle_module_on_path"] is False

    # A module-level import in the controller is still not triggered by discovery: the probe
    # asserts the *bundle* stayed inert, and discovery never imports it at all.
    (bundle / "bundle_controller.py").write_text(
        "import socket\nsocket.socket()\nraise AssertionError('never imported')\n", encoding="utf-8"
    )
    still_clean = run_probe("project:side-effects", project)
    assert still_clean["ok"] is True, still_clean


def test_author_tests_are_optional_and_reported_as_such() -> None:
    report = conformance_report("project:offline-switchboard", REPO_ROOT)
    assert report["author_tests"]["status"] == "not_run"
    assert "--run-tests" in report["author_tests"]["detail"]


def test_a_missing_tests_directory_is_reported_not_hidden(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    create_bundle(bundle, "no-tests")
    shutil.rmtree(bundle / "tests")
    result = run_tests(bundle)
    assert result["status"] == "no_tests"


def test_failing_author_tests_make_conformance_fail_with_the_real_exit_code(tmp_path: Path) -> None:
    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "failing-tests"
    create_bundle(bundle, "failing-tests")
    (bundle / "tests" / "test_broken.py").write_text(
        "import unittest\n\n\nclass Broken(unittest.TestCase):\n    def test_fails(self):\n        self.fail('nope')\n",
        encoding="utf-8",
    )
    report = conformance_report("project:failing-tests", project, run_tests_flag=True)
    assert report["ok"] is False
    assert report["author_tests"]["status"] == "failed"
    assert report["author_tests"]["exit_code"] == 1


def test_a_timeout_is_reported_rather_than_hanging(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    create_bundle(bundle, "slow-tests")
    (bundle / "tests" / "test_slow.py").write_text(
        "import time\nimport unittest\n\n\nclass Slow(unittest.TestCase):\n"
        "    def test_sleeps(self):\n        time.sleep(10)\n",
        encoding="utf-8",
    )
    result = run_tests(bundle, timeout=1.0)
    assert result["status"] == "timeout"
    assert "1" in result["detail"]


def test_conformance_on_an_unusable_reference_reports_the_static_error(tmp_path: Path) -> None:
    report = conformance_report("project:absent", tmp_path)
    assert report["ok"] is False
    assert report["static"]["ok"] is False
    assert report["static"]["errors"]

def test_the_probe_and_the_digest_never_read_outside_the_bundle_or_into_hidden_files(tmp_path: Path) -> None:
    """The conformance default path must use the same bounded, symlink-free reads as validation."""
    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "sealed"
    create_bundle(bundle, "sealed")

    secret = tmp_path / "outside-secret.txt"
    secret.write_text("PRIVATE-MATERIAL-0123456789\n", encoding="utf-8")
    (bundle / "linked.txt").symlink_to(secret)
    (bundle / "linked-dir").symlink_to(tmp_path)
    (bundle / ".hidden").mkdir()
    (bundle / ".hidden" / "credentials.txt").write_text("PRIVATE-MATERIAL-0123456789\n", encoding="utf-8")

    report = conformance_report("project:sealed", project)
    assert report["side_effects"]["ok"] is True, report["side_effects"]
    observed = report["side_effects"]["observed"]
    assert observed["files_changed"] == []
    assert observed["scan_incomplete"] is False
    assert "hidden and cache directories pruned" in observed["scan_coverage"]
    assert "nothing outside the bundle read" in observed["scan_coverage"]
    assert observed["symlinks_skipped"] >= 2

    # Neither the digest nor the probe output may carry a hash derived from the external or hidden
    # file, and the digest must state its own scope.
    rendered = json.dumps(report)
    assert "PRIVATE-MATERIAL" not in rendered
    assert report["bundle_digest"] and len(report["bundle_digest"]) == 64
    assert "hidden and cache directories pruned" in report["bundle_digest_scope"]


def test_an_incomplete_scan_yields_no_digest_and_says_so(tmp_path: Path) -> None:
    from jev_loop.bundles import validate as validate_module

    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "huge"
    create_bundle(bundle, "huge")
    many = bundle / "many"
    many.mkdir()
    for index in range(validate_module.MAX_SCAN_ENTRIES + 20):
        (many / f"f-{index}.txt").write_text("x\n", encoding="utf-8")

    report = conformance_report("project:huge", project)
    assert report["bundle_digest"] is None, "a partial read must not be presented as a bundle digest"
    assert "partial:" in report["bundle_digest_scope"]
    assert report["side_effects"]["observed"]["scan_incomplete"] is True
    assert report["side_effects"]["ok"] is False, "an incomplete scan cannot support a 'nothing changed' claim"


def test_a_failing_probe_reports_a_fixed_reason_and_sizes_only(tmp_path: Path, monkeypatch) -> None:
    from jev_loop.bundles import conformance as conformance_module

    class Exploding:
        returncode = 3
        stdout = "bundle-internal-value=do-not-echo"
        stderr = "traceback with /private/paths and secrets"

    def fake_run(*_args, **_kwargs):
        return Exploding()

    monkeypatch.setattr(conformance_module.subprocess, "run", fake_run)
    result = conformance_module.run_probe("project:offline-switchboard", REPO_ROOT)
    assert result["ok"] is False and result["status"] == "probe_failed"
    rendered = json.dumps(result)
    assert "do-not-echo" not in rendered and "traceback" not in rendered and "/private/paths" not in rendered
    assert result["diagnostics"]["exit_code"] == 3
    assert result["diagnostics"]["stdout_bytes"] > 0 and result["diagnostics"]["stderr_bytes"] > 0


def test_author_tests_are_not_executed_when_the_static_contract_fails(tmp_path: Path) -> None:
    """Running author code after a failed contract check would be both pointless and unsafe."""
    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "broken-contract"
    create_bundle(bundle, "broken-contract")
    marker = tmp_path / "author-code-ran.txt"
    (bundle / "tests" / "test_marker.py").write_text(
        "import pathlib\nimport unittest\n\n\nclass Marker(unittest.TestCase):\n"
        f"    def test_writes_marker(self):\n        pathlib.Path({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    manifest["scaffold"] = False
    manifest["python_path"] = "does-not-exist"
    (bundle / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = conformance_report("project:broken-contract", project, run_tests_flag=True)
    assert report["ok"] is False
    assert report["static"]["ok"] is False
    assert report["author_tests"]["status"] == "skipped"
    assert not marker.exists(), "author code must not run after a failed static contract"


def test_conformance_of_the_builtin_diagnostic_bundle_does_not_fail_its_probe() -> None:
    """The diagnostic ref has no bundle directory: the probe must handle that shape, not KeyError."""
    report = conformance_report("diagnostic", REPO_ROOT)
    assert report["static"]["ok"] is True
    assert report["side_effects"]["ok"] is True, report["side_effects"]
    observed = report["side_effects"]["observed"]
    # One consistent shape, explicitly scoped as not applicable rather than "no file changed".
    for key in (
        "fingerprint",
        "scan_coverage",
        "scan_incomplete",
        "scan_not_applicable",
        "symlinks_skipped",
        "read_failures",
        "files_changed",
    ):
        assert key in observed, key
    assert observed["scan_not_applicable"] is True
    assert observed["fingerprint"] is None
    assert observed["scan_incomplete"] is False
    assert "not applicable" in observed["scan_coverage"]
    assert observed["files_changed"] == []
    assert report["bundle_digest"] is None
    assert "not applicable" in report["bundle_digest_scope"]

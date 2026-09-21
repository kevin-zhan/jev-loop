"""The live-files reference example: real filesystem, offline HTTP fixture, honest failures.

The example modules are loaded locally with importlib (the repository must not hide import
problems behind a global pytest pythonpath), and the user-facing command is also exercised
through a real subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jev_loop.core.types import VerifyVerdict

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "live-files"
REPO_ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "example-sentinel-credential"


def load_example_module(name: str):
    path = EXAMPLE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"live_files_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def example():
    return load_example_module("environment")


def run_runner(fake_jev, argv, *, tmp_path, with_credential=True):
    # A deliberately minimal environment: the documented command must not depend on the developer's
    # shell profile, personal variables or agent configuration.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if with_credential:
        env["TYPESAFE_API_KEY"] = SENTINEL
    return subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "run.py"), "--api-url", fake_jev.url, *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=60,
    )


def test_workspace_must_be_new_or_empty(example, tmp_path):
    workspace = tmp_path / "occupied"
    workspace.mkdir()
    keep = workspace / "notes.txt"
    keep.write_text("user data", encoding="utf-8")

    with pytest.raises(example.WorkspaceError):
        example.prepare_workspace(workspace)

    assert keep.read_text(encoding="utf-8") == "user data"
    assert sorted(entry.name for entry in workspace.iterdir()) == ["notes.txt"]


def test_workspace_symlink_is_refused(example, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(example.WorkspaceError):
        example.prepare_workspace(link)


def test_verifier_reads_the_real_filesystem_not_the_model(example, tmp_path):
    workspace = example.prepare_workspace(tmp_path / "ws")
    example.seed_workspace(workspace)
    verifier = example.LiveFilesVerifier(workspace)

    assert verifier.verify(None).verdict is VerifyVerdict.UNSATISFIED
    assert verifier.verify(None).evidence["checked"]["beta_content"] == "ready"

    (workspace / "inbox" / "alpha.txt").write_text("ready", encoding="utf-8")
    (workspace / "archive").mkdir()
    (workspace / "inbox" / "gamma.log").rename(workspace / "archive" / "gamma.log")
    satisfied = verifier.verify(None)
    assert satisfied.verdict is VerifyVerdict.SATISFIED
    assert satisfied.evidence["checked"]["gamma_still_in_inbox"] is False

    (workspace / "inbox" / "beta.txt").write_text("changed", encoding="utf-8")
    assert verifier.verify(None).verdict is VerifyVerdict.UNSATISFIED


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_verifier_reports_unknown_when_a_file_cannot_be_read(example, tmp_path):
    workspace = example.prepare_workspace(tmp_path / "ws")
    example.seed_workspace(workspace)
    beta = workspace / "inbox" / "beta.txt"
    beta.chmod(0o000)
    try:
        assert example.LiveFilesVerifier(workspace).verify(None).verdict is VerifyVerdict.UNKNOWN
    finally:
        beta.chmod(0o600)


def test_symlink_inside_the_workspace_is_refused(example, tmp_path):
    workspace = example.prepare_workspace(tmp_path / "ws")
    example.seed_workspace(workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    alpha = workspace / "inbox" / "alpha.txt"
    alpha.unlink()
    alpha.symlink_to(outside)

    assert example.LiveFilesVerifier(workspace).verify(None).verdict is VerifyVerdict.UNKNOWN
    with pytest.raises(Exception) as caught:
        example.LiveFilesEnvironment(workspace).observe()
    assert "outside-secret" not in str(caught.value)
    assert outside.read_text(encoding="utf-8") == "outside-secret"


def test_standalone_runner_end_to_end_offline(fake_jev, tmp_path):
    workspace = tmp_path / "ws"
    result = run_runner(fake_jev, ["--workspace", str(workspace), "--json"], tmp_path=tmp_path)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "succeeded"
    assert summary["decision_requests"] >= 3
    assert summary["verification"]["verdict"] == "satisfied"

    # Assert the real filesystem directly, independently of the verifier's own output.
    assert (workspace / "inbox" / "alpha.txt").read_text(encoding="utf-8") == "ready"
    assert (workspace / "archive" / "gamma.log").read_text(encoding="utf-8") == "noise"
    assert not (workspace / "inbox" / "gamma.log").exists()
    assert (workspace / "inbox" / "beta.txt").read_text(encoding="utf-8") == "ready"

    evidence = Path(summary["evidence_path"])
    assert evidence.is_file()
    assert json.loads(evidence.read_text(encoding="utf-8"))["verification"]["verdict"] == "satisfied"
    assert SENTINEL not in result.stdout + result.stderr + evidence.read_text(encoding="utf-8")
    assert fake_jev.requests
    assert all(request["authorization"] == f"Bearer {SENTINEL}" for request in fake_jev.requests)


def test_standalone_runner_refuses_a_non_empty_workspace(fake_jev, tmp_path):
    workspace = tmp_path / "occupied"
    workspace.mkdir()
    keep = workspace / "keep.txt"
    keep.write_text("user data", encoding="utf-8")

    result = run_runner(fake_jev, ["--workspace", str(workspace)], tmp_path=tmp_path)

    assert result.returncode == 3
    assert "refusing" in result.stderr
    assert keep.read_text(encoding="utf-8") == "user data"
    assert sorted(entry.name for entry in workspace.iterdir()) == ["keep.txt"]
    assert fake_jev.requests == []


def test_standalone_runner_checks_the_credential_before_creating_the_workspace(fake_jev, tmp_path):
    workspace = tmp_path / "never-created"
    result = run_runner(
        fake_jev, ["--workspace", str(workspace)], tmp_path=tmp_path, with_credential=False
    )

    assert result.returncode == 2
    assert "TYPESAFE_API_KEY" in result.stderr
    assert not workspace.exists()
    assert fake_jev.requests == []


def test_standalone_runner_fails_honestly_when_verification_stays_unsatisfied(fake_jev, tmp_path):
    fake_jev.answer_picker = lambda payload: "request_finish"
    workspace = tmp_path / "ws"
    result = run_runner(fake_jev, ["--workspace", str(workspace), "--json"], tmp_path=tmp_path)

    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["status"] != "succeeded"
    assert summary["verification"]["verdict"] != "satisfied"
    assert (workspace / "inbox" / "alpha.txt").read_text(encoding="utf-8") == "draft"  # untouched
    assert fake_jev.requests


def test_example_has_no_unbound_placeholders():
    for path in sorted(EXAMPLE_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for placeholder in ("my_adapter", "my_verifier", "<absolute-path", "YOUR_KEY"):
            assert placeholder not in text, f"{path.name} still contains the placeholder {placeholder!r}"


def test_example_help_documents_the_exit_codes():
    result = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "run.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert result.returncode == 0
    assert "exit codes" in result.stdout
    assert "--workspace" in result.stdout


def test_standalone_runner_exits_two_when_the_service_rejects_the_credential(fake_jev, tmp_path):
    fake_jev.behavior = "auth_error"
    for extra in ([], ["--json"]):
        workspace = tmp_path / f"ws-{len(extra)}"
        result = run_runner(fake_jev, ["--workspace", str(workspace), *extra], tmp_path=tmp_path)

        assert result.returncode == 2, (extra, result.stderr)
        assert "rejected the credential" in result.stderr
        assert "fixture-body-marker" not in result.stdout + result.stderr
        if extra:
            summary = json.loads(result.stdout)
            assert summary["failure_kind"] == "credential_rejected"
            assert summary["verification"] is None


def test_malformed_200_answer_never_leaks_provider_text(fake_jev, tmp_path):
    fake_jev.behavior = "malformed"
    workspace = tmp_path / "ws"
    result = run_runner(fake_jev, ["--workspace", str(workspace), "--json"], tmp_path=tmp_path)

    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["status"] != "succeeded"
    assert summary["failure_kind"] == "policy_error"
    combined = result.stdout + result.stderr + (workspace / "verification.json").read_text(encoding="utf-8")
    assert "fixture-body-marker" not in combined
    assert fake_jev.requests


@pytest.mark.parametrize("value", ["0", "-3"])
def test_non_positive_budget_is_a_configuration_error_before_any_side_effect(fake_jev, tmp_path, value):
    workspace = tmp_path / f"ws{value}"
    result = run_runner(fake_jev, ["--workspace", str(workspace), "--max-steps", value], tmp_path=tmp_path)

    assert result.returncode == 3
    assert "max-steps" in result.stderr
    assert not workspace.exists()
    assert fake_jev.requests == []


def test_invalid_budget_syntax_is_a_configuration_error_not_a_credential_one(fake_jev, tmp_path):
    workspace = tmp_path / "ws"
    result = run_runner(fake_jev, ["--workspace", str(workspace), "--max-steps", "abc"], tmp_path=tmp_path)

    assert result.returncode == 3
    assert "invalid command-line usage" in result.stderr
    assert "abc" not in result.stderr
    assert not workspace.exists()
    assert fake_jev.requests == []


def test_evidence_target_must_not_overwrite_an_existing_file(fake_jev, tmp_path):
    report = tmp_path / "report.json"
    report.write_text("SENTINEL-EXTERNAL-CONTENT", encoding="utf-8")
    workspace = tmp_path / "ws"

    result = run_runner(
        fake_jev,
        ["--workspace", str(workspace), "--evidence", str(report), "--json"],
        tmp_path=tmp_path,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert report.read_text(encoding="utf-8") == "SENTINEL-EXTERNAL-CONTENT"
    assert not workspace.exists()
    assert fake_jev.requests == []
    payload = json.loads(result.stdout)
    assert payload["decision_requests"] == 0
    assert payload["verified"] is False


def test_evidence_target_must_not_be_a_workspace_data_file(fake_jev, tmp_path):
    workspace = tmp_path / "ws"
    target = workspace / "inbox" / "alpha.txt"

    result = run_runner(
        fake_jev,
        ["--workspace", str(workspace), "--evidence", str(target), "--json"],
        tmp_path=tmp_path,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert not workspace.exists()
    assert fake_jev.requests == []
    payload = json.loads(result.stdout)
    assert payload["decision_requests"] == 0
    assert payload["verified"] is False


def test_custom_evidence_path_succeeds_without_touching_the_task_files(fake_jev, tmp_path):
    workspace = tmp_path / "ws"
    report = tmp_path / "custom-report.json"

    result = run_runner(
        fake_jev,
        ["--workspace", str(workspace), "--evidence", str(report), "--json"],
        tmp_path=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["status"] == "succeeded"
    assert summary["verification"]["verdict"] == "satisfied"
    assert (workspace / "inbox" / "alpha.txt").read_text(encoding="utf-8") == "ready"
    assert (workspace / "inbox" / "beta.txt").read_text(encoding="utf-8") == "ready"
    assert (workspace / "archive" / "gamma.log").read_text(encoding="utf-8") == "noise"
    assert not (workspace / "inbox" / "gamma.log").exists()


def _envelope(result):
    return json.loads(result.stdout)


def _assert_not_started(payload):
    assert payload["status"] == "not_started"
    assert payload["decision_requests"] == 0
    assert payload["verified"] is False
    assert payload["verification"] is None
    assert payload["steps"] == 0 and payload["executions"] == 0 and payload["decisions"] == 0


def test_json_missing_credential_is_a_stable_failure_envelope(fake_jev, tmp_path):
    workspace = tmp_path / "ws"
    result = run_runner(
        fake_jev, ["--workspace", str(workspace), "--json"], tmp_path=tmp_path, with_credential=False
    )

    assert result.returncode == 2
    payload = _envelope(result)
    _assert_not_started(payload)
    assert payload["failure_kind"] == "credential_error"
    assert "TYPESAFE_API_KEY" in payload["detail"]
    assert SENTINEL not in result.stdout + result.stderr
    assert not workspace.exists()
    assert fake_jev.requests == []


def test_json_usage_error_is_a_stable_failure_envelope(fake_jev, tmp_path):
    result = run_runner(
        fake_jev, ["--workspace", str(tmp_path / "ws"), "--max-steps", "abc", "--json"], tmp_path=tmp_path
    )

    assert result.returncode == 3
    payload = _envelope(result)
    _assert_not_started(payload)
    assert payload["failure_kind"] == "usage_error"
    assert "abc" not in result.stdout + result.stderr  # the raw argument is never echoed
    assert not (tmp_path / "ws").exists()
    assert fake_jev.requests == []


def test_json_budget_error_is_a_stable_failure_envelope(fake_jev, tmp_path):
    result = run_runner(
        fake_jev, ["--workspace", str(tmp_path / "ws"), "--max-steps", "0", "--json"], tmp_path=tmp_path
    )

    assert result.returncode == 3
    payload = _envelope(result)
    _assert_not_started(payload)
    assert payload["failure_kind"] == "configuration_error"
    assert payload["detail"] == "--max-steps must be a positive integer"
    assert not (tmp_path / "ws").exists()
    assert fake_jev.requests == []


def test_json_invalid_endpoint_is_a_stable_failure_envelope(fake_jev, tmp_path):
    result = run_runner(
        fake_jev,
        [
            "--workspace",
            str(tmp_path / "ws"),
            "--api-url",
            "http://api.example.com:abc/v1",
            "--json",
        ],
        tmp_path=tmp_path,
    )

    assert result.returncode == 3
    payload = _envelope(result)
    _assert_not_started(payload)
    assert payload["failure_kind"] == "configuration_error"
    assert "api.example.com" not in result.stdout + result.stderr  # no value echo
    assert not (tmp_path / "ws").exists()
    assert fake_jev.requests == []


def test_json_non_empty_workspace_is_a_stable_failure_envelope(fake_jev, tmp_path):
    workspace = tmp_path / "occupied"
    workspace.mkdir()
    keep = workspace / "keep.txt"
    keep.write_text("user data", encoding="utf-8")

    result = run_runner(fake_jev, ["--workspace", str(workspace), "--json"], tmp_path=tmp_path)

    assert result.returncode == 3
    payload = _envelope(result)
    _assert_not_started(payload)
    assert payload["failure_kind"] == "workspace_error"
    assert keep.read_text(encoding="utf-8") == "user data"
    assert sorted(entry.name for entry in workspace.iterdir()) == ["keep.txt"]
    assert fake_jev.requests == []


def test_evidence_path_equal_to_the_workspace_is_refused(fake_jev, tmp_path):
    workspace = tmp_path / "ws"  # does not exist yet

    result = run_runner(
        fake_jev,
        ["--workspace", str(workspace), "--evidence", str(workspace), "--json"],
        tmp_path=tmp_path,
    )

    assert result.returncode == 3
    assert not workspace.exists()
    assert fake_jev.requests == []
    payload = _envelope(result)
    assert payload["failure_kind"] == "evidence_error"
    assert payload["decision_requests"] == 0


def test_evidence_parent_that_is_a_file_is_refused_before_anything(fake_jev, tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("user data", encoding="utf-8")
    workspace = tmp_path / "ws"

    result = run_runner(
        fake_jev,
        ["--workspace", str(workspace), "--evidence", str(parent / "report.json"), "--json"],
        tmp_path=tmp_path,
    )

    assert result.returncode == 3
    assert parent.read_text(encoding="utf-8") == "user data"
    assert not (parent / "report.json").exists()
    assert not workspace.exists()
    assert fake_jev.requests == []
    payload = _envelope(result)
    assert payload["failure_kind"] == "evidence_error"
    assert payload["decision_requests"] == 0
    assert "not-a-directory" not in result.stdout + result.stderr  # no path echo


def test_evidence_written_flag_matches_the_artifact_on_success(fake_jev, tmp_path):
    for label, custom_evidence in (("default", None), ("custom", tmp_path / "custom.json")):
        workspace = tmp_path / f"ws-{label}"
        argv = ["--workspace", str(workspace), "--json"]
        if custom_evidence is not None:
            argv += ["--evidence", str(custom_evidence)]
        result = run_runner(fake_jev, argv, tmp_path=tmp_path)

        assert result.returncode == 0, (label, result.stdout + result.stderr)
        payload = _envelope(result)
        assert payload["evidence_written"] is True, label
        artifact = custom_evidence or (workspace / "verification.json")
        stored = json.loads(Path(artifact).read_text(encoding="utf-8"))
        assert stored["evidence_written"] is True, label
        assert stored["status"] == "succeeded", label


def _load_run_module():
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        return load_example_module("run")
    finally:
        sys.path.remove(str(EXAMPLE_DIR))


def test_injected_evidence_write_failure_keeps_real_counts_in_one_json(fake_jev, tmp_path, monkeypatch, capsys):
    run_module = _load_run_module()
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)

    def boom(*args, **kwargs):
        raise OSError("injected-fsync-detail")

    monkeypatch.setattr(run_module.os, "fsync", boom)
    report = tmp_path / "report.json"
    workspace = tmp_path / "ws"
    code = run_module.main(
        [
            "--workspace",
            str(workspace),
            "--evidence",
            str(report),
            "--api-url",
            fake_jev.url,
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, captured.out  # exactly one JSON document
    payload = json.loads(lines[0])
    assert payload["status"] == "succeeded"
    assert payload["verified"] is True
    assert payload["decision_requests"] == 3  # real counts, not a not_started envelope
    assert payload["usage"]
    assert payload["failure_kind"] == "evidence_write_failed"
    assert payload["evidence_written"] is False
    assert "injected-fsync-detail" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    assert "OSError" not in captured.out + captured.err
    assert not report.exists()
    assert (workspace / "inbox" / "alpha.txt").read_text(encoding="utf-8") == "ready"


def test_cleanup_failure_does_not_mask_the_primary_evidence_error(fake_jev, tmp_path, monkeypatch, capsys):
    run_module = _load_run_module()
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)

    def boom(*args, **kwargs):
        raise OSError("injected-fsync-detail")

    def unlink_boom(*args, **kwargs):
        raise OSError("injected-unlink-detail")

    monkeypatch.setattr(run_module.os, "fsync", boom)
    monkeypatch.setattr(Path, "unlink", unlink_boom)
    report = tmp_path / "report.json"
    code = run_module.main(
        [
            "--workspace",
            str(tmp_path / "ws"),
            "--evidence",
            str(report),
            "--api-url",
            fake_jev.url,
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    payload = json.loads(captured.out.strip())
    assert payload["failure_kind"] == "evidence_write_failed"
    assert payload["decision_requests"] == 3
    assert "injected-fsync-detail" not in captured.out + captured.err
    assert "injected-unlink-detail" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize(
    "method,exception",
    [
        ("resolve", OSError("sentinel-evidence-path-detail")),
        ("resolve", RuntimeError("sentinel-evidence-path-detail")),
        ("exists", OSError("sentinel-evidence-path-detail")),
    ],
)
def test_evidence_preflight_path_failures_are_mapped_safely(
    fake_jev, tmp_path, monkeypatch, capsys, method, exception
):
    run_module = _load_run_module()
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)

    def boom(self, *args, **kwargs):
        raise exception

    monkeypatch.setattr(Path, method, boom)
    workspace = tmp_path / "ws"
    report = tmp_path / "report.json"
    code = run_module.main(
        [
            "--workspace",
            str(workspace),
            "--evidence",
            str(report),
            "--api-url",
            fake_jev.url,
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, captured.out  # exactly one not_started JSON document
    payload = json.loads(lines[0])
    assert payload["status"] == "not_started"
    assert payload["failure_kind"] == "evidence_error"
    assert payload["decision_requests"] == 0
    assert payload["verified"] is False
    # Path.exists may itself be the injected failure, so check existence without touching it.
    assert not os.path.lexists(str(workspace))
    assert fake_jev.requests == []
    assert not os.path.lexists(str(report))
    assert "sentinel-evidence-path-detail" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err

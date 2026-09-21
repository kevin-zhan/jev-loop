"""The offline doctor: stable exit codes, no network, and no secret-derived output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev_loop import doctor

SENTINEL = "doctor-sentinel-credential"
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_MANIFEST = REPO_ROOT / "examples" / "live-files" / "bundle.json"


def run(capsys, argv):
    code = doctor.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def report(out: str) -> dict:
    return json.loads(out)


def test_present_credential_reports_presence_without_any_value(monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, err = run(capsys, ["--json"])

    assert code == doctor.EXIT_OK
    payload = report(out)
    assert payload["mode"] == "preflight"
    assert payload["credential_required"] is True
    assert payload["network_requests"] == 0
    assert payload["ok"] is True
    assert payload["configuration_ready"] is True
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    assert statuses["credential"] == "ok"
    assert statuses["authentication"] == "not_checked"
    assert statuses["connectivity"] == "not_checked"
    assert statuses["model_availability"] == "not_checked"
    assert SENTINEL not in out
    assert SENTINEL not in err
    assert "sha256" not in out.lower()


def test_missing_credential_exits_two_and_names_the_variable(monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code, out, err = run(capsys, ["--json"])

    assert code == doctor.EXIT_CREDENTIAL
    payload = report(out)
    assert payload["ok"] is False
    assert payload["configuration_ready"] is False
    credential = next(check for check in payload["checks"] if check["name"] == "credential")
    assert credential["status"] == "fail"
    assert "TYPESAFE_API_KEY" in credential["detail"]
    assert err == ""


def test_offline_mode_does_not_require_a_credential(monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code, out, _ = run(capsys, ["--offline", "--json"])

    assert code == doctor.EXIT_OK
    payload = report(out)
    assert payload["mode"] == "preflight"
    assert payload["credential_required"] is False
    assert payload["configuration_ready"] is True
    credential = next(check for check in payload["checks"] if check["name"] == "credential")
    assert credential["status"] == "skipped"


@pytest.mark.parametrize(
    "argv",
    [
        ["--api-url", "http://api.example.com/v1"],
        ["--api-url", "https://user:pass@api.example.com/v1"],
        ["--timeout", "0"],
        ["--model", " "],
    ],
)
def test_invalid_non_secret_configuration_exits_three(monkeypatch, capsys, argv):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", *argv])

    assert code == doctor.EXIT_CONFIG
    payload = report(out)
    assert payload["ok"] is False
    assert payload["configuration_ready"] is False
    assert SENTINEL not in out


def test_doctor_never_touches_the_network(monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise AssertionError("the doctor must not open a socket")

    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    monkeypatch.setattr("socket.socket.connect", explode)

    code, out, _ = run(capsys, ["--json"])
    assert code == doctor.EXIT_OK
    assert report(out)["configuration_ready"] is True


def test_bundle_manifest_is_checked_without_being_executed(monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(EXAMPLE_MANIFEST)])

    assert code == doctor.EXIT_OK
    payload = report(out)
    check = next(check for check in payload["checks"] if check["name"] == "bundle")
    assert check["status"] == "ok"


def test_an_unsupported_manifest_version_exits_three(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "bundle.json"
    manifest.write_text(json.dumps({"schema_version": 3, "entrypoint": "controller:build"}), encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_CONFIG
    payload = report(out)
    check = next(check for check in payload["checks"] if check["name"] == "bundle")
    assert check["status"] == "fail"
    assert "unsupported schema_version 3" in check["detail"]


def test_an_incomplete_standard_manifest_exits_three(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "bundle.json"
    manifest.write_text(json.dumps({"schema_version": 2, "entrypoint": "controller:build"}), encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_CONFIG
    payload = report(out)
    check = next(check for check in payload["checks"] if check["name"] == "bundle")
    assert check["status"] == "fail"
    assert "missing required field" in check["detail"]


def test_a_standard_manifest_is_accepted_and_names_its_version(monkeypatch, capsys):
    manifest = REPO_ROOT / ".agents" / "jev-bundle" / "offline-switchboard" / "bundle.json"
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_OK
    check = next(check for check in report(out)["checks"] if check["name"] == "bundle")
    assert check["status"] == "ok"
    assert "schema_version 2" in check["detail"]


def test_a_bundle_name_is_resolved_in_the_working_project(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", "project:offline-switchboard"])

    assert code == doctor.EXIT_OK
    check = next(check for check in report(out)["checks"] if check["name"] == "bundle")
    assert check["status"] == "ok"
    assert "schema_version 2" in check["detail"]


def test_a_legacy_manifest_with_a_missing_python_path_fails_the_preflight(monkeypatch, capsys, tmp_path):
    """The base doctor failed this; the shared validator must keep failing it."""
    manifest = tmp_path / "bundle.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "entrypoint": "controller:build", "python_path": "missing-dir"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_CONFIG
    check = next(check for check in report(out)["checks"] if check["name"] == "bundle")
    assert check["status"] == "fail"
    assert "missing_python_path" in check["detail"]


def test_a_legacy_manifest_with_a_present_python_path_still_passes(monkeypatch, capsys):
    """The legacy layout keeps working; only the broken directory is refused."""
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(EXAMPLE_MANIFEST)])
    assert code == doctor.EXIT_OK
    check = next(check for check in report(out)["checks"] if check["name"] == "bundle")
    assert check["status"] == "ok"


def test_a_scaffold_is_reported_as_not_startable(monkeypatch, capsys, tmp_path):
    # A v2 manifest must live in a directory with the same name, however it is addressed.
    directory = tmp_path / "doctor-scaffold"
    directory.mkdir()
    manifest = directory / "bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": "doctor-scaffold",
                "version": "0.1.0",
                "description": "a scaffold",
                "entrypoint": "bundle_controller:build",
                "scaffold": True,
            }
        ),
        encoding="utf-8",
    )
    (directory / "BUNDLE.md").write_text("# scaffold\n", encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    check = next(check for check in report(out)["checks"] if check["name"] == "bundle")
    assert check["status"] == "ok"
    assert "scaffold=true" in check["detail"]


def test_bundle_config_supplies_the_effective_values(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entrypoint": "controller:build",
                "python_path": ".",
                "config": {"api_url": "http://api.example.com/v1"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_CONFIG  # the bundle's own endpoint is what gets checked
    payload = report(out)
    endpoint = next(check for check in payload["checks"] if check["name"] == "endpoint")
    assert endpoint["status"] == "fail"


def test_exit_codes_are_documented_in_help(capsys):
    with pytest.raises(SystemExit) as caught:
        doctor._parser().parse_args(["--help"])
    assert caught.value.code == 0
    assert "exit codes" in capsys.readouterr().out


def test_human_report_states_what_was_not_checked(monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, [])
    assert code == doctor.EXIT_OK
    assert "configuration-ready: yes" in out
    assert "were not checked" in out
    assert "not_checked" in out
    assert "live-ready" not in out

    monkeypatch.delenv("TYPESAFE_API_KEY")
    code, out, _ = run(capsys, [])
    assert code == doctor.EXIT_CREDENTIAL
    assert "configuration-ready: no" in out


def test_usage_error_exits_three_with_a_stable_json_report(capsys):
    code, out, err = run(capsys, ["--json", "--timeout", "abc"])

    assert code == doctor.EXIT_CONFIG
    payload = report(out)
    assert payload["mode"] == "preflight"
    assert payload["ok"] is False
    assert payload["configuration_ready"] is False
    assert payload["network_requests"] == 0
    assert any(check["name"] == "usage" and check["status"] == "fail" for check in payload["checks"])
    assert "abc" not in out  # the raw argument is never echoed
    assert err == ""


def test_usage_error_without_json_is_still_a_configuration_failure(capsys):
    code, out, _ = run(capsys, ["--timeout", "abc"])
    assert code == doctor.EXIT_CONFIG
    assert "configuration-ready: no" in out
    assert doctor.LIVE_RUN_COMMAND not in out


def test_explicitly_empty_settings_are_not_replaced_by_defaults(monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    for argv in (["--json", "--api-url", ""], ["--json", "--model", ""]):
        code, out, _ = run(capsys, argv)
        assert code == doctor.EXIT_CONFIG, argv
        payload = report(out)
        assert payload["configuration_ready"] is False
        failed = [check for check in payload["checks"] if check["status"] == "fail"]
        assert any(check["name"] == "settings" for check in failed), payload


def test_manifest_config_with_a_wrong_type_is_refused_not_coerced(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entrypoint": "controller:build",
                "python_path": ".",
                "config": {"model": 5},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_CONFIG
    payload = report(out)
    failed = [check for check in payload["checks"] if check["status"] == "fail"]
    assert any(check["name"] == "settings" for check in failed), payload
    assert not any(check["name"] == "model" and check["status"] == "ok" for check in payload["checks"])


def test_unexpected_error_returns_a_stable_secret_free_json_report(monkeypatch, capsys):
    def explode(**kwargs):
        raise RuntimeError("provider-body-marker should never surface")

    monkeypatch.setattr(doctor, "build_report", explode)
    code, out, err = run(capsys, ["--json"])

    assert code == doctor.EXIT_UNEXPECTED
    payload = report(out)
    assert payload["ok"] is False
    assert payload["network_requests"] == 0
    assert any(check["name"] == "internal" and check["status"] == "fail" for check in payload["checks"])
    assert "provider-body-marker" not in out
    assert "RuntimeError" not in out
    assert err == ""


def test_next_commands_follow_the_preflight_state(monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json"])
    assert code == doctor.EXIT_OK
    payload = report(out)
    assert payload["next_commands"] == [doctor.LIVE_RUN_COMMAND]
    assert any("authorization" in note for note in payload["next_notes"])

    monkeypatch.delenv("TYPESAFE_API_KEY")
    code, out, _ = run(capsys, ["--json"])
    assert code == doctor.EXIT_CREDENTIAL
    payload = report(out)
    assert payload["next_commands"] == [doctor.OFFLINE_DEMO_COMMAND]
    assert doctor.LIVE_RUN_COMMAND not in payload["next_commands"]

    code, out, _ = run(capsys, ["--offline", "--json"])
    assert code == doctor.EXIT_OK
    payload = report(out)
    assert payload["next_commands"] == [doctor.OFFLINE_DEMO_COMMAND]
    assert doctor.LIVE_RUN_COMMAND not in payload["next_commands"]


def test_human_report_never_recommends_the_paid_command_when_not_ready(monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code, out, _ = run(capsys, [])
    assert code == doctor.EXIT_CREDENTIAL
    assert doctor.LIVE_RUN_COMMAND not in out
    assert doctor.OFFLINE_DEMO_COMMAND in out


@pytest.mark.parametrize(
    "config",
    [
        {"api_url": None},
        {"model": None},
        {"timeout": None},
        {"model": 5},
        {"api_url": 5},
        {"timeout": "20"},
    ],
)
def test_manifest_null_or_wrong_typed_values_are_not_the_default(monkeypatch, capsys, tmp_path, config):
    manifest = tmp_path / "bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entrypoint": "controller:build",
                "python_path": ".",
                "config": config,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_CONFIG, config
    payload = report(out)
    failed = [check for check in payload["checks"] if check["status"] == "fail"]
    assert any(check["name"] == "settings" for check in failed), payload
    # an explicitly null/invalid manifest value must not be reported as the effective default
    assert not any(
        check["name"] in {"endpoint", "model", "timeout"} for check in payload["checks"]
    ), payload


def test_manifest_absent_keys_use_defaults_and_valid_overrides_are_kept(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "bundle.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entrypoint": "controller:build",
                "python_path": ".",
                "config": {"model": "jev-1.13.0"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    code, out, _ = run(capsys, ["--json", "--bundle", str(manifest)])

    assert code == doctor.EXIT_OK
    payload = report(out)
    checks = {check["name"]: check["status"] for check in payload["checks"]}
    assert checks["endpoint"] == "ok"  # absent key: the default is used
    assert checks["model"] == "ok"  # explicit valid override is kept
    model_detail = next(check["detail"] for check in payload["checks"] if check["name"] == "model")
    assert model_detail == "jev-1.13.0"

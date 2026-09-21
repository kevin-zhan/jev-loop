"""The host-neutral CLI: stable envelopes, honest exit codes and portability."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jev_loop import cli
from jev_loop.bundles.discovery import discovery_root

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_cli(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(item for item in sys.path if item)
    return subprocess.run(
        [sys.executable, "-m", "jev_loop.cli", *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(cwd or REPO_ROOT),
        env=environment,
    )


def envelope(completed: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_bundle_list_finds_the_reference_bundle(capsys: pytest.CaptureFixture[str]) -> None:
    completed = run_cli("bundle", "list", "--project-root", str(REPO_ROOT), "--json")
    assert completed.returncode == 0
    payload = envelope(completed)
    assert payload["ok"] is True and payload["action"] == "bundle.list"
    assert payload["result"]["valid"] == ["offline-switchboard"]
    assert payload["result"]["discovery_root"] == str(discovery_root(REPO_ROOT))
    entry = payload["result"]["bundles"][0]
    assert entry["status"] == "ok"
    assert entry["provenance"] == ".agents/jev-bundle/offline-switchboard/bundle.json"


def test_bundle_show_reports_the_contract_not_a_claim(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    cli.main(["bundle", "init", "shown-bundle", "--project-root", str(target)])
    completed = run_cli("bundle", "show", "project:shown-bundle", "--project-root", str(target), "--json")
    payload = envelope(completed)
    assert payload["ok"] is True
    manifest = payload["result"]["manifest"]
    assert manifest["scaffold"] is True
    assert manifest["discovery_relative"].endswith(".agents/jev-bundle/shown-bundle/bundle.json")
    assert payload["result"]["not_checked"]


def test_a_failed_validation_exits_three_with_a_safe_envelope(tmp_path: Path) -> None:
    completed = run_cli("bundle", "validate", "project:absent", "--project-root", str(tmp_path), "--json")
    assert completed.returncode == 3
    payload = envelope(completed)
    assert payload["ok"] is False and payload["exit_code"] == 3
    assert payload["error"].startswith("unresolved_reference:")
    assert "Traceback" not in completed.stdout + completed.stderr


def test_usage_errors_are_reported_without_echoing_the_argument() -> None:
    completed = run_cli("bundle", "show")
    assert completed.returncode == 3
    assert "run jev-loop --help" in completed.stderr
    assert completed.stdout == ""

    leaked = run_cli("bundle", "show", "SECRET-VALUE=abc", "--json", "--project-root", str(REPO_ROOT))
    assert leaked.returncode == 3
    payload = envelope(leaked)
    assert payload["ok"] is False
    assert "SECRET-VALUE" not in leaked.stdout + leaked.stderr
    assert "the given bundle reference" in payload["error"]


def test_a_recognisable_reference_is_still_named_in_the_error(tmp_path: Path) -> None:
    completed = run_cli("bundle", "validate", "project:absent", "--project-root", str(tmp_path), "--json")
    assert "absent" in envelope(completed)["error"]


def test_help_is_a_success_and_documents_the_exit_codes() -> None:
    completed = run_cli("--help")
    assert completed.returncode == 0
    assert "exit codes" in completed.stdout
    nested = run_cli("bundle", "--help")
    assert nested.returncode == 0
    assert "conformance" in nested.stdout


def test_init_reports_where_it_wrote_and_refuses_a_second_time(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    first = run_cli("bundle", "init", "first-bundle", "--project-root", str(target), "--json")
    assert first.returncode == 0
    payload = envelope(first)
    assert payload["result"]["created"]["scaffold"] is True
    assert "never that a user's goal was implemented" in payload["result"]["note"]

    created_dir = Path(payload["result"]["created"]["directory"])
    marker = created_dir / "user-note.txt"
    marker.write_text("mine\n", encoding="utf-8")
    second = run_cli("bundle", "init", "first-bundle", "--project-root", str(target), "--json")
    assert second.returncode == 3
    assert marker.read_text(encoding="utf-8") == "mine\n"


def test_conformance_exit_code_follows_the_evidence(tmp_path: Path) -> None:
    completed = run_cli(
        "bundle", "conformance", "project:offline-switchboard", "--project-root", str(REPO_ROOT),
        "--run-tests", "--json",
    )
    assert completed.returncode == 0
    payload = envelope(completed)
    assert payload["result"]["author_tests"]["status"] == "passed"
    assert payload["result"]["runtime_semantics"]["status"] == "not_checked"


def test_an_internal_error_still_prints_a_stable_envelope(monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr(cli, "validate_ref", explode)
    code = cli.main(["bundle", "validate", "project:x", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"] == "unexpected internal error; details were withheld"
    assert "internal detail" not in captured.out + captured.err


def test_the_rpc_alias_is_the_host_entry_point(tmp_path: Path) -> None:
    home = tmp_path / "home"
    request = json.dumps({"action": "list", "owner_id": "alias-check"})
    completed = subprocess.run(
        [sys.executable, "-m", "jev_loop.cli", "rpc", "--home", str(home)],
        input=request,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(item for item in sys.path if item)},
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload == {"ok": True, "runs": []}
    unknown = subprocess.run(
        [sys.executable, "-m", "jev_loop.cli", "rpc", "--home", str(home)],
        input=json.dumps({"action": "not_an_action"}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(item for item in sys.path if item)},
    )
    assert unknown.returncode != 0
    assert json.loads(unknown.stdout.strip().splitlines()[-1])["error"] == "unknown host action 'not_an_action'"
    assert not (home / "runs").exists(), "a read-only alias must not create host state"


def test_both_console_scripts_exist_in_packaging_metadata() -> None:
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["jev-loop"] == "jev_loop.cli:main"
    assert project["scripts"]["jev-loop-host"] == "jev_loop.host.cli:main"


def make_project(root: Path, names: tuple[str, ...] = ("demo-bundle",)) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        cli.main(["bundle", "init", name, "--project-root", str(root)])
    return root


@pytest.mark.parametrize(
    "argv",
    [
        ["--project-root", "{root}", "bundle", "list", "--json"],
        ["bundle", "--project-root", "{root}", "list", "--json"],
        ["bundle", "list", "--project-root", "{root}", "--json"],
    ],
    ids=["before-subcommand", "before-action", "after-action"],
)
def test_every_advertised_project_root_position_is_honoured(tmp_path: Path, argv: list[str]) -> None:
    """The flag is advertised at each of these positions, so each must actually select the root."""
    project = make_project(tmp_path / "project")
    completed = run_cli(*[item.format(root=project) for item in argv], cwd=tmp_path)
    assert completed.returncode == 0, completed.stderr
    payload = envelope(completed)
    assert payload["result"]["project_root"] == str(project)
    assert payload["result"]["valid"] == ["demo-bundle"]


def test_without_the_flag_the_current_directory_is_the_project(tmp_path: Path) -> None:
    project = make_project(tmp_path / "cwd-project")
    completed = run_cli("bundle", "list", "--json", cwd=project)
    assert envelope(completed)["result"]["project_root"] == str(project)


def test_help_keeps_advertising_the_project_root_flag() -> None:
    for argv in (["--help"], ["bundle", "--help"], ["bundle", "list", "--help"]):
        completed = run_cli(*argv)
        assert completed.returncode == 0, argv
        assert "--project-root" in completed.stdout, argv


def test_init_writes_into_the_selected_root_from_another_directory(tmp_path: Path) -> None:
    """Not just parse_args: the scaffold must land in the project the flag selected."""
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    completed = run_cli(
        "--project-root", str(project), "bundle", "init", "placed-here", "--json", cwd=elsewhere
    )
    assert completed.returncode == 0, completed.stderr
    created = Path(envelope(completed)["result"]["created"]["directory"])
    assert created == project / ".agents" / "jev-bundle" / "placed-here"
    assert (created / "bundle.json").is_file()
    assert not (elsewhere / ".agents").exists()


def test_the_generated_next_command_runs(tmp_path: Path) -> None:
    """The suggested follow-up must execute, not merely look right."""
    project = make_project(tmp_path / "project")
    completed = run_cli("bundle", "init", "runnable", "--project-root", str(project), "--json")
    created = envelope(completed)["result"]["created"]
    command = created["next_commands"][0]
    assert "conformance" in command and "--run-tests" in command
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), environment.get("PATH", "")])
    environment["PYTHONPATH"] = os.pathsep.join(item for item in sys.path if item)
    executed = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, timeout=180, check=False, env=environment
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert "author tests: passed" in executed.stdout


def test_a_bundle_created_outside_the_project_root_gets_no_doomed_command(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "elsewhere" / "stray"
    completed = run_cli(
        "bundle", "init", "stray", "--project-root", str(project), "--dir", str(outside), "--json"
    )
    assert completed.returncode == 0
    created = envelope(completed)["result"]["created"]
    assert all("bundle validate" not in command and "bundle conformance" not in command
               for command in created["next_commands"])
    assert any("outside" in note for note in created["notes"])
    # The same project root really would refuse it, which is why it must not be suggested.
    refused = run_cli("bundle", "validate", "project:stray", "--project-root", str(project), "--json")
    assert refused.returncode == 3


def test_a_bundle_inside_the_project_but_outside_discovery_is_validated_by_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = project / "vendor" / "inner"
    completed = run_cli("bundle", "init", "inner", "--project-root", str(project), "--dir", str(target), "--json")
    created = envelope(completed)["result"]["created"]
    command = created["next_commands"][0]
    assert str(target / "bundle.json") in command
    assert "project:inner" not in command
    executed = run_cli(*command.split()[1:], cwd=tmp_path)  # drop the leading 'jev-loop'
    assert executed.returncode == 0, executed.stdout + executed.stderr


def test_show_reports_no_discovery_path_for_a_path_addressed_bundle() -> None:
    completed = run_cli("bundle", "show", "examples/bundles/switchboard/bundle.json", "--json")
    manifest = envelope(completed)["result"]["manifest"]
    assert manifest["discovery_relative"] is None
    discovered = run_cli("bundle", "show", "project:offline-switchboard", "--json")
    assert envelope(discovered)["result"]["manifest"]["discovery_relative"] == (
        ".agents/jev-bundle/offline-switchboard/bundle.json"
    )

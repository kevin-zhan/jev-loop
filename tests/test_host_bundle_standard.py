"""Process-level acceptance for the bundle standard through the managed host.

Covers what a static validator cannot: a name-resolved manifest v2 bundle running in a real
worker, the declared task/config contract being enforced before a run exists, the update path
honouring the same contract, the scaffold refusal, and the legacy v1 path still working.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jev_loop.bundles.discovery import discovery_root
from jev_loop.bundles.scaffold import create_bundle
from jev_loop.host.service import ACTIVE_STATUSES, dispatch

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def host_home(tmp_path):
    home = tmp_path / "host"
    yield home
    for status_path in home.glob("runs/*/status.json") if home.is_dir() else ():
        try:
            status = dispatch(
                {"action": "inspect", "owner_id": "standard", "run_id": status_path.parent.name}, home=home
            )["run"]
            if status["status"] in ACTIVE_STATUSES:
                dispatch(
                    {"action": "stop", "owner_id": "standard", "run_id": status["run_id"], "confirm_seconds": 2},
                    home=home,
                )
        except (OSError, ValueError, TimeoutError):
            pass


def start(home: Path, *, key: str = "case-1", **overrides):
    request = {
        "action": "start",
        "owner_id": "standard",
        "idempotency_key": key,
        "project_root": str(REPO_ROOT),
        "bundle": "project:offline-switchboard",
        "task": {"goal": "turn on every required switch"},
        "lease_seconds": 20,
        "max_runtime_seconds": 20,
        **overrides,
    }
    return dispatch(request, home=home)


def wait_for(home: Path, run_id: str, predicate, timeout: float = 10):
    deadline = time.monotonic() + timeout
    status = dispatch({"action": "inspect", "owner_id": "standard", "run_id": run_id}, home=home)["run"]
    while not predicate(status) and time.monotonic() < deadline:
        time.sleep(0.05)
        status = dispatch({"action": "inspect", "owner_id": "standard", "run_id": run_id}, home=home)["run"]
    assert predicate(status), status
    return status


def test_the_reference_bundle_runs_by_name_and_verifies_its_own_result(host_home) -> None:
    started = start(host_home)["run"]
    final = wait_for(host_home, started["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)
    assert final["status"] == "succeeded"
    assert final["resources_released"] is True
    assert final["controller"]["verification"]["verdict"] == "satisfied"
    assert final["controller"]["world"] == {"alpha": True, "beta": True}

    events = dispatch(
        {"action": "events", "owner_id": "standard", "run_id": started["run_id"], "after": 0, "limit": 500},
        home=host_home,
    )["events"]
    created = next(event for event in events if event["kind"] == "run_created")
    assert created["data"]["bundle_name"] == "offline-switchboard"
    assert created["data"]["bundle_format"] == "manifest_v2"
    assert created["data"]["bundle_scaffold"] is False


def test_a_v2_manifest_keeps_working_through_the_legacy_path_form(host_home) -> None:
    manifest = REPO_ROOT / ".agents" / "jev-bundle" / "offline-switchboard" / "bundle.json"
    started = start(host_home, key="by-path", bundle=str(manifest))["run"]
    final = wait_for(host_home, started["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)
    assert final["status"] == "succeeded"


def test_the_legacy_v1_example_still_runs_by_path(host_home) -> None:
    manifest = REPO_ROOT / "examples" / "bundles" / "switchboard" / "bundle.json"
    started = start(
        host_home,
        key="legacy",
        bundle=str(manifest),
        task={"goal": "flip the required switches"},
        resource_keys=[],
    )["run"]
    final = wait_for(host_home, started["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)
    assert final["status"] == "succeeded"
    assert final["resources_released"] is True
    events = dispatch(
        {"action": "events", "owner_id": "standard", "run_id": started["run_id"], "after": 0, "limit": 500},
        home=host_home,
    )["events"]
    created = next(event for event in events if event["kind"] == "run_created")
    assert created["data"]["bundle_format"] == "legacy_manifest_v1"


def test_declared_inputs_are_checked_before_a_run_is_created(host_home) -> None:
    with pytest.raises(ValueError, match="does not match the bundle's inputs_schema"):
        start(host_home, task={"goal": "x", "inputs": {"unexpected": True}})
    assert list(host_home.glob("runs/*")) == []
    assert list(host_home.glob("resource-claims/*.json")) == []


def test_declared_inputs_accept_the_bundle_contract(host_home) -> None:
    started = start(
        host_home,
        key="custom-inputs",
        task={"goal": "turn on gamma", "inputs": {"switches": {"gamma": False}, "required": {"gamma": True}}},
    )["run"]
    final = wait_for(host_home, started["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)
    assert final["controller"]["world"] == {"gamma": True}


def test_declared_config_is_checked_before_a_run_is_created(host_home) -> None:
    with pytest.raises(ValueError, match="does not match the bundle's config_schema"):
        start(host_home, bundle_config={"max_steps": 999})
    assert list(host_home.glob("runs/*")) == []


def test_a_locally_authored_contract_is_enforced_too(host_home, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    bundle = create_bundle(discovery_root(project) / "local-contract", "local-contract").directory
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    manifest["scaffold"] = False
    manifest["inputs_schema"] = {
        "type": "object",
        "properties": {"topic": {"type": "string", "minLength": 1}},
        "required": ["topic"],
        "additionalProperties": False,
    }
    (bundle / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required properties"):
        start(
            host_home,
            key="local-contract-missing",
            project_root=str(project),
            bundle="project:local-contract",
            task={"goal": "x", "inputs": {}},
        )
    with pytest.raises(ValueError, match="unexpected properties"):
        start(
            host_home,
            key="local-contract-extra",
            project_root=str(project),
            bundle="project:local-contract",
            task={"goal": "x", "inputs": {"topic": "ok", "extra": 1}},
        )
    with pytest.raises(ValueError, match="must contain at least 1 characters"):
        start(
            host_home,
            key="local-contract-schema-error",
            project_root=str(project),
            bundle="project:local-contract",
            task={"goal": "x", "inputs": {"topic": ""}},
        )
    assert list(host_home.glob("runs/*")) == []


def make_waiting_bundle(project: Path, name: str, *, scaffold: bool = False) -> Path:
    """A v2 bundle whose controller stays alive, so lifecycle commands can be exercised."""
    directory = discovery_root(project) / name
    directory.mkdir(parents=True)
    manifest = {
        "schema_version": 2,
        "name": name,
        "version": "0.1.0",
        "description": "a waiting bundle",
        "entrypoint": "waiting_controller:build",
        "python_path": ".",
        "inputs_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "minLength": 1}},
            "required": ["topic"],
            "additionalProperties": False,
        },
        "scaffold": scaffold,
    }
    (directory / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "BUNDLE.md").write_text("# waiting bundle\n", encoding="utf-8")
    (directory / "waiting_controller.py").write_text(
        "from jev_loop.host import ControllerResult, ManagedStatus\n"
        "\n"
        "\n"
        "class Waiting:\n"
        "    def __init__(self, spec, services):\n"
        "        self.spec = spec\n"
        "        self.services = services\n"
        "        self._stopped = False\n"
        "\n"
        "    def run(self, stop_event):\n"
        "        while not stop_event.wait(0.05):\n"
        "            pass\n"
        "        return ControllerResult(ManagedStatus.CANCELLED, 'stop_requested')\n"
        "\n"
        "    def snapshot(self):\n"
        "        return {'task': dict(self.spec.task)}\n"
        "\n"
        "    def update(self, task, patch):\n"
        "        self.spec = self.spec  # the host records the update; the controller only accepts it\n"
        "\n"
        "    def request_stop(self, reason):\n"
        "        self._stopped = True\n"
        "\n"
        "\n"
        "def build(spec, services, config):\n"
        "    return Waiting(spec, services)\n",
        encoding="utf-8",
    )
    return directory


def test_an_inputs_update_is_validated_by_the_worker(host_home, tmp_path) -> None:
    project = tmp_path / "project"
    make_waiting_bundle(project, "waiting-bundle")
    started = start(
        host_home,
        key="update-contract",
        project_root=str(project),
        bundle="project:waiting-bundle",
        task={"goal": "wait for lifecycle commands", "inputs": {"topic": "ok"}},
    )["run"]
    run_id = started["run_id"]
    wait_for(host_home, run_id, lambda item: item["status"] == "running")

    with pytest.raises(ValueError, match="task update rejected"):
        dispatch(
            {
                "action": "update",
                "owner_id": "standard",
                "run_id": run_id,
                "expected_config_version": 1,
                "task_patch": {"inputs": {"not_allowed": True}},
            },
            home=host_home,
        )
    with pytest.raises(ValueError, match="task update rejected"):
        dispatch(
            {
                "action": "update",
                "owner_id": "standard",
                "run_id": run_id,
                "expected_config_version": 1,
                "task_patch": {"inputs": {}},
            },
            home=host_home,
        )

    accepted = dispatch(
        {
            "action": "update",
            "owner_id": "standard",
            "run_id": run_id,
            "expected_config_version": 1,
            "task_patch": {"inputs": {"topic": "still fine"}, "constraints": ["read only"]},
        },
        home=host_home,
    )
    assert accepted["config_version"] == 2

    stopped = dispatch(
        {"action": "stop", "owner_id": "standard", "run_id": run_id, "confirm_seconds": 5}, home=host_home
    )
    assert stopped["confirmed"] is True
    assert stopped["run"]["resources_released"] is True


def test_a_scaffold_can_only_be_started_with_an_explicit_escape_hatch(host_home, tmp_path) -> None:
    project = tmp_path / "project"
    create_bundle(discovery_root(project) / "plumbing", "plumbing")
    with pytest.raises(ValueError, match="scaffold=true"):
        start(
            host_home,
            key="scaffold-default",
            project_root=str(project),
            bundle="project:plumbing",
            task={"goal": "x"},
        )

    started = start(
        host_home,
        key="scaffold-explicit",
        project_root=str(project),
        bundle="project:plumbing",
        task={"goal": "plumbing check", "inputs": {"example_input": "plumbing"}},
        allow_scaffold=True,
    )["run"]
    final = wait_for(host_home, started["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)
    # The generated scaffold raises until it is implemented: a scaffold run can never look successful.
    assert final["status"] == "failed"
    events = dispatch(
        {"action": "events", "owner_id": "standard", "run_id": started["run_id"], "after": 0, "limit": 500},
        home=host_home,
    )["events"]
    created = next(event for event in events if event["kind"] == "run_created")
    assert created["data"]["bundle_scaffold"] is True
    assert any(event["kind"] == "error" for event in events)


def test_a_scaffold_is_refused_by_default(host_home, tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    create_bundle(discovery_root(project) / "generated", "generated")
    with pytest.raises(ValueError, match="declares scaffold=true and is refused by default"):
        start(
            host_home,
            key="scaffold",
            project_root=str(project),
            bundle="project:generated",
            task={"goal": "start the scaffold"},
        )
    assert list(host_home.glob("runs/*")) == []


def test_read_only_bundle_actions_need_no_owner_and_change_nothing(host_home) -> None:
    listing = dispatch({"action": "bundle_list", "project_root": str(REPO_ROOT)}, home=host_home)
    assert listing["ok"] is True
    assert listing["valid"] == ["offline-switchboard"]

    shown = dispatch(
        {"action": "bundle_show", "project_root": str(REPO_ROOT), "bundle": "project:offline-switchboard"},
        home=host_home,
    )
    assert shown["bundle"]["manifest"]["name"] == "offline-switchboard"

    validated = dispatch(
        {"action": "bundle_validate", "project_root": str(REPO_ROOT), "bundle": "project:offline-switchboard"},
        home=host_home,
    )
    assert validated["validation"]["ok"] is True
    assert not (host_home / "runs").exists()
    assert not (host_home / "resource-claims").exists()


def test_standalone_cli_lifecycle_without_pi(host_home, tmp_path) -> None:
    """The documented agent flow without pi: cognition, heartbeat and an active stop.

    The bundle here is the built-in ``diagnostic`` probe on purpose: it is the contract probe
    that keeps working while a cognition job is pending.  The reference bundle's own evidence
    is the name-resolved v2 run above; the two proofs stay separate.
    """
    environment = {
        **os.environ,
        "JEV_LOOP_HOME": str(host_home),
        "PYTHONPATH": os.pathsep.join(item for item in sys.path if item),
        "TMPDIR": str(tmp_path / "tmp"),
    }
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)

    def rpc(request: dict) -> dict:
        completed = subprocess.run(
            [sys.executable, "-m", "jev_loop.cli", "rpc"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=str(REPO_ROOT),
            env=environment,
        )
        assert completed.returncode == 0, completed.stderr + completed.stdout
        return json.loads(completed.stdout.strip().splitlines()[-1])

    started = rpc(
        {
            "action": "start",
            "owner_id": "standalone-agent",
            "idempotency_key": "standalone-1",
            "project_root": str(REPO_ROOT),
            "bundle": "diagnostic",
            "task": {"goal": "prove the standalone protocol"},
            "bundle_config": {"tick_seconds": 0.05, "minimum_decisions_while_waiting": 3},
            "max_runtime_seconds": 30,
            "lease_seconds": 30,
        }
    )
    run_id = started["run"]["run_id"]

    deadline = time.monotonic() + 20
    status = started["run"]
    while not status.get("pending_cognition") and time.monotonic() < deadline:
        time.sleep(0.05)
        status = rpc({"action": "inspect", "owner_id": "standalone-agent", "run_id": run_id})["run"]
    assert status["pending_cognition"], status

    heartbeat = rpc({"action": "heartbeat", "owner_id": "standalone-agent", "run_id": run_id})
    assert heartbeat["run"]["lease_deadline"] is not None
    assert heartbeat["run"]["status"] in ACTIVE_STATUSES

    job = status["pending_cognition"][0]
    answered = rpc(
        {
            "action": "respond",
            "owner_id": "standalone-agent",
            "run_id": run_id,
            "job_id": job["job_id"],
            "expected_job_version": job["version"],
            "result": {"plan": "finish the standalone probe"},
        }
    )
    assert answered["job"]["status"] == "completed"

    stopped = rpc({"action": "stop", "owner_id": "standalone-agent", "run_id": run_id, "confirm_seconds": 5})
    assert stopped["accepted"] in {True, False}
    assert stopped["run"]["status"] not in ACTIVE_STATUSES
    assert stopped["run"]["resources_released"] is True

# --- one static gate: start refuses what validate reports as an error ----------------------------


def test_start_refuses_the_same_errors_as_bundle_validate(host_home, tmp_path) -> None:
    """Three real negatives: each must be refused before any run, journal, claim or worker."""
    project = tmp_path / "project"
    bundles = project / ".agents" / "jev-bundle"
    bundles.mkdir(parents=True)

    (bundles / "bad-python").mkdir()
    (bundles / "bad-python" / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": "bad-python",
                "version": "0.1.0",
                "description": "python_path points nowhere",
                "entrypoint": "bundle_controller:build",
                "python_path": "nope",
            }
        ),
        encoding="utf-8",
    )
    (bundles / "bad-python" / "BUNDLE.md").write_text("# x\n", encoding="utf-8")

    (bundles / "token-left").mkdir()
    (bundles / "token-left" / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": "token-left",
                "version": "0.1.0",
                "description": "an unrendered template token remains",
                "entrypoint": "bundle_controller:build",
            }
        ),
        encoding="utf-8",
    )
    (bundles / "token-left" / "BUNDLE.md").write_text("# __BUNDLE_NAME__\n", encoding="utf-8")
    (bundles / "token-left" / "bundle_controller.py").write_text(
        "def build(spec, services, config):\n    raise SystemExit(3)\n", encoding="utf-8"
    )

    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": "loose",
                "version": "0.1.0",
                "description": "no BUNDLE.md next to it",
                "entrypoint": "bundle_controller:build",
            }
        ),
        encoding="utf-8",
    )
    (loose / "bundle_controller.py").write_text(
        "def build(spec, services, config):\n    raise SystemExit(3)\n", encoding="utf-8"
    )

    cases = [
        ("project:bad-python", str(project), "missing_python_path"),
        ("project:token-left", str(project), "unrendered_template_token"),
        (str(loose / "bundle.json"), str(tmp_path), "missing_instructions"),
    ]
    for index, (reference, root, expected_code) in enumerate(cases):
        with pytest.raises(ValueError, match=expected_code):
            start(
                host_home,
                key=f"static-gate-{index}",
                project_root=root,
                bundle=reference,
                task={"goal": "must not start"},
            )
    # Nothing was created for any of them: no run directory, no idempotency mapping, no claim.
    assert list(host_home.glob("runs/*")) == []
    assert list(host_home.glob("idempotency/*")) == []
    assert list(host_home.glob("resource-claims/*.json")) == []


def test_a_bundle_with_only_warnings_and_advisories_still_starts(host_home, tmp_path) -> None:
    """Warnings and advisory declarations are not errors: the gate must not over-refuse."""
    project = tmp_path / "project"
    project.mkdir()
    create_bundle(discovery_root(project) / "warn-only", "warn-only")
    manifest = json.loads((discovery_root(project) / "warn-only" / "bundle.json").read_text(encoding="utf-8"))
    manifest["scaffold"] = False
    (discovery_root(project) / "warn-only" / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    started = start(
        host_home,
        key="warn-only",
        project_root=str(project),
        bundle="project:warn-only",
        task={"goal": "plumbing", "inputs": {"example_input": "x"}},
    )["run"]
    assert started["run_id"]
    final = wait_for(host_home, started["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)
    # The generated scaffold controller raises NotImplementedError: the run reached the worker and the
    # controller decided, which is exactly what "the gate did not refuse it" means.
    assert final["status"] == "failed"
    assert final["stop_reason"] == "controller_error"
    assert "NotImplementedError" in (final.get("last_error") or "")


def test_partial_config_defaults_pass_statically_and_are_completed_by_the_caller(host_home, tmp_path) -> None:
    """Static check accepts partial defaults; the merged config is what start validates strictly."""
    project = tmp_path / "project"
    make_waiting_bundle(project, "partial-config")
    bundle_dir = discovery_root(project) / "partial-config"
    manifest = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    manifest["config"] = {"a": 1}
    manifest["config_schema"] = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }
    (bundle_dir / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    validated = dispatch(
        {"action": "bundle_validate", "project_root": str(project), "bundle": "project:partial-config"},
        home=host_home,
    )
    assert validated["validation_ok"] is True, validated["validation"]["errors"]

    with pytest.raises(ValueError, match="does not match the bundle's config_schema"):
        start(
            host_home,
            key="partial-config-missing",
            project_root=str(project),
            bundle="project:partial-config",
            task={"goal": "wait", "inputs": {"topic": "ok"}},
        )

    started = start(
        host_home,
        key="partial-config-complete",
        project_root=str(project),
        bundle="project:partial-config",
        task={"goal": "wait", "inputs": {"topic": "ok"}},
        bundle_config={"b": 2},
    )["run"]
    assert started["status"] in ACTIVE_STATUSES
    dispatch(
        {"action": "stop", "owner_id": "standard", "run_id": started["run_id"], "confirm_seconds": 5},
        home=host_home,
    )


def test_bundle_validate_keeps_the_request_and_verdict_meanings_apart(host_home, tmp_path) -> None:
    good = dispatch(
        {"action": "bundle_validate", "project_root": str(REPO_ROOT), "bundle": "project:offline-switchboard"},
        home=host_home,
    )
    assert good["ok"] is True and good["validation_ok"] is True and good["validation"]["ok"] is True
    assert good["validation"]["errors"] == []

    bad = dispatch(
        {"action": "bundle_validate", "project_root": str(tmp_path), "bundle": "project:absent"}, home=host_home
    )
    assert bad["ok"] is True, "the request itself was processed"
    assert bad["validation_ok"] is False
    assert bad["validation"]["ok"] is False
    assert bad["validation"]["errors"]

def test_validate_and_start_can_never_disagree(host_home, tmp_path) -> None:
    """The invariant behind the shared gate: every static error is also a start refusal.

    Each fixture is first judged by the shared validator, then handed to `start`; the code that
    stopped the start must be the same first code the validator reported.  A bundle whose only
    findings are warnings/advisories must not be refused by the gate.
    """
    from jev_loop.bundles.validate import validate_ref

    project = tmp_path / "project"
    bundles = project / ".agents" / "jev-bundle"
    bundles.mkdir(parents=True)

    def write(name: str, manifest: dict, *, instructions: str | None = "# x\n", files: dict | None = None) -> None:
        directory = bundles / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
        if instructions is not None:
            (directory / "BUNDLE.md").write_text(instructions, encoding="utf-8")
        for filename, content in (files or {}).items():
            (directory / filename).write_text(content, encoding="utf-8")

    base = {"schema_version": 2, "version": "0.1.0", "description": "fixture", "entrypoint": "bundle_controller:build"}
    write("bad-version", {**base, "name": "bad-version", "schema_version": 3})
    write("name-mismatch", {**base, "name": "something-else"})
    write("no-instructions", {**base, "name": "no-instructions"}, instructions=None)
    write("extra-field", {**base, "name": "extra-field", "surprise": 1})
    write("escape-python-path", {**base, "name": "escape-python-path", "python_path": ".."})
    write("bad-python-path", {**base, "name": "bad-python-path", "python_path": "nope"})
    write("unrendered", {**base, "name": "unrendered"}, instructions="# __BUNDLE_NAME__\n")
    write("bad-schema", {**base, "name": "bad-schema", "inputs_schema": {"oneOf": [{"type": "string"}]}})
    write(
        "bad-default",
        {
            **base,
            "name": "bad-default",
            "config": {"a": "not-an-integer"},
            "config_schema": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
        },
    )
    write(
        "warn-only",
        {
            **base,
            "name": "warn-only",
            "config": {"a": 1},
            "config_schema": {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
            "scaffold": False,
        },
    )
    write("scaffold-flag", {**base, "name": "scaffold-flag", "scaffold": True})

    checked = 0
    for name in sorted(path.name for path in bundles.iterdir()):
        report = validate_ref(f"project:{name}", project)
        if not report["ok"]:
            first = report["errors"][0]
            with pytest.raises(ValueError) as caught:
                start(
                    host_home,
                    key=f"divergence-{name}",
                    project_root=str(project),
                    bundle=f"project:{name}",
                    task={"goal": "must not start", "inputs": {"example_input": "x"}},
                )
            message = str(caught.value)
            if first["code"] == "unresolved_reference":
                # A manifest that cannot even be parsed fails during resolution; both paths still
                # refuse, and the start must explain the same underlying reason.
                reason = first["detail"].split("is not usable: ", 1)[-1]
                assert reason in message, (name, reason, message)
            else:
                assert first["code"] in message, (name, first["code"], message)
            checked += 1
        else:
            # Not refused by the static gate: a run may still fail later for its own reasons, but the
            # gate itself must not be the thing that refuses it.
            assert not report["errors"], name
    assert checked >= 9, f"the fixture set must actually exercise the gate (checked {checked})"
    assert list(host_home.glob("runs/*")) == []


def test_allow_scaffold_requires_a_real_boolean(host_home, tmp_path) -> None:
    """Only the literal true opts in; a truthy string, integer or float must not authorize it."""
    project = tmp_path / "project"
    project.mkdir()
    create_bundle(discovery_root(project) / "typed-gate", "typed-gate")
    base = {
        "owner_id": "standard",
        "project_root": str(project),
        "bundle": "project:typed-gate",
        "task": {"goal": "x", "inputs": {"example_input": "x"}},
    }

    for index, value in enumerate(["false", "true", 1, 0, 1.0, "yes", [], {}]):
        with pytest.raises(ValueError, match="allow_scaffold must be a JSON boolean"):
            start(host_home, key=f"typed-{index}", **base, allow_scaffold=value)

    # Nothing was created by any of the wrong-typed requests.
    assert list(host_home.glob("runs/*")) == []
    assert list(host_home.glob("idempotency/*")) == []
    assert list(host_home.glob("resource-claims/*.json")) == []

    # The literal booleans keep their meaning.
    with pytest.raises(ValueError, match="declares scaffold=true and is refused by default"):
        start(host_home, key="typed-false", **base, allow_scaffold=False)
    accepted = start(host_home, key="typed-true", **base, allow_scaffold=True)["run"]
    assert accepted["run_id"]
    wait_for(host_home, accepted["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)


def test_explicit_null_inputs_are_not_normalised_into_an_empty_object(host_home, tmp_path) -> None:
    """Omitted inputs default to {}; an explicit null or any non-object must fail."""
    project = tmp_path / "project"
    make_waiting_bundle(project, "input-types")
    bundle_dir = discovery_root(project) / "input-types"
    manifest = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    manifest["inputs_schema"] = {"type": "object", "properties": {"topic": {"type": "string"}}}
    (bundle_dir / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    for index, inputs in enumerate([None, [], "text", 5, False]):
        with pytest.raises(ValueError, match="task.inputs must be a JSON object"):
            start(
                host_home,
                key=f"inputs-{index}",
                project_root=str(project),
                bundle="project:input-types",
                task={"goal": "x", "inputs": inputs},
            )
    assert list(host_home.glob("runs/*")) == []

    # Omitted inputs are the only thing that defaults to an empty object.
    omitted = start(
        host_home,
        key="inputs-omitted",
        project_root=str(project),
        bundle="project:input-types",
        task={"goal": "x"},
    )["run"]
    assert omitted["status"] in ACTIVE_STATUSES
    dispatch(
        {"action": "stop", "owner_id": "standard", "run_id": omitted["run_id"], "confirm_seconds": 5},
        home=host_home,
    )


def test_a_present_bundle_config_must_be_an_object_for_a_standard_bundle(host_home, tmp_path) -> None:
    """null/false/[]/""/pair-lists must not be silently turned into {} before the contract check."""
    project = tmp_path / "project"
    make_waiting_bundle(project, "config-types")
    for index, value in enumerate([None, False, [], "", [["a", 1]], 5, "text"]):
        with pytest.raises(ValueError, match="bundle_config must be a JSON object when present"):
            start(
                host_home,
                key=f"config-{index}",
                project_root=str(project),
                bundle="project:config-types",
                task={"goal": "x", "inputs": {"topic": "ok"}},
                bundle_config=value,
            )
    assert list(host_home.glob("runs/*")) == []

    accepted = start(
        host_home,
        key="config-object",
        project_root=str(project),
        bundle="project:config-types",
        task={"goal": "x", "inputs": {"topic": "ok"}},
        bundle_config={"example_setting": 7},
    )["run"]
    assert accepted["status"] in ACTIVE_STATUSES
    dispatch(
        {"action": "stop", "owner_id": "standard", "run_id": accepted["run_id"], "confirm_seconds": 5},
        home=host_home,
    )


def test_legacy_bundle_config_semantics_are_preserved(host_home) -> None:
    """A legacy manifest keeps treating a falsy bundle_config as "no overrides"."""
    manifest = REPO_ROOT / "examples" / "bundles" / "switchboard" / "bundle.json"
    for index, value in enumerate([None, False, [], ""]):
        started = start(
            host_home,
            key=f"legacy-config-{index}",
            bundle=str(manifest),
            task={"goal": "flip the required switches"},
            bundle_config=value,
        )["run"]
        final = wait_for(host_home, started["run_id"], lambda item: item["status"] not in ACTIVE_STATUSES)
        assert final["status"] == "succeeded"
    # A truthy non-object is refused clearly instead of failing with an internal TypeError.
    with pytest.raises(ValueError, match="bundle_config must be a JSON object"):
        start(
            host_home,
            key="legacy-config-int",
            bundle=str(manifest),
            task={"goal": "x"},
            bundle_config=5,
        )

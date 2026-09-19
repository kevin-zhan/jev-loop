"""Process-level acceptance tests for the pi-jev managed host."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jev_loop.host.events import ManagedEventKind
from jev_loop.host.service import ACTIVE_STATUSES, dispatch
from jev_loop.host.store import ManagedRecorder, atomic_write_json
from jev_loop.host.types import RunSpec


@pytest.fixture
def host_home(tmp_path):
    home = tmp_path / "host"
    yield home
    if home.is_dir():
        for status_path in home.glob("runs/*/status.json"):
            try:
                status = dispatch(
                    {
                        "action": "inspect",
                        "owner_id": "session-test",
                        "run_id": status_path.parent.name,
                    },
                    home=home,
                )["run"]
                if status["status"] in ACTIVE_STATUSES:
                    dispatch(
                        {
                            "action": "stop",
                            "owner_id": "session-test",
                            "run_id": status["run_id"],
                            "confirm_seconds": 2,
                        },
                        home=home,
                    )
            except (OSError, ValueError, TimeoutError):
                pass


def start(home: Path, *, key: str = "case-1", **overrides):
    request = {
        "action": "start",
        "owner_id": "session-test",
        "idempotency_key": key,
        "project_root": os.getcwd(),
        "bundle": "diagnostic",
        "task": {"goal": "prove managed cognition"},
        "lease_seconds": 20,
        "max_runtime_seconds": 20,
        **overrides,
    }
    return dispatch(request, home=home)


def inspect(home: Path, run_id: str):
    return dispatch(
        {"action": "inspect", "owner_id": "session-test", "run_id": run_id},
        home=home,
    )["run"]


def wait_for(home: Path, run_id: str, predicate, timeout: float = 5):
    deadline = time.monotonic() + timeout
    status = inspect(home, run_id)
    while not predicate(status) and time.monotonic() < deadline:
        time.sleep(0.025)
        status = inspect(home, run_id)
    assert predicate(status), status
    return status


def test_event_reads_are_cursor_based_and_bounded(host_home):
    run_id = start(host_home, key="events")["run"]["run_id"]
    page = dispatch(
        {"action": "events", "owner_id": "session-test", "run_id": run_id, "after": 0, "limit": 1},
        home=host_home,
    )
    assert len(page["events"]) == 1
    assert page["next_seq"] == page["events"][0]["seq"]
    assert page["has_more"] is True
    following = dispatch(
        {
            "action": "events",
            "owner_id": "session-test",
            "run_id": run_id,
            "after": page["next_seq"],
            "limit": 100,
        },
        home=host_home,
    )
    assert all(event["seq"] > page["next_seq"] for event in following["events"])


def test_cognition_wait_does_not_stop_world_or_control(host_home):
    started = start(host_home)
    run_id = started["run"]["run_id"]
    first = wait_for(host_home, run_id, lambda item: bool(item["pending_cognition"]))
    time.sleep(0.25)
    later = inspect(host_home, run_id)

    assert later["controller"]["world_ticks"] > first["controller"]["world_ticks"]
    assert later["controller"]["decisions_while_waiting"] > first["controller"]["decisions_while_waiting"]
    assert later["controller"]["confirmed_inputs"] == {"forward": True}

    job = later["pending_cognition"][0]
    dispatch(
        {
            "action": "respond",
            "owner_id": "session-test",
            "run_id": run_id,
            "job_id": job["job_id"],
            "expected_job_version": job["version"],
            "result": {"plan": "finish"},
        },
        home=host_home,
    )
    final = wait_for(host_home, run_id, lambda item: item["status"] == "succeeded")
    assert final["output"]["decisions_while_waiting"] >= 3
    assert final["output"]["inputs_released"] is True
    assert final["controller"]["confirmed_inputs"] == {"forward": False}


def test_concurrent_idempotent_starts_create_one_worker(host_home):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(host_home, key="concurrent"), range(2)))
    assert results[0]["run"]["run_id"] == results[1]["run"]["run_id"]
    assert sorted(result["reused"] for result in results) == [False, True]
    listed = dispatch({"action": "list", "owner_id": "session-test"}, home=host_home)["runs"]
    assert [item["run_id"] for item in listed].count(results[0]["run"]["run_id"]) == 1


def test_project_bundle_runs_the_existing_loop_kernel(host_home):
    manifest = Path(os.getcwd()) / "examples" / "bundles" / "switchboard" / "bundle.json"
    started = start(host_home, key="bundle", bundle=str(manifest))
    run_id = started["run"]["run_id"]
    final = wait_for(host_home, run_id, lambda item: item["status"] == "succeeded")
    assert final["controller"]["kernel_status"] == "succeeded"
    assert final["controller"]["executions"] == 2
    assert final["controller"]["world"] == {"research": True, "evidence": True}


def test_cognition_result_must_match_requested_schema(host_home):
    run_id = start(host_home, key="schema")["run"]["run_id"]
    status = wait_for(host_home, run_id, lambda item: bool(item["pending_cognition"]))
    job = status["pending_cognition"][0]
    with pytest.raises(ValueError, match="does not match its schema"):
        dispatch(
            {
                "action": "respond",
                "owner_id": "session-test",
                "run_id": run_id,
                "job_id": job["job_id"],
                "expected_job_version": job["version"],
                "result": {"wrong": "shape"},
            },
            home=host_home,
        )
    assert inspect(host_home, run_id)["pending_cognition"][0]["version"] == job["version"]


def test_start_is_idempotent_and_owner_scoped(host_home):
    first = start(host_home, key="same")
    second = start(host_home, key="same")
    assert second["reused"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]

    with pytest.raises(ValueError, match="not owned"):
        dispatch(
            {
                "action": "inspect",
                "owner_id": "another-session",
                "run_id": first["run"]["run_id"],
            },
            home=host_home,
        )


def test_resource_claim_prevents_two_controllers_and_is_released_on_stop(host_home):
    first = start(host_home, key="resource-a", resource_keys=["browser:profile-a"])
    with pytest.raises(ValueError, match="active under"):
        start(host_home, key="resource-b", resource_keys=["browser:profile-a"])
    dispatch(
        {
            "action": "stop",
            "owner_id": "session-test",
            "run_id": first["run"]["run_id"],
            "confirm_seconds": 3,
        },
        home=host_home,
    )
    second = start(host_home, key="resource-b", resource_keys=["browser:profile-a"])
    assert second["run"]["run_id"] != first["run"]["run_id"]


def test_stop_acknowledgement_is_distinct_from_confirmed_stop_and_releases_inputs(host_home):
    run_id = start(host_home, key="stop")["run"]["run_id"]
    wait_for(host_home, run_id, lambda item: item["controller"].get("confirmed_inputs") == {"forward": True})
    response = dispatch(
        {
            "action": "stop",
            "owner_id": "session-test",
            "run_id": run_id,
            "reason": "test_stop",
            "confirm_seconds": 3,
        },
        home=host_home,
    )
    assert response["accepted"] is True
    assert response["confirmed"] is True
    assert response["run"]["status"] == "cancelled"
    assert response["run"]["resources_released"] is True
    assert response["run"]["controller"]["confirmed_inputs"] == {"forward": False}

    job = response["run"]["cognition"][0]
    with pytest.raises(ValueError, match="already cancelled"):
        dispatch(
            {
                "action": "respond",
                "owner_id": "session-test",
                "run_id": run_id,
                "job_id": job["job_id"],
                "expected_job_version": job["version"],
                "result": {"plan": "too late"},
            },
            home=host_home,
        )


def test_controller_result_can_quarantine_resource_without_a_worker_crash(host_home, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "bundle.json").write_text(
        '{"schema_version":1,"entrypoint":"controller:build","python_path":"."}'
    )
    (project / "controller.py").write_text(
        "from jev_loop.host import ControllerResult, ManagedStatus\n"
        "class Controller:\n"
        "  def run(self, stop_event): return ControllerResult(ManagedStatus.SUCCEEDED, resources_released=False)\n"
        "  def snapshot(self): return {'release_confirmed': False}\n"
        "  def update(self, task, patch): pass\n"
        "  def request_stop(self, reason): pass\n"
        "def build(spec, services, config): return Controller()\n"
    )
    request = {
        "action": "start",
        "owner_id": "session-test",
        "idempotency_key": "unconfirmed-result",
        "project_root": str(project),
        "bundle": str(project / "bundle.json"),
        "task": {"goal": "return without release confirmation"},
        "resource_keys": ["device:unconfirmed"],
        "lease_seconds": 20,
        "max_runtime_seconds": 10,
    }
    first = dispatch(request, home=host_home)["run"]
    final = wait_for(host_home, first["run_id"], lambda item: item["status"] == "succeeded")
    assert final["resources_released"] is False
    with pytest.raises(ValueError, match="quarantined"):
        dispatch({**request, "idempotency_key": "unconfirmed-result-2"}, home=host_home)


def test_worker_crash_quarantines_resources_until_operator_verifies_release(host_home):
    first = start(host_home, key="orphan-a", resource_keys=["device:unsafe"])["run"]
    os.kill(first["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 3
    status = inspect(host_home, first["run_id"])
    while status.get("worker_alive") and time.monotonic() < deadline:
        time.sleep(0.025)
        status = inspect(host_home, first["run_id"])
    assert status["status"] == "failed"
    assert status["resources_released"] is False
    with pytest.raises(ValueError, match="quarantined"):
        start(host_home, key="orphan-b", resource_keys=["device:unsafe"])
    with pytest.raises(ValueError, match="confirmed=true"):
        dispatch(
            {
                "action": "release_resources",
                "owner_id": "session-test",
                "run_id": first["run_id"],
                "confirmed": False,
            },
            home=host_home,
        )
    released = dispatch(
        {
            "action": "release_resources",
            "owner_id": "session-test",
            "run_id": first["run_id"],
            "confirmed": True,
        },
        home=host_home,
    )["run"]
    assert released["resources_released"] is True
    second = start(host_home, key="orphan-b", resource_keys=["device:unsafe"])
    assert second["run"]["run_id"] != first["run_id"]


def test_heartbeat_renews_owner_lease(host_home):
    run_id = start(host_home, key="heartbeat")["run"]["run_id"]
    before = inspect(host_home, run_id)["lease_deadline"]
    time.sleep(0.03)
    renewed = dispatch(
        {"action": "heartbeat", "owner_id": "session-test", "run_id": run_id},
        home=host_home,
    )["run"]
    assert renewed["lease_deadline"] > before


def test_owner_lease_expiry_stops_run_and_releases_inputs(host_home):
    run_id = "run_lease_test"
    run_dir = host_home / "runs" / run_id
    for path in (run_dir, run_dir / "inbox", run_dir / "acks", run_dir / "artifacts"):
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    spec = RunSpec(
        run_id=run_id,
        owner_id="session-test",
        project_root=os.getcwd(),
        bundle="diagnostic",
        task={"goal": "lease safety"},
        lease_seconds=0.3,
        max_runtime_seconds=10,
        stop_grace_seconds=1,
    )
    atomic_write_json(run_dir / "spec.json", spec.to_json())
    recorder = ManagedRecorder(run_dir)
    recorder.emit(
        ManagedEventKind.RUN_CREATED,
        {
            "run_id": run_id,
            "owner_id": spec.owner_id,
            "project_root": spec.project_root,
            "bundle": spec.bundle,
            "task": spec.task,
            "detached": False,
            "lease_seconds": spec.lease_seconds,
            "max_runtime_seconds": spec.max_runtime_seconds,
            "stop_grace_seconds": spec.stop_grace_seconds,
        },
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "jev_loop.host", "worker", "--run-dir", str(run_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait(timeout=5)
    status = inspect(host_home, run_id)
    assert status["status"] == "expired"
    assert status["stop_reason"] == "lease_expired"
    assert status["resources_released"] is True
    assert status["controller"]["confirmed_inputs"] == {"forward": False}


def test_updates_are_version_bound(host_home):
    run_id = start(host_home, key="update")["run"]["run_id"]
    status = inspect(host_home, run_id)
    response = dispatch(
        {
            "action": "update",
            "owner_id": "session-test",
            "run_id": run_id,
            "expected_config_version": status["config_version"],
            "task_patch": {"goal": "updated goal"},
        },
        home=host_home,
    )
    assert response["run"]["task"]["goal"] == "updated goal"
    with pytest.raises(ValueError, match="config version moved"):
        dispatch(
            {
                "action": "update",
                "owner_id": "session-test",
                "run_id": run_id,
                "expected_config_version": status["config_version"],
                "task_patch": {"goal": "stale overwrite"},
            },
            home=host_home,
        )

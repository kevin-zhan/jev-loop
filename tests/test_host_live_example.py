"""Process-level acceptance for the live-files bundle under the managed host.

The API is a loopback fixture (no paid call), but everything else is real: a worker process,
event journal, run-private workspace, verifier evidence and the two-phase stop.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jev_loop.host.service import ACTIVE_STATUSES, dispatch

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "examples" / "live-files" / "bundle.json"
SENTINEL = "host-sentinel-credential"


@pytest.fixture
def host_home(tmp_path):
    home = tmp_path / "host"
    yield home
    if home.is_dir():
        for status_path in home.glob("runs/*/status.json"):
            try:
                status = dispatch(
                    {"action": "inspect", "owner_id": "session-test", "run_id": status_path.parent.name},
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
        "project_root": str(REPO_ROOT),
        "bundle": str(MANIFEST),
        "task": {"goal": "prepare the synthetic inbox"},
        "lease_seconds": 60,
        "max_runtime_seconds": 60,
        **overrides,
    }
    return dispatch(request, home=home)


def inspect(home: Path, run_id: str):
    return dispatch({"action": "inspect", "owner_id": "session-test", "run_id": run_id}, home=home)["run"]


def wait_for(home: Path, run_id: str, predicate, timeout: float = 20):
    deadline = time.monotonic() + timeout
    status = inspect(home, run_id)
    while not predicate(status) and time.monotonic() < deadline:
        time.sleep(0.05)
        status = inspect(home, run_id)
    assert predicate(status), status
    return status


def test_host_bundle_runs_the_live_example_and_exposes_evidence(host_home, fake_jev, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    started = start(host_home, key="live-files-ok", bundle_config={"api_url": fake_jev.url})
    run_id = started["run"]["run_id"]
    final = wait_for(host_home, run_id, lambda item: item["status"] in {"succeeded", "failed", "cancelled"})

    assert final["status"] == "succeeded"
    assert final["resources_released"] is True
    verification = final["controller"]["verification"]
    assert verification["verdict"] == "satisfied", final["controller"]

    run_dir = host_home / "runs" / run_id
    workspace = Path(final["controller"]["workspace"])
    assert workspace.is_relative_to(run_dir / "artifacts")  # run-private, never shared
    assert (workspace / "inbox" / "alpha.txt").read_text(encoding="utf-8") == "ready"
    assert (workspace / "archive" / "gamma.log").read_text(encoding="utf-8") == "noise"
    assert not (workspace / "inbox" / "gamma.log").exists()
    assert (workspace / "inbox" / "beta.txt").read_text(encoding="utf-8") == "ready"

    evidence = json.loads((run_dir / "artifacts" / "verification.json").read_text(encoding="utf-8"))
    assert evidence["verdict"] == "satisfied"
    assert fake_jev.requests

    for path in run_dir.rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text(encoding="utf-8", errors="replace"), path


def test_host_bundle_fails_clearly_without_a_credential(host_home, fake_jev, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    started = start(host_home, key="live-files-nokey", bundle_config={"api_url": fake_jev.url})
    run_id = started["run"]["run_id"]
    final = wait_for(host_home, run_id, lambda item: item["status"] in {"succeeded", "failed", "cancelled"})

    assert final["status"] == "failed"
    assert final["resources_released"] is True
    run_dir = host_home / "runs" / run_id
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "TYPESAFE_API_KEY is not configured" in events
    assert "bundle_load_failed" in events
    # The credential is checked before the environment: no workspace may have been created.
    assert not (run_dir / "artifacts" / "workspace").exists()
    assert fake_jev.requests == []


def test_host_stop_is_two_phase_and_releases_inputs(host_home, fake_jev, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    fake_jev.answer_picker = lambda payload: "blocked"  # suspend after the first decision
    started = start(host_home, key="live-files-stop", bundle_config={"api_url": fake_jev.url})
    run_id = started["run"]["run_id"]
    wait_for(host_home, run_id, lambda item: item["controller"].get("kernel_status") == "suspended")

    accepted = dispatch(
        {
            "action": "stop",
            "owner_id": "session-test",
            "run_id": run_id,
            "reason": "user_requested_stop",
            "confirm_seconds": 5,
        },
        home=host_home,
    )
    assert accepted["ok"] is True  # accepted is not a stop confirmation

    final = wait_for(host_home, run_id, lambda item: item["status"] in {"succeeded", "failed", "cancelled"})
    assert final["status"] == "cancelled"
    assert final["resources_released"] is True


def test_host_bundle_respects_the_configured_step_budget(host_home, fake_jev, monkeypatch):
    """Offline proof that bundle_config max_steps really bounds decision requests."""
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    started = start(host_home, key="live-files-budget", bundle_config={"api_url": fake_jev.url, "max_steps": 1})
    run_id = started["run"]["run_id"]
    final = wait_for(host_home, run_id, lambda item: item["status"] in {"succeeded", "failed", "cancelled"})

    assert final["status"] == "failed"
    assert final["controller"]["max_steps"] == 1
    assert final["controller"]["decision_requests"] == 1
    assert len(fake_jev.requests) == 1  # the default budget of 8 was not silently used
    assert final["resources_released"] is True


def test_host_bundle_rejects_an_invalid_step_budget(host_home, fake_jev, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    for value in (0, -1, 2.5, True, "3"):
        started = start(
            host_home,
            key=f"live-files-bad-{value}",
            bundle_config={"api_url": fake_jev.url, "max_steps": value},
        )
        run_id = started["run"]["run_id"]
        final = wait_for(host_home, run_id, lambda item: item["status"] in {"succeeded", "failed", "cancelled"})
        assert final["status"] == "failed", value
        events = (host_home / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")
        assert "config max_steps must be an integer" in events, value
        assert not (host_home / "runs" / run_id / "artifacts" / "workspace").exists(), value
    assert fake_jev.requests == []


@pytest.mark.parametrize(
    "bad_config",
    [
        {"api_url": None},
        {"model": None},
        {"timeout": None},
        {"model": 5},
        {"api_url": 5},
    ],
)
def test_host_bundle_refuses_null_or_wrong_typed_config_before_the_workspace(
    host_home, fake_jev, monkeypatch, bad_config
):
    monkeypatch.setenv("TYPESAFE_API_KEY", SENTINEL)
    key = "live-files-badcfg-" + "-".join(sorted(bad_config))
    started = start(host_home, key=key, bundle_config=bad_config)
    run_id = started["run"]["run_id"]
    final = wait_for(host_home, run_id, lambda item: item["status"] in {"succeeded", "failed", "cancelled"})

    assert final["status"] == "failed", bad_config
    run_dir = host_home / "runs" / run_id
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "bundle factory" in events, bad_config
    assert not (run_dir / "artifacts" / "workspace").exists(), bad_config
    assert fake_jev.requests == []  # nothing was sent anywhere, including the real endpoint

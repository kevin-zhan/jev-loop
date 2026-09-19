"""Managed host state is an event projection, including cognition lifecycle."""

from __future__ import annotations

import json

from jev_loop.host.events import ManagedEvent, ManagedEventKind
from jev_loop.host.reduce import apply, replay
from jev_loop.host.store import ManagedRecorder, load_events
from jev_loop.host.types import CognitionStatus, ManagedStatus


def events():
    return [
        ManagedEvent(
            1,
            10.0,
            ManagedEventKind.RUN_CREATED,
            {
                "run_id": "run_test",
                "owner_id": "session-1",
                "project_root": "/tmp/project",
                "bundle": "diagnostic",
                "task": {"goal": "test"},
                "resource_keys": ["device:test"],
                "detached": False,
                "lease_seconds": 30,
                "max_runtime_seconds": 300,
                "stop_grace_seconds": 3,
            },
        ),
        ManagedEvent(2, 11.0, ManagedEventKind.WORKER_STARTED, {"pid": 42}),
        ManagedEvent(
            3,
            12.0,
            ManagedEventKind.COGNITION_REQUESTED,
            {
                "job_id": "job_1",
                "question": "plan?",
                "deadline_at": 30.0,
                "context": {"x": 1},
            },
        ),
        ManagedEvent(
            4,
            13.0,
            ManagedEventKind.COGNITION_COMPLETED,
            {"job_id": "job_1", "version": 2, "result": {"plan": "go"}},
        ),
        ManagedEvent(
            5,
            14.0,
            ManagedEventKind.RUN_FINISHED,
            {"status": "succeeded", "output": {"ok": True}},
        ),
    ]


def test_managed_reducer_is_pure_and_replayable():
    first = replay(events())
    second = replay(events())
    assert first == second
    assert first.status is ManagedStatus.SUCCEEDED
    assert first.output == {"ok": True}
    assert first.job("job_1").status is CognitionStatus.COMPLETED
    assert first.job("job_1").result == {"plan": "go"}
    assert first.pending_jobs() == ()


def test_operator_release_confirmation_can_follow_a_terminal_orphan():
    terminal = replay(events())
    assert terminal.resources_released is False
    released = apply(
        terminal,
        ManagedEvent(6, 15.0, ManagedEventKind.RESOURCES_RELEASED, {"reason": "operator_verified_release"}),
    )
    assert released.status is ManagedStatus.SUCCEEDED
    assert released.resources_released is True


def test_recorders_serialize_cross_process_style_appends(tmp_path):
    run_dir = tmp_path / "run"
    first = ManagedRecorder(run_dir)
    first.emit(ManagedEventKind.RUN_CREATED, events()[0].data, at=10.0)
    second = ManagedRecorder.open(run_dir)
    first.emit(ManagedEventKind.ERROR, {"error": "one"}, at=11.0)
    second.emit(ManagedEventKind.ERROR, {"error": "two"}, at=12.0)
    stored = load_events(run_dir / "events.jsonl")
    assert [event.seq for event in stored] == [1, 2, 3]
    assert ManagedRecorder.open(run_dir).state.last_error == "two"


def test_truncated_final_journal_append_is_ignored_but_prior_events_survive(tmp_path):
    path = tmp_path / "events.jsonl"
    first = events()[0]
    path.write_text(json.dumps(first.to_json()) + "\n{\"seq\":2", encoding="utf-8")
    assert load_events(path) == (first,)


def test_apply_does_not_mutate_previous_state():
    created = apply(None, events()[0])
    before = created
    started = apply(created, events()[1])
    assert created == before
    assert created.status is ManagedStatus.STARTING
    assert started.status is ManagedStatus.RUNNING
    assert started.lease_deadline == 41.0

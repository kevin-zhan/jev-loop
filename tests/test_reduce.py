"""State is derived from events, and the reducer is a pure function."""

from __future__ import annotations

from jev_loop import Event, EventKind, ReceiptStatus, RunStatus, StopReason, TaskSpec
from jev_loop.core.reduce import apply, initial_state, replay
from jev_loop.stores.jsonl import JsonlEventStore, MemoryEventStore


def sample_events():
    return [
        Event(1, 1, 0.0, EventKind.RUN_STARTED, {"task": {"goal": "turn a on"}}),
        Event(2, 1, 0.0, EventKind.OBSERVED, {"ok": True, "revision": "rev0", "payload": {"a": False}}),
        Event(3, 1, 0.0, EventKind.FRAME_BUILT, {"frame_id": "f1:aa", "fingerprint": "aa"}),
        Event(4, 1, 0.0, EventKind.DECISION, {"frame_id": "f1:aa", "candidate_id": "a1"}),
        Event(5, 1, 0.0, EventKind.EXECUTION_INTENT, {"operation_id": "op1", "key": "switch:a"}),
        Event(6, 1, 0.0, EventKind.EXECUTION_RECEIPT, {"operation_id": "op1", "key": "switch:a", "status": "pending"}),
        Event(7, 2, 0.1, EventKind.SUSPENDED, {"reason": StopReason.AWAITING_RESULT.value}),
        Event(8, 3, 0.2, EventKind.RESUMED, {"input": "still waiting"}),
        Event(
            9, 3, 0.2, EventKind.OPERATION_RESOLVED,
            {"operation_id": "op1", "key": "switch:a", "status": "completed"},
        ),
        Event(10, 3, 0.2, EventKind.RUN_FINISHED, {"status": RunStatus.SUCCEEDED.value, "stop_reason": None}),
    ]


def test_reducer_is_pure_and_replay_matches():
    state = initial_state("run-1", TaskSpec(goal="turn a on"))
    once = replay(sample_events(), state)
    twice = replay(sample_events(), state)
    assert once == twice

    # Applying the same event to the same state twice gives the same result, with no hidden mutation.
    before = state
    after = apply(before, sample_events()[1])
    assert apply(before, sample_events()[1]) == after
    assert before == state

    assert once.status is RunStatus.SUCCEEDED
    assert once.resume_inputs == ("still waiting",)
    assert once.unresolved == ()
    assert once.attempts[0].status is ReceiptStatus.PENDING
    assert once.frame_visits == {"aa": 1}
    assert once.counters["decisions"] == 1


def test_task_text_changes_only_through_a_task_update_event():
    state = initial_state("run-1", TaskSpec(goal="original goal"))
    for event in sample_events():
        state = apply(state, event)
    assert state.task.goal == "original goal"
    updated = apply(state, Event(11, 3, 0.3, EventKind.TASK_UPDATED, {"goal": "operator changed the goal"}))
    assert updated.task.goal == "operator changed the goal"


def test_jsonl_store_round_trips(tmp_path):
    store = JsonlEventStore(tmp_path / "events.jsonl")
    for event in sample_events():
        store.append(event)
    stored = store.events()
    assert stored == tuple(sample_events())

    memory = MemoryEventStore()
    for event in sample_events():
        memory.append(event)
    assert memory.events() == stored
    assert replay(memory.events(), initial_state("run-1", TaskSpec(goal="turn a on"))) == replay(
        stored, initial_state("run-1", TaskSpec(goal="turn a on"))
    )
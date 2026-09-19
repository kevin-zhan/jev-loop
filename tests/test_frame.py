"""Frame binding: what a decision may and may not refer to."""

from __future__ import annotations

from jev_loop import (
    ActionCandidate,
    Control,
    Decision,
    Effect,
    Event,
    EventKind,
    Frame,
    Loop,
    LoopConfig,
    RunStatus,
    StopReason,
    TaskSpec,
)
from jev_loop.adapters.mock import Fault, FaultKind, GoalPolicy, SwitchboardEnvironment, SwitchboardVerifier
from jev_loop.core.frame import build_frame
from jev_loop.core.reduce import apply, initial_state


class OutOfFramePolicy:
    """Always answers with something that is not in the frame."""

    def __init__(self, candidate_id: str | None = None, frame_id: str | None = None):
        self.candidate_id = candidate_id
        self.frame_id = frame_id

    def decide(self, frame: Frame) -> Decision:
        if self.frame_id is not None:
            return Decision(frame_id=self.frame_id, candidate_id="a1")
        return Decision(frame_id=frame.frame_id, candidate_id=self.candidate_id)


class UngroundedPolicy:
    """Answers with a control that was not offered in this frame."""

    def decide(self, frame: Frame) -> Decision:
        return Decision(frame_id=frame.frame_id, control=Control.WAIT)


def loop_with(policy, config=None, required=None, faults=None):
    required = required or {"a": True}
    env = SwitchboardEnvironment({"a": False, "b": False}, faults=faults)
    return (
        env,
        Loop(
            task=TaskSpec(goal="turn a on"),
            environment=env,
            policy=policy,
            verifier=SwitchboardVerifier(env, required),
            config=config,
        ),
    )


def test_candidate_from_another_frame_is_rejected():
    env, loop = loop_with(OutOfFramePolicy(candidate_id="a99"))
    result = loop.run()
    assert env.calls == []
    assert any(event.kind is EventKind.DECISION_REJECTED for event in loop.timeline())
    assert result.status is RunStatus.SUSPENDED


def test_stale_frame_id_is_rejected():
    env, loop = loop_with(OutOfFramePolicy(frame_id="f0:deadbeef"))
    result = loop.run()
    assert env.calls == []
    assert any(event.kind is EventKind.DECISION_REJECTED for event in loop.timeline())
    assert result.status is RunStatus.SUSPENDED


def test_unoffered_control_is_rejected():
    env, loop = loop_with(UngroundedPolicy(), config=LoopConfig(controls=(Control.REQUEST_FINISH,)))
    result = loop.run()
    assert env.calls == []
    assert any(event.kind is EventKind.DECISION_REJECTED for event in loop.timeline())
    assert result.status is RunStatus.SUSPENDED


def test_dead_action_leaves_the_candidate_set_for_this_observation():
    policy = GoalPolicy({"a": True, "b": True})
    env, loop = loop_with(policy, required={"a": True, "b": True}, faults={"a": Fault(FaultKind.NOOP)})
    loop.run()
    first, second = policy.frames[0], policy.frames[1]
    assert [candidate.key for candidate in first.candidates] == ["switch:a", "switch:b"]
    assert [candidate.key for candidate in second.candidates] == ["switch:b"]
    assert first.fingerprint != second.fingerprint
    assert env.world["b"] is True  # the loop still made progress elsewhere


def test_frame_budget_is_enforced_before_asking():
    policy = GoalPolicy({"a": True})
    env, loop = loop_with(policy, config=LoopConfig(max_options=1))
    result = loop.run()
    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert policy.frames == []


def test_fingerprint_covers_content_not_history_and_ids_are_versioned():
    state = initial_state("run-1", TaskSpec(goal="turn a on"))
    state = apply(state, Event(1, 1, 0.0, EventKind.RUN_STARTED))
    state = apply(
        state, Event(2, 1, 0.0, EventKind.OBSERVED, {"ok": True, "revision": "rev0", "payload": {"a": False}})
    )
    candidates = (
        ActionCandidate(key="switch:a", capability="switch", description="Turn a on.", effect=Effect.LOCAL_WRITE),
    )
    first = build_frame(state, candidates, controls=(), config=LoopConfig())
    later = apply(
        state,
        Event(3, 1, 0.0, EventKind.FRAME_BUILT, {"frame_id": first.frame_id, "fingerprint": first.fingerprint}),
    )
    later = apply(later, Event(4, 1, 0.0, EventKind.STEP, {"frame_id": first.frame_id, "observation": "rev0"}))
    second = build_frame(later, candidates, controls=(), config=LoopConfig())
    assert first.fingerprint == second.fingerprint  # history is not part of the cycle identity
    assert first.frame_id != second.frame_id  # ... but ids stay bound to the version that built them
    assert second.candidate("a1").key == "switch:a"
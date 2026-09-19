"""Offline tests of the loop kernel. No model, no network, no browser."""

from __future__ import annotations

from jev_loop import (
    Control,
    Decision,
    EventKind,
    Loop,
    LoopConfig,
    ReceiptStatus,
    RunStatus,
    StopReason,
    TaskSpec,
)
from jev_loop.adapters.mock import GoalPolicy, SwitchboardEnvironment, SwitchboardVerifier

AUTO: object = object()


def build(switches, required, *, faults=None, policy=None, verifier=AUTO, config=None, clock=None):
    env = SwitchboardEnvironment(switches, faults=faults)
    policy = policy or GoalPolicy(required)
    if verifier is AUTO:
        verifier = SwitchboardVerifier(env, required)
    loop = Loop(
        task=TaskSpec(goal="reach the required switch state", inputs={}, constraints=("local only",)),
        environment=env,
        policy=policy,
        verifier=verifier,
        config=config,
        clock=clock,
    )
    return env, policy, loop


class FixedControlPolicy:
    """Return one control action; after ``wake`` it returns a different one."""

    def __init__(self, control: Control, wake: Control | None = None):
        self.control = control
        self.wake = wake
        self.calls = 0

    def decide(self, frame) -> Decision:
        self.calls += 1
        control = self.control if self.wake is None or self.calls <= 1 else self.wake
        return Decision(frame_id=frame.frame_id, control=control)


def test_completes_and_verifies_with_evidence():
    env, policy, loop = build({"a": False, "b": False}, {"a": True, "b": True})
    result = loop.run()
    assert result.status is RunStatus.SUCCEEDED
    assert result.stop_reason is None
    assert env.world == {"a": True, "b": True}
    assert result.executions == 2
    assert len(policy.frames) == 3  # two actions, then the verification request
    assert all(record.receipt is ReceiptStatus.COMPLETED for record in result.history[:2])
    assert result.history[0].changed is True
    assert any(verification.verdict.value == "satisfied" for verification in result.verifications)


def test_repeated_frame_suspends_without_spending_a_decision():
    policy = FixedControlPolicy(Control.REFRESH)
    env, _, loop = build({"a": False}, {}, policy=policy)
    result = loop.run()
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_EVIDENCE
    assert policy.calls == 1  # the second identical frame never reached the policy
    assert any(event.kind is EventKind.GUARD for event in loop.timeline())


def test_wait_suspends_with_a_wake_condition():
    policy = FixedControlPolicy(Control.WAIT)
    env, _, loop = build({"a": False}, {}, policy=policy)
    result = loop.run()
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_RESULT
    assert policy.calls == 1
    assert result.history[-1].control is Control.WAIT


def test_resume_continues_when_the_world_changed_while_suspended():
    policy = FixedControlPolicy(Control.WAIT, wake=Control.REQUEST_FINISH)
    env, _, loop = build({"a": False}, {}, policy=policy)
    assert loop.run().status is RunStatus.SUSPENDED
    env.external_change("a", True)  # the content arrived while the run was suspended
    loop.resume(input="operator confirms nothing is still loading")
    result = loop.run()
    assert result.status is RunStatus.SUCCEEDED
    assert result.resume_inputs == ("operator confirms nothing is still loading",)
    assert policy.calls == 2


def test_resume_without_new_information_suspends_again():
    policy = FixedControlPolicy(Control.WAIT, wake=Control.REQUEST_FINISH)
    env, _, loop = build({"a": False}, {}, policy=policy)
    assert loop.run().status is RunStatus.SUSPENDED
    loop.resume()
    result = loop.run()
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_EVIDENCE  # same frame, no new evidence


def test_max_steps_is_a_named_failure():
    policy = FixedControlPolicy(Control.REFRESH)
    env, _, loop = build({"a": False}, {}, policy=policy, config=LoopConfig(max_steps=1))
    result = loop.run()
    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.steps == 1


def test_wall_clock_budget_is_a_named_failure():
    ticks = {"n": 0}

    def clock() -> float:
        ticks["n"] += 1
        return 0.0 if ticks["n"] == 1 else 10.0

    env, _, loop = build({"a": False}, {}, config=LoopConfig(max_seconds=1.0), clock=clock)
    result = loop.run()
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.steps == 0


def test_cancel_stops_starting_new_actions():
    env, _, loop = build({"a": False}, {"a": True})
    loop.cancel()
    result = loop.run()
    assert result.status is RunStatus.CANCELLED
    assert result.stop_reason is StopReason.CANCELLED
    assert env.calls == []


def test_observation_failure_suspends_before_any_decision():
    env, policy, loop = build({"a": False}, {"a": True})
    env._fail_after = 0
    result = loop.run()
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_EVIDENCE
    assert policy.frames == []
    assert not any(event.kind is EventKind.DECISION for event in loop.timeline())


def test_missing_verifier_never_counts_as_success():
    policy = FixedControlPolicy(Control.REQUEST_FINISH)
    env, _, loop = build({"a": True}, {"a": True}, policy=policy, verifier=None)
    result = loop.run()
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_EVIDENCE
    assert result.status is not RunStatus.SUCCEEDED
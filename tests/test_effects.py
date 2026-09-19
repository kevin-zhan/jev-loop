"""Execution receipts and their consequences: pending, unknown, delayed, rejected.

The point of these tests is that "the tool call returned" is not the same as "the action
happened", and neither is the same as "the task is done".
"""

from __future__ import annotations

from jev_loop import (
    Control,
    Decision,
    EventKind,
    Idempotency,
    Loop,
    LoopConfig,
    ReceiptStatus,
    RunStatus,
    StopReason,
    TaskSpec,
)
from jev_loop.adapters.mock import Fault, FaultKind, GoalPolicy, SwitchboardEnvironment, SwitchboardVerifier


def build(switches, required, *, faults=None, policy=None, config=None):
    env = SwitchboardEnvironment(switches, faults=faults)
    policy = policy or GoalPolicy(required)
    loop = Loop(
        task=TaskSpec(goal="reach the required switch state"),
        environment=env,
        policy=policy,
        verifier=SwitchboardVerifier(env, required),
        config=config,
    )
    return env, policy, loop


def receipts(loop):
    return [event for event in loop.timeline() if event.kind is EventKind.EXECUTION_RECEIPT]


def test_pending_operation_suspends_and_is_never_resubmitted():
    env, _, loop = build(
        {"a": False},
        {"a": True},
        faults={"a": Fault(FaultKind.PENDING, count=2)},
    )
    first = loop.run()
    assert first.status is RunStatus.SUSPENDED
    assert first.stop_reason is StopReason.AWAITING_RESULT
    assert [call["key"] for call in env.calls] == ["switch:a"]  # submitted exactly once
    assert first.state.unresolved[0].key == "switch:a"
    assert first.state.unresolved[0].idempotency is Idempotency.NONE

    loop.resume()
    result = loop.run()
    assert result.status is RunStatus.SUCCEEDED
    assert [call["key"] for call in env.calls] == ["switch:a"]  # still exactly once
    assert result.state.unresolved == ()


def test_unknown_receipt_is_queried_never_retried():
    env, _, loop = build({"a": False}, {"a": True}, faults={"a": Fault(FaultKind.UNKNOWN)})
    result = loop.run()
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_RESULT

    loop.resume()
    again = loop.run()
    assert again.status is RunStatus.SUSPENDED
    assert len(env.calls) == 1  # an idempotency=NONE action is never blindly repeated
    assert receipts(loop)[0].data["status"] == ReceiptStatus.UNKNOWN.value


def test_rejected_receipt_is_recorded_and_not_reported_as_progress():
    env, _, loop = build({"a": False}, {"a": True}, faults={"a": Fault(FaultKind.REJECT)})
    result = loop.run()
    assert receipts(loop)[0].data["status"] == ReceiptStatus.REJECTED.value
    assert (env.world["a"]) is False
    assert result.status in {RunStatus.SUSPENDED, RunStatus.FAILED}


def test_delayed_effect_is_not_treated_as_a_reason_to_act_again():
    env, _, loop = build({"a": False}, {"a": True}, faults={"a": Fault(FaultKind.DELAY, count=2)})
    result = loop.run()
    assert result.status is RunStatus.SUCCEEDED
    assert [call["key"] for call in env.calls] == ["switch:a"]  # one execution, then the effect arrived
    assert len(receipts(loop)) == 1


def test_executor_failure_becomes_unknown_and_blocks_retry():
    class BrokenEnvironment(SwitchboardEnvironment):
        def execute(self, candidate, intent):
            raise RuntimeError("transport died mid-request")

    env = BrokenEnvironment({"a": False}, faults={"a": Fault(FaultKind.UNKNOWN)})
    loop = Loop(
        task=TaskSpec(goal="turn a on"),
        environment=env,
        policy=GoalPolicy({"a": True}),
        verifier=SwitchboardVerifier(env, {"a": True}),
    )
    result = loop.run()
    assert receipts(loop)[0].data["status"] == ReceiptStatus.UNKNOWN.value
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_RESULT
    assert env.calls == []


class FinishPolicy:
    def decide(self, frame) -> Decision:
        return Decision(frame_id=frame.frame_id, control=Control.REQUEST_FINISH)


def test_verification_is_required_before_success():
    env, _, loop = build({"a": False}, {"a": True}, policy=FinishPolicy())
    result = loop.run()
    assert result.status is not RunStatus.SUCCEEDED
    assert result.stop_reason is StopReason.AWAITING_EVIDENCE  # verifier says unknown... actually unsatisfied
    assert result.verifications[-1].verdict.value in {"unsatisfied", "unknown"}


def test_suspended_run_can_be_resumed_after_user_input():
    env, _, loop = build({"a": False}, {"a": True}, faults={"a": Fault(FaultKind.UNKNOWN)})
    loop.run()
    assert loop.state.status is RunStatus.SUSPENDED
    loop.resume(input="operator confirmed the switch is on")
    assert loop.state.status is RunStatus.RUNNING
    assert loop.state.resume_inputs == ("operator confirmed the switch is on",)


def test_post_observation_is_optional():
    env, _, loop = build(
        {"a": False, "b": False},
        {"a": True},
        config=LoopConfig(post_observe=False),
    )
    result = loop.run()
    assert result.status is RunStatus.SUCCEEDED
    assert env.world["a"] is True
    assert not any(event.kind is EventKind.GUARD for event in loop.timeline())
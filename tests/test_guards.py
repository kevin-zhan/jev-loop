"""Guard behaviour: bounded, named stops instead of silent spinning.

These are the tests that answer "can the loop actually loop": a deterministic policy on a
state that does not change would repeat forever, so the runtime must detect each of the
three ways that happens and end the run with a reason.
"""

from __future__ import annotations

from jev_loop import (
    Control,
    Decision,
    Event,
    EventKind,
    GuardKind,
    Loop,
    LoopConfig,
    RunStatus,
    StopReason,
    TaskSpec,
)
from jev_loop.adapters.mock import Fault, FaultKind, GoalPolicy, SwitchboardEnvironment, SwitchboardVerifier
from jev_loop.core.reduce import apply, initial_state
from jev_loop.policies.scripted import ScriptedPolicy


def build(switches, required, *, faults=None, policy=None, config=None):
    env = SwitchboardEnvironment(switches, faults=faults)
    policy = policy or GoalPolicy(required)
    return (
        env,
        policy,
        Loop(
            task=TaskSpec(goal="reach the required state"),
            environment=env,
            policy=policy,
            verifier=SwitchboardVerifier(env, required),
            config=config,
        ),
    )


def guards(loop):
    return [event for event in loop.timeline() if event.kind is EventKind.GUARD]


class FirstCandidateThenFinish:
    def decide(self, frame: object) -> Decision:
        if not frame.candidates:  # type: ignore[attr-defined]
            return Decision(frame_id=frame.frame_id, control=Control.REQUEST_FINISH)  # type: ignore[attr-defined]
        return Decision(frame_id=frame.frame_id, candidate_id=frame.candidates[0].id)  # type: ignore[attr-defined]


def test_no_effect_action_is_named_and_the_loop_still_makes_progress():
    env, _, loop = build({"a": False, "b": False}, {"a": True, "b": True}, faults={"a": Fault(FaultKind.NOOP)})
    result = loop.run()

    assert any(event.data["kind"] == GuardKind.NO_PROGRESS.value for event in guards(loop))
    assert env.world["b"] is True  # progress was preserved
    assert env.world["a"] is False  # the no-op switch never actually turned on
    assert [call["key"] for call in env.calls].count("switch:a") == 2  # bounded, not infinite
    assert result.status is RunStatus.SUSPENDED  # stops and asks for new information
    assert result.steps <= 6


def test_reversible_cycle_suspends_early_instead_of_spinning():
    policy = ScriptedPolicy(prefer=("switch:a",), fallback="first")
    env, _, loop = build(
        {"a": False, "b": False},
        {"b": True},  # not reachable for this deterministic policy
        faults={"a": Fault(FaultKind.TOGGLE, partner="b")},
        policy=policy,
        config=LoopConfig(max_steps=40),
    )
    result = loop.run()

    assert result.status is RunStatus.SUSPENDED  # the world returned to a frame it had decided on
    assert result.stop_reason is StopReason.AWAITING_EVIDENCE
    assert result.steps < 40  # the guard stopped it, not the step budget
    assert len(env.calls) == 2  # A -> B -> A was detected before the third execution
    assert any(event.data["kind"] == GuardKind.CYCLE.value for event in guards(loop))


def test_repeated_cycles_across_observations_end_with_a_named_failure():
    policy = ScriptedPolicy(prefer=("switch:a",), fallback="first")
    env, _, loop = build(
        {"a": False, "b": False},
        {"b": True},
        faults={"a": Fault(FaultKind.TOGGLE, partner="b")},
        policy=policy,
        config=LoopConfig(max_steps=40, max_frame_visits=99),  # let the repeat detector do the work
    )
    result = loop.run()

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.CYCLE
    assert result.steps < 40
    assert len(env.calls) <= 6
    assert any(event.data["kind"] == GuardKind.REPEAT.value for event in guards(loop))


def test_same_action_on_the_same_observation_is_never_executed_twice():
    env, _, loop = build(
        {"a": False},
        {"a": True},
        faults={"a": Fault(FaultKind.NOOP)},
        policy=FirstCandidateThenFinish(),
        config=LoopConfig(
            post_observe=False,  # isolate the repeat detector from the no-effect detector
            max_frame_visits=99,  # ... and from the frame-revisit detector
        ),
    )
    loop.run()
    assert [call["key"] for call in env.calls] == ["switch:a"]
    assert any(event.data["kind"] == GuardKind.REPEAT.value for event in guards(loop))


def test_refresh_that_changes_nothing_suspends_without_another_decision():
    class RefreshThenFinish:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, frame) -> Decision:
            self.calls += 1
            control = Control.REFRESH if self.calls == 1 else Control.REQUEST_FINISH
            return Decision(frame_id=frame.frame_id, control=control)

    policy = RefreshThenFinish()
    env, _, loop = build({"a": True}, {"a": True}, policy=policy)
    result = loop.run()
    assert result.status is RunStatus.SUSPENDED
    assert result.stop_reason is StopReason.AWAITING_EVIDENCE
    assert policy.calls == 1  # the repeated frame never reached the policy
    assert all(event.data["dead_keys"] == [] for event in guards(loop))


def test_dead_actions_are_scoped_to_the_observation():
    state = initial_state("run", TaskSpec(goal="turn a on"))
    state = apply(state, Event(1, 1, 0.0, EventKind.OBSERVED, {"ok": True, "revision": "rev0", "payload": {}}))
    state = apply(
        state,
        Event(2, 1, 0.0, EventKind.GUARD, {"kind": "no_progress", "dead_keys": ["switch:a"], "escalate": True}),
    )
    assert state.dead_actions == frozenset({"switch:a"})

    same = apply(state, Event(3, 1, 0.0, EventKind.OBSERVED, {"ok": True, "revision": "rev0", "payload": {}}))
    assert same.dead_actions == frozenset({"switch:a"})  # same world: still excluded

    moved = apply(state, Event(4, 1, 0.0, EventKind.OBSERVED, {"ok": True, "revision": "rev1", "payload": {}}))
    assert moved.dead_actions == frozenset()  # new world: the action may matter again
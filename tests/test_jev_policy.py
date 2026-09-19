"""The Jev policy: request shape, exact answer matching, and a full loop against a fake model.

No network call happens here. The fake requester still has to consume the real request shape
and answer with option keys, which is what makes these tests useful: the kernel is exercised
end to end through the actual policy code path.
"""

from __future__ import annotations

import pytest

from jev_loop import Loop, RunStatus, TaskSpec
from jev_loop.adapters.mock import SwitchboardEnvironment, SwitchboardVerifier
from jev_loop.core.events import Event, EventKind
from jev_loop.core.frame import build_frame
from jev_loop.core.reduce import apply, initial_state
from jev_loop.core.types import Control, LoopConfig
from jev_loop.policies.jev import (
    JevPolicy,
    ResponseShapeError,
    build_request,
    parse_response,
)


def make_frame(switches=None, dead=()):
    state = initial_state("run", TaskSpec(goal="turn a on"))
    state = apply(state, Event(1, 1, 0.0, EventKind.RUN_STARTED))
    observed = {"ok": True, "revision": "rev0", "payload": {"switches": switches or {"a": False}}}
    state = apply(state, Event(2, 1, 0.0, EventKind.OBSERVED, observed))
    if dead:
        state = apply(
            state, Event(3, 1, 0.0, EventKind.GUARD, {"kind": "no_progress", "dead_keys": list(dead), "escalate": True})
        )
    env = SwitchboardEnvironment(switches or {"a": False})
    candidates = env.offer(state, state.observation)
    return build_frame(state, candidates, controls=(Control.REQUEST_FINISH, Control.BLOCKED), config=LoopConfig())


def answer(choice, *, confidence=0.9):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "next_action": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {choice: confidence},
            }
        },
        "usage": {"input_tokens": 180},
    }


def test_request_offers_every_candidate_and_control():
    frame = make_frame()
    request = build_request(frame)
    criteria = request["questions"]["next_action"]["criteria"]

    assert set(criteria) == set(frame.option_ids()) | {"request_finish", "blocked"}
    assert request["state"]["goal"] == "turn a on"
    assert request["model"] == "jev-1.13.0"
    for candidate in frame.candidates:
        assert criteria[candidate.id]["args"] == dict(candidate.args)
    assert request["questions"]["next_action"]["type"] == "choice"


def test_request_reports_excluded_actions_to_the_model():
    frame = make_frame(dead=["switch:b"])
    request = build_request(frame)
    assert request["state"]["excluded_actions"] == ["switch:b"]


def test_answer_outside_the_frame_is_a_shape_error():
    frame = make_frame()
    with pytest.raises(ResponseShapeError):
        parse_response(frame, answer("a99"))
    with pytest.raises(ResponseShapeError):
        parse_response(frame, answer("switch:a"))  # the key is not the option id


def test_option_ids_are_never_normalized():
    frame = make_frame()
    with pytest.raises(ResponseShapeError):
        parse_response(frame, answer(" a1"))
    with pytest.raises(ResponseShapeError):
        parse_response(frame, answer("a1 "))


def test_control_answers_are_bound_to_the_frame():
    frame = make_frame()
    decision = parse_response(frame, answer("request_finish"))
    assert decision.control is Control.REQUEST_FINISH and decision.candidate_id is None
    assert decision.model == "jev-1.13.0"
    assert decision.confidence == pytest.approx(0.9)
    assert decision.usage == {"input_tokens": 180}

    with pytest.raises(ResponseShapeError):
        parse_response(frame, answer("wait"))  # offered nowhere in this frame


def fake_requester(required):
    def request(payload):
        for option, description in payload["questions"]["next_action"]["criteria"].items():
            args = description.get("args") or {}
            name = args.get("name")
            if name in required and bool(args.get("value")) == required[name]:
                return answer(option, confidence=0.93)
        return answer("request_finish", confidence=0.8)

    return request


def test_loop_runs_end_to_end_on_the_real_policy_path():
    env = SwitchboardEnvironment({"a": False, "b": False})
    policy = JevPolicy(request_fn=fake_requester({"a": True, "b": True}))
    loop = Loop(
        task=TaskSpec(goal="turn a and b on"),
        environment=env,
        policy=policy,
        verifier=SwitchboardVerifier(env, {"a": True, "b": True}),
    )
    result = loop.run()

    assert result.status is RunStatus.SUCCEEDED
    assert env.world == {"a": True, "b": True}
    assert len(policy.requests) == 3  # two actions, then the finish request
    assert all(request["questions"]["next_action"]["criteria"] for request in policy.requests)
    assert result.decisions == 3
    assert all(record.receipt is not None or record.control is not None for record in result.history)


def test_dead_action_is_absent_from_the_next_request():
    from jev_loop.adapters.mock import Fault, FaultKind

    env = SwitchboardEnvironment({"a": False, "b": False}, faults={"a": Fault(FaultKind.NOOP)})
    policy = JevPolicy(request_fn=fake_requester({"a": True}))
    loop = Loop(
        task=TaskSpec(goal="turn a on"),
        environment=env,
        policy=policy,
        verifier=SwitchboardVerifier(env, {"a": True}),
    )
    loop.run()

    second = policy.requests[1]
    criteria = second["questions"]["next_action"]["criteria"]
    assert not any((entry.get("args") or {}).get("name") == "a" for entry in criteria.values())
    assert second["state"]["excluded_actions"] == ["switch:a"]
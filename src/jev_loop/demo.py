"""Run the loop against the deterministic mock, with no model and no network.

    python -m jev_loop.demo --scenario clean
    python -m jev_loop.demo --scenario noop
    python -m jev_loop.demo --scenario cycle

The three scenarios are the three answers to "can it loop": it advances when the world
changes, it narrows and stops when an action has no effect, and it names the cycle instead
of burning its step budget.
"""

from __future__ import annotations

import argparse

from .adapters.mock import Fault, FaultKind, GoalPolicy, SwitchboardEnvironment, SwitchboardVerifier
from .core.loop import Loop
from .core.types import LoopConfig, TaskSpec, VerifyVerdict


def build(scenario: str):
    if scenario == "clean":
        env = SwitchboardEnvironment({"a": False, "b": False})
        required = {"a": True, "b": True}
    elif scenario == "noop":
        env = SwitchboardEnvironment({"a": False, "b": False}, faults={"a": Fault(FaultKind.NOOP)})
        required = {"a": True, "b": True}
    elif scenario == "cycle":
        env = SwitchboardEnvironment({"a": False, "b": False}, faults={"a": Fault(FaultKind.TOGGLE, partner="b")})
        required = {"b": True}
    else:
        raise SystemExit(f"unknown scenario {scenario!r}")
    policy = GoalPolicy(required) if scenario != "cycle" else _TogglePolicy()
    loop = Loop(
        task=TaskSpec(goal=f"scenario {scenario}: reach {required}"),
        environment=env,
        policy=policy,
        verifier=SwitchboardVerifier(env, required),
        config=LoopConfig(max_steps=12),
    )
    return env, loop


class _TogglePolicy:
    """Deterministic policy that keeps choosing the reversible switch on purpose."""

    def decide(self, frame):
        from .core.types import Decision

        if frame.candidates:
            return Decision(frame_id=frame.frame_id, candidate_id=frame.candidates[0].id)
        from .core.types import Control

        return Decision(frame_id=frame.frame_id, control=Control.REQUEST_FINISH)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="clean", choices=("clean", "noop", "cycle"))
    args = parser.parse_args()

    env, loop = build(args.scenario)
    result = loop.run()

    for event in loop.timeline():
        tracked = {
            "observed", "decision", "execution_receipt", "guard",
            "verification", "suspended", "run_finished", "step",
        }
        if event.kind.value in tracked:
            hide = {"payload", "probabilities"}
            detail = {key: value for key, value in event.data.items() if key not in hide}
            print(f"step {event.step:>2} {event.kind.value:<18} {detail}")

    print()
    print(f"status={result.status.value} stop_reason={result.stop_reason.value if result.stop_reason else None}")
    print(f"steps={result.steps} executions={result.executions} decisions={result.decisions} world={env.world}")
    verdicts = [record.verdict.value for record in result.verifications]
    print(f"verifications={verdicts or 'none'} (only {VerifyVerdict.SATISFIED.value!r} counts as success)")


if __name__ == "__main__":
    main()
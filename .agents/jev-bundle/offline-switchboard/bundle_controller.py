"""Reference controller for the ``offline-switchboard`` bundle.

It hosts the existing synchronous kernel through ``LoopController``: the bundle owns the
environment and the verifier, the kernel owns the loop and the guards, and the host owns
the process, the lease and the resource claims.  Everything here is local and offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jev_loop import Loop, LoopConfig, TaskSpec
from jev_loop.adapters.mock import GoalPolicy, SwitchboardEnvironment, SwitchboardVerifier
from jev_loop.host import LoopController

Json = Any


def build(spec, services, config: Mapping[str, Json]) -> LoopController:
    settings = {**dict(config), **dict(spec.bundle_config)}
    inputs = dict(spec.task.get("inputs") or {})
    switches = {
        str(name): bool(value)
        for name, value in dict(inputs.get("switches") or settings.get("switches") or {}).items()
    }
    required = {
        str(name): bool(value)
        for name, value in dict(inputs.get("required") or settings.get("required") or {}).items()
    }
    max_steps = int(inputs.get("max_steps") or settings.get("max_steps") or 8)
    environment = SwitchboardEnvironment(switches)
    loop = Loop(
        task=TaskSpec(
            goal=str(spec.task["goal"]),
            inputs=inputs,
            constraints=tuple(spec.task.get("constraints") or ()),
            success_criteria=tuple(spec.task.get("success_criteria") or ()),
            authorization=tuple(spec.task.get("authorization") or ()),
        ),
        environment=environment,
        policy=GoalPolicy(required),
        verifier=SwitchboardVerifier(environment, required),
        config=LoopConfig(max_steps=max_steps),
    )
    return LoopController(
        loop,
        services,
        snapshot_extra=lambda: {
            "world": dict(environment.world),
            "required": dict(required),
            "verification": _last_verification(loop),
        },
    )


def _last_verification(loop: Loop) -> Json:
    records = loop.state.verifications
    if not records:
        return None
    latest = records[-1]
    return {
        "verdict": latest.verdict.value,
        "detail": latest.detail,
        "evidence": dict(latest.evidence),
        "step": latest.step,
    }

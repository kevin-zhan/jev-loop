"""Frame construction: bind the state revision, the candidate table, and their ids.

Contract: the id a policy returns is only meaningful inside the frame it was given. The
loop resolves it against that frame's table (never against a newer one) and re-checks the
observation revision before executing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .types import ActionCandidate, Control, Frame, LoopConfig, RuntimeState

Json = Any


class FrameBudgetError(ValueError):
    """The frame would exceed the runtime's own limits (or the model's)."""


def _fingerprint(payload: Mapping[str, Json]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def decision_state(state: RuntimeState, *, config: LoopConfig) -> dict[str, Json]:
    """Project the full run state down to what this decision actually needs.

    Deliberately small: the model is never sent the whole world. History is a compact
    summary, not a transcript, because unrelated detail measurably degrades judgment.
    """
    observation = state.observation
    return {
        "goal": state.task.goal,
        "inputs": dict(state.task.inputs),
        "constraints": list(state.task.constraints),
        "success_criteria": list(state.task.success_criteria),
        "step": state.step,
        "status": state.status.value,
        "observation_stale": state.observation_stale,
        "observation": dict(observation.payload) if observation else {},
        "observation_revision": observation.revision if observation else None,
        "unresolved_operations": [
            {"key": op.key, "status": op.status.value, "step": op.step, "args": dict(op.args)}
            for op in state.unresolved
        ],
        "excluded_actions": sorted(state.dead_actions),
        "recent_steps": [
            {
                "step": record.step,
                "action": record.candidate_key,
                "control": record.control.value if record.control else None,
                "receipt": record.receipt.value if record.receipt else None,
                "changed": record.changed,
            }
            for record in state.history[-config.history_limit :]
        ],
    }


def build_frame(
    state: RuntimeState,
    candidates: Sequence[ActionCandidate],
    *,
    controls: Sequence[Control],
    config: LoopConfig,
) -> Frame:
    """Normalize candidate ids (frame-scoped, deterministic) and freeze the frame.

    Two filters belong to the runtime, not the adapter: actions that already had no
    observable effect on this observation, and logical operations whose result is still
    unresolved. Either one, if left in, lets a deterministic policy re-select it.
    """
    normalized = tuple(replace(candidate, id=f"a{index}") for index, candidate in enumerate(candidates, start=1))
    unresolved = state.unresolved_keys()
    offered = tuple(
        candidate
        for candidate in normalized
        if candidate.key not in unresolved and candidate.key not in state.dead_actions
    )
    options = len(offered) + len(controls)
    if options > config.max_options:
        raise FrameBudgetError(f"{options} options exceeds max_options={config.max_options}")

    projection = decision_state(state, config=config)
    candidates_json = [
        {
            "id": candidate.id,
            "key": candidate.key,
            "capability": candidate.capability,
            "description": candidate.description,
            "args": dict(candidate.args),
            "effect": candidate.effect.value,
            "complete": candidate.complete,
        }
        for candidate in offered
    ]
    # The fingerprint covers only content that can yield progress: the observation, the
    # candidate table, the controls and the exclusions. History is shown to the model but
    # excluded here, so a returning state is still recognized as a returning state.
    stability = {
        "observation_revisions": {"default": state.revision},
        "goal": state.task.goal,
        "inputs": dict(state.task.inputs),
        "candidates": candidates_json,
        "controls": [control.value for control in controls],
        "excluded_actions": sorted(state.dead_actions),
    }
    fingerprint = _fingerprint(stability)

    snapshot = {**stability, "state_version": state.state_version, "decision_state": projection}
    size = len(json.dumps(snapshot, ensure_ascii=False))
    if size > config.frame_char_budget:
        raise FrameBudgetError(f"frame is {size} chars, over budget {config.frame_char_budget}")

    return Frame(
        frame_id=f"f{state.state_version}:{fingerprint[:8]}",
        run_id=state.run_id,
        state_version=state.state_version,
        fingerprint=fingerprint,
        observation_revisions={"default": state.revision},
        decision_state=projection,
        candidates=offered,
        controls=tuple(controls),
    )
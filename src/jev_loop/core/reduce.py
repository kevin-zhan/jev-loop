"""The pure reducer: ``old_state + recorded event -> new_state``.

Rules enforced here (and only here):

* ``dead_actions`` is scoped to the current observation. A change of observation revision
  clears it, because an action that had no effect in the old world may be meaningful in the
  new one. Without a rule like this, excluding a candidate once would blind the loop forever.
* ``EXECUTION_RECEIPT`` with a pending/unknown status creates an unresolved operation; it is
  removed only by ``OPERATION_RESOLVED``.
* ``TASK_UPDATED`` is the only way task text changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .events import Event, EventKind
from .types import (
    Attempt,
    Control,
    Idempotency,
    Observation,
    ReceiptStatus,
    RunStatus,
    RuntimeState,
    StepRecord,
    StopReason,
    TaskSpec,
    UnresolvedOperation,
    VerificationRecord,
    VerifyVerdict,
)

Json = Any


def initial_state(run_id: str, task: TaskSpec, *, started_at: float = 0.0) -> RuntimeState:
    return RuntimeState(run_id=run_id, task=task, started_at=started_at)


def apply(state: RuntimeState, event: Event) -> RuntimeState:
    """Pure transition. Must stay side-effect free: no model, no I/O, no clock reads."""
    data = event.data
    step = max(state.step, event.step)

    if event.kind is EventKind.RUN_STARTED:
        # started_at was captured when the state was created; a zero start time is legitimate.
        return replace(state, step=step, status=RunStatus.RUNNING, elapsed_ms=0.0)

    if event.kind is EventKind.TASK_UPDATED:
        task = TaskSpec(
            goal=str(data.get("goal", state.task.goal)),
            inputs={**state.task.inputs, **dict(data.get("inputs") or {})},
            constraints=tuple(data.get("constraints") or state.task.constraints),
            success_criteria=tuple(data.get("success_criteria") or state.task.success_criteria),
            authorization=tuple(data.get("authorization") or state.task.authorization),
        )
        return replace(state, step=step, task=task, state_version=state.state_version + 1)

    if event.kind is EventKind.OBSERVED:
        if data.get("ok") is False:
            observation = state.observation
            return replace(
                state,
                step=step,
                observation=observation,
                observation_stale=True,
                state_version=state.state_version + 1,
            )
        observation = Observation(
            revision=str(data["revision"]),
            payload=dict(data.get("payload") or {}),
            observed_at=event.at,
            stale=False,
        )
        changed = state.observation is None or state.observation.revision != observation.revision
        return replace(
            state,
            step=step,
            observation=observation,
            observation_stale=False,
            observations=state.observations + 1,
            dead_actions=frozenset() if changed else state.dead_actions,
            state_version=state.state_version + 1,
        )

    if event.kind is EventKind.FRAME_BUILT:
        fingerprint = str(data["fingerprint"])
        visits = dict(state.frame_visits)
        visits[fingerprint] = visits.get(fingerprint, 0) + 1
        return replace(state, step=step, frame_visits=visits, state_version=state.state_version + 1)

    if event.kind is EventKind.GUARD:
        dead = set(state.dead_actions) | {str(key) for key in data.get("dead_keys") or ()}
        escalations = state.guard_escalations + (1 if data.get("escalate") else 0)
        return replace(state, step=step, dead_actions=frozenset(dead), guard_escalations=escalations)

    if event.kind in (EventKind.DECISION, EventKind.DECISION_REJECTED):
        key = "decisions" if event.kind is EventKind.DECISION else "decisions_rejected"
        return replace(state, step=step, counters={**state.counters, key: state.counters.get(key, 0) + 1})

    if event.kind is EventKind.EXECUTION_INTENT:
        return replace(state, step=step, counters={**state.counters, "intents": state.counters.get("intents", 0) + 1})

    if event.kind is EventKind.EXECUTION_RECEIPT:
        status = ReceiptStatus(str(data["status"]))
        attempt = Attempt(
            observation=state.revision,
            key=str(data["key"]),
            status=status,
            step=event.step,
        )
        unresolved = state.unresolved
        if status.unresolved:
            operation = UnresolvedOperation(
                operation_id=str(data["operation_id"]),
                key=attempt.key,
                status=status,
                step=event.step,
                args=dict(data.get("args") or {}),
                idempotency=Idempotency(str(data.get("idempotency", "none"))),
            )
            if all(op.operation_id != operation.operation_id for op in unresolved):
                unresolved = (*unresolved, operation)
        return replace(
            state,
            step=step,
            attempts=(*state.attempts, attempt),
            unresolved=unresolved,
            counters={**state.counters, "executions": state.counters.get("executions", 0) + 1},
            state_version=state.state_version + 1,
        )

    if event.kind is EventKind.OPERATION_RESOLVED:
        operation_id = str(data["operation_id"])
        remaining = tuple(op for op in state.unresolved if op.operation_id != operation_id)
        status = ReceiptStatus(str(data["status"]))
        attempt = Attempt(observation=state.revision, key=str(data.get("key", "")), status=status, step=event.step)
        return replace(state, step=step, unresolved=remaining, attempts=(*state.attempts, attempt))

    if event.kind is EventKind.VERIFICATION:
        record = VerificationRecord(
            verdict=VerifyVerdict(str(data["verdict"])),
            detail=str(data.get("detail", "")),
            evidence=dict(data.get("evidence") or {}),
            step=event.step,
        )
        return replace(state, step=step, verifications=(*state.verifications, record))

    if event.kind is EventKind.SUSPENDED:
        return replace(
            state,
            step=step,
            status=RunStatus.SUSPENDED,
            stop_reason=StopReason(str(data["reason"])),
        )

    if event.kind is EventKind.RESUMED:
        inputs = state.resume_inputs
        if data.get("input") is not None:
            inputs = (*inputs, data["input"])
        return replace(state, step=step, status=RunStatus.RUNNING, stop_reason=None, resume_inputs=inputs)

    if event.kind is EventKind.RUN_FINISHED:
        return replace(
            state,
            step=step,
            status=RunStatus(str(data["status"])),
            stop_reason=StopReason(str(data["stop_reason"])) if data.get("stop_reason") else None,
            elapsed_ms=float(data.get("elapsed_ms", state.elapsed_ms)),
        )

    if event.kind is EventKind.STEP:
        record = StepRecord(
            step=event.step,
            frame_id=str(data.get("frame_id", "")),
            observation=str(data.get("observation", state.revision)),
            candidate_key=data.get("candidate_key"),
            control=Control(str(data["control"])) if data.get("control") else None,
            receipt=ReceiptStatus(str(data["receipt"])) if data.get("receipt") else None,
            changed=bool(data.get("changed", False)),
            note=str(data.get("note", "")),
        )
        return replace(
            state, step=step, history=(*state.history, record), elapsed_ms=float(data.get("elapsed_ms", 0.0))
        )

    return replace(state, step=step)


def replay(events: Mapping[str, Json] | list[Event] | tuple[Event, ...], state: RuntimeState) -> RuntimeState:
    """Fold events into state. Used by tests and by session recovery."""
    for event in events:
        state = apply(state, event if isinstance(event, Event) else Event.from_json(event))
    return state
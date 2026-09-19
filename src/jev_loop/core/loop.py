"""The loop kernel.

``step()`` performs at most one externally visible action and returns a structured record of
what happened. ``run()`` is only a scheduler over ``step()``; it adds no policy of its own.

Ordering inside a step (deliberate, each stage is a separate event):

1. lifecycle / budget check
2. resolve pending or unknown operations — query, never blind retry
3. observe (a failed read suspends; the old observation is kept and marked stale)
4. offer candidates (never a model call)
5. build the frame and register it against the cycle guard
6. ask the policy for a choice
7. validate the choice against *this* frame, the dead-key set, the environment, and authorization
8. record the execution intent (the idempotency key) before touching the world
9. execute, record the receipt
10. re-observe, reduce, and apply the progress guards
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..ports import (
    Clock,
    Environment,
    EnvironmentError,
    EventStore,
    ExecutionRejected,
    ObservationError,
    Policy,
    Verifier,
    default_validate,
)
from ..stores.jsonl import MemoryEventStore
from .events import Event, EventKind
from .frame import FrameBudgetError, build_frame
from .guards import assess_attempts, assess_effect, assess_frame, combine
from .reduce import apply, initial_state
from .types import (
    ActionCandidate,
    AuthorizeFn,
    Control,
    ExecutionIntent,
    ExecutionReceipt,
    GuardKind,
    GuardOutcome,
    LoopConfig,
    ReceiptStatus,
    RunStatus,
    RuntimeState,
    StepRecord,
    StopReason,
    VerifyVerdict,
)

Json = Any


@dataclass(frozen=True)
class StepResult:
    step: int
    status: RunStatus
    stop_reason: StopReason | None = None
    control: Control | None = None
    candidate_key: str | None = None
    receipt: ReceiptStatus | None = None
    guard: GuardKind = GuardKind.NONE
    detail: str = ""
    events: tuple[Event, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    stop_reason: StopReason | None
    steps: int
    observations: int
    executions: int
    decisions: int
    history: tuple[StepRecord, ...]
    verifications: tuple[Any, ...]
    resume_inputs: tuple[Json, ...]
    elapsed_ms: float
    state: RuntimeState


class Loop:
    def __init__(
        self,
        *,
        task: Any,
        environment: Environment,
        policy: Policy,
        verifier: Verifier | None = None,
        store: EventStore | None = None,
        config: LoopConfig | None = None,
        clock: Clock | None = None,
        authorize: AuthorizeFn | None = None,
        run_id: str | None = None,
    ) -> None:
        self.config = config or LoopConfig()
        self.environment = environment
        self.policy = policy
        self.verifier = verifier
        self.store = store or MemoryEventStore()
        self.clock: Clock = clock or time.monotonic
        self.authorize = authorize
        self._cancelled = False
        self._last_frame = None
        self._seq = 0
        self._state = initial_state(run_id or uuid.uuid4().hex[:12], task, started_at=self.clock())
        self._emit(EventKind.RUN_STARTED, task=_task_json(task))

    # -- public surface ----------------------------------------------------------------

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def last_frame(self):
        return self._last_frame

    def timeline(self) -> tuple[Event, ...]:
        return self.store.events()

    def cancel(self) -> None:
        """Stop starting new actions. Effects already produced keep their receipts."""
        self._cancelled = True
        if not self._state.status.terminal:
            self._finish(RunStatus.CANCELLED, StopReason.CANCELLED, detail="cancelled by host")

    def resume(self, input: Json = None, *, task_update: Mapping[str, Json] | None = None) -> RuntimeState:
        """Wake a suspended run. ``task_update`` is the only way goal text may change."""
        if self._state.status.terminal:
            return self._state
        self._emit(EventKind.RESUMED, input=input)
        if task_update:
            self._emit(EventKind.TASK_UPDATED, **dict(task_update))
        return self._state

    def run(self) -> RunResult:
        while self._state.status is RunStatus.RUNNING:
            self.step()
        return self.result()

    def result(self) -> RunResult:
        return RunResult(
            status=self._state.status,
            stop_reason=self._state.stop_reason,
            steps=self._state.step,
            observations=self._state.observations,
            executions=self._state.counters.get("executions", 0),
            decisions=self._state.counters.get("decisions", 0),
            history=self._state.history,
            verifications=self._state.verifications,
            resume_inputs=self._state.resume_inputs,
            elapsed_ms=self._state.elapsed_ms,
            state=self._state,
        )

    # -- one step ----------------------------------------------------------------------

    def step(self) -> StepResult:
        before = len(self.store.events())
        self._seq = before
        state = self._state

        if state.status.terminal:
            return self._step_result(detail="run already finished", events=self._new_events(before))

        step_no = state.step + 1
        if self.config.max_steps and step_no > self.config.max_steps:
            self._finish(RunStatus.FAILED, StopReason.MAX_STEPS, detail=f"step limit {self.config.max_steps} reached")
            return self._step_result(events=self._new_events(before))
        if self._over_budget():
            self._finish(RunStatus.FAILED, StopReason.BUDGET_EXHAUSTED, detail="wall-clock budget exhausted")
            return self._step_result(events=self._new_events(before))

        # 2. pending/unknown operations first: query, never blind retry.
        if state.unresolved:
            self._resolve_operations()
            if self._state.has_unresolved():
                self._suspend(StopReason.AWAITING_RESULT, detail="unresolved operation must be resolved first")
                return self._step_result(events=self._new_events(before))

        # 3. observe
        try:
            observation = self.environment.observe()
        except ObservationError as error:
            self._emit(EventKind.OBSERVED, step=step_no, at=self.clock(), ok=False, error=str(error))
            self._suspend(StopReason.AWAITING_EVIDENCE, detail=f"observation failed: {error}")
            return self._step_result(events=self._new_events(before))
        changed = self._state.observation is None or self._state.observation.revision != observation.revision
        self._emit(
            EventKind.OBSERVED,
            step=step_no,
            at=self.clock(),
            ok=True,
            revision=observation.revision,
            payload=dict(observation.payload),
            changed=changed,
        )

        # 4. offer
        candidates = tuple(self.environment.offer(self._state, observation))
        self._emit(
            EventKind.OFFERED, step=step_no, at=self.clock(), count=len(candidates), keys=[c.key for c in candidates]
        )

        # 5. frame
        try:
            frame = build_frame(self._state, candidates, controls=self.config.controls, config=self.config)
        except FrameBudgetError as error:
            self._finish(RunStatus.FAILED, StopReason.BUDGET_EXHAUSTED, detail=str(error))
            return self._step_result(events=self._new_events(before))
        self._last_frame = frame
        self._emit(
            EventKind.FRAME_BUILT,
            step=step_no,
            at=self.clock(),
            frame_id=frame.frame_id,
            fingerprint=frame.fingerprint,
            candidates=len(frame.candidates),
            options=len(frame.candidates) + len(frame.controls),
        )

        frame_outcome = assess_frame(self._state, frame.fingerprint, self.config)
        if frame_outcome.kind is not GuardKind.NONE:
            self._emit_guard(step_no, frame_outcome)
            if frame_outcome.escalate and not frame_outcome.dead_keys:
                # Nothing in this frame can be narrowed away: the run needs new information
                # rather than another identical decision. Stop spending calls.
                self._suspend(StopReason.AWAITING_EVIDENCE, detail=frame_outcome.detail)
                return self._step_result(
                    events=self._new_events(before), guard=frame_outcome.kind, detail=frame_outcome.detail
                )

        # 6. decide
        try:
            decision = self.policy.decide(frame)
        except Exception as error:  # a broken policy is a hard stop, not a retry loop
            self._emit(EventKind.ERROR, step=step_no, at=self.clock(), stage="policy", error=repr(error))
            self._finish(RunStatus.FAILED, StopReason.POLICY_ERROR, detail=f"policy failed: {error!r}")
            return self._step_result(events=self._new_events(before))
        self._emit(
            EventKind.DECISION,
            step=step_no,
            at=self.clock(),
            frame_id=decision.frame_id,
            candidate_id=decision.candidate_id,
            control=decision.control.value if decision.control else None,
            confidence=decision.confidence,
            probabilities=dict(decision.probabilities),
            model=decision.model,
            usage=dict(decision.usage),
            latency_ms=decision.latency_ms,
        )

        # 7. validate the choice against this frame only
        if decision.frame_id != frame.frame_id:
            self._reject(step_no, decision.candidate_id, f"decision belongs to frame {decision.frame_id!r}")
            return self._step_result(events=self._new_events(before), detail="stale decision rejected")
        if (decision.candidate_id is None) == (decision.control is None):
            self._reject(step_no, decision.candidate_id, "exactly one of candidate_id / control must be set")
            return self._step_result(events=self._new_events(before), detail="malformed decision rejected")

        if decision.control is not None:
            return self._handle_control(step_no, decision.control, frame, before)

        candidate = frame.candidate(decision.candidate_id or "")
        if candidate is None:
            self._reject(step_no, decision.candidate_id, "candidate id is not in this frame")
            return self._step_result(events=self._new_events(before), detail="out-of-frame candidate rejected")

        rejection = self._validate_candidate(candidate)
        if rejection is not None:
            self._reject(step_no, candidate.id, rejection)
            return self._step_result(
                events=self._new_events(before),
                guard=GuardKind.REPEAT if "already attempted" in rejection else GuardKind.NONE,
            )

        repeat = assess_attempts(
            self._state, observation=frame.observation_revisions["default"], key=candidate.key, config=self.config
        )
        if repeat.kind is not GuardKind.NONE:
            self._emit_guard(step_no, repeat)
            self._guard_failure(repeat.kind, repeat.detail)
            return self._step_result(events=self._new_events(before), guard=repeat.kind, detail=repeat.detail)

        # 8. intent before execution
        operation_id = self._operation_id(step_no, candidate)
        intent = ExecutionIntent(
            operation_id=operation_id,
            run_id=self._state.run_id,
            step=step_no,
            key=candidate.key,
            args=dict(candidate.args),
            effect=candidate.effect,
            idempotency=candidate.idempotency,
        )
        self._emit(
            EventKind.EXECUTION_INTENT,
            step=step_no,
            at=self.clock(),
            operation_id=operation_id,
            key=candidate.key,
            args=dict(candidate.args),
            effect=candidate.effect.value,
            idempotency=candidate.idempotency.value,
        )

        # 9. execute
        try:
            receipt = self.environment.execute(candidate, intent)
        except ExecutionRejected as error:
            receipt = ExecutionReceipt(operation_id=operation_id, status=ReceiptStatus.REJECTED, detail=str(error))
        except Exception as error:
            # The executor does not know what happened: record it as unknown, never crash the
            # host and never retry it. An unresolved operation is the honest outcome.
            receipt = ExecutionReceipt(
                operation_id=operation_id, status=ReceiptStatus.UNKNOWN, detail=f"executor error: {error!r}"
            )
            self._emit(EventKind.ERROR, step=step_no, at=self.clock(), stage="execute", error=repr(error))
        self._emit(
            EventKind.EXECUTION_RECEIPT,
            step=step_no,
            at=self.clock(),
            operation_id=receipt.operation_id,
            key=candidate.key,
            args=dict(candidate.args),
            idempotency=candidate.idempotency.value,
            status=receipt.status.value,
            detail=receipt.detail,
            evidence=dict(receipt.evidence),
        )

        # 10. post-observation and progress accounting
        changed_after = False
        if self.config.post_observe and candidate.effect.mutates and not self._state.has_unresolved():
            try:
                after = self.environment.observe()
                changed_after = after.revision != observation.revision
                self._emit(
                    EventKind.OBSERVED,
                    step=step_no,
                    at=self.clock(),
                    ok=True,
                    revision=after.revision,
                    payload=dict(after.payload),
                    changed=changed_after,
                )
            except ObservationError as error:
                self._emit(EventKind.OBSERVED, step=step_no, at=self.clock(), ok=False, error=str(error))

        effect_outcome = (
            assess_effect(
                self._state,
                key=candidate.key,
                status=receipt.status,
                changed=changed_after,
                effect_mutates=candidate.effect.mutates,
            )
            if (self.config.post_observe and candidate.effect.mutates and not self._state.has_unresolved())
            else GuardOutcome()
        )
        combined = combine((frame_outcome, effect_outcome))
        if effect_outcome.kind is not GuardKind.NONE:
            self._emit_guard(step_no, effect_outcome)

        self._emit(
            EventKind.STEP,
            step=step_no,
            at=self.clock(),
            frame_id=frame.frame_id,
            observation=observation.revision,
            candidate_key=candidate.key,
            control=None,
            receipt=receipt.status.value,
            changed=changed_after,
            note=combined.detail,
            elapsed_ms=self._elapsed_ms(),
        )

        if combined.kind is not GuardKind.NONE:
            self._guard_failure(combined.kind, combined.detail)
        return self._step_result(
            events=self._new_events(before),
            candidate_key=candidate.key,
            receipt=receipt.status,
            guard=combined.kind,
            detail=combined.detail,
        )

    # -- stages ------------------------------------------------------------------------

    def _resolve_operations(self) -> None:
        for operation in self._state.unresolved:
            try:
                receipt = self.environment.query(operation)
            except EnvironmentError as error:
                self._emit(EventKind.ERROR, step=self._state.step, at=self.clock(), stage="query", error=repr(error))
                continue
            if receipt is None or receipt.status.unresolved:
                continue
            self._emit(
                EventKind.OPERATION_RESOLVED,
                step=self._state.step,
                at=self.clock(),
                operation_id=operation.operation_id,
                key=operation.key,
                status=receipt.status.value,
                detail=receipt.detail,
                evidence=dict(receipt.evidence),
            )

    def _handle_control(self, step_no: int, control: Control, frame, before: int) -> StepResult:
        if control not in frame.controls:
            self._reject(step_no, None, f"control {control.value!r} was not offered in frame {frame.frame_id!r}")
            return self._step_result(events=self._new_events(before), detail="unoffered control rejected")

        if control is Control.REQUEST_FINISH:
            self._emit(
                EventKind.STEP,
                step=step_no,
                at=self.clock(),
                frame_id=frame.frame_id,
                observation=self._state.revision,
                control=control.value,
                note="verification requested",
                elapsed_ms=self._elapsed_ms(),
            )
            self._verify()
            return self._step_result(events=self._new_events(before), control=control)

        if control is Control.BLOCKED:
            incomplete = [c.key for c in frame.candidates if not c.complete]
            self._emit(
                EventKind.STEP,
                step=step_no,
                at=self.clock(),
                frame_id=frame.frame_id,
                observation=self._state.revision,
                control=control.value,
                note="policy reported no usable candidate",
                elapsed_ms=self._elapsed_ms(),
            )
            self._suspend(
                StopReason.BLOCKED,
                detail="policy blocked"
                + (f"; incomplete candidates: {incomplete}" if incomplete else "; candidate set claimed complete"),
            )
            return self._step_result(events=self._new_events(before), control=control)

        if control is Control.WAIT:
            self._emit(
                EventKind.STEP,
                step=step_no,
                at=self.clock(),
                frame_id=frame.frame_id,
                observation=self._state.revision,
                control=control.value,
                note="waiting for content",
                elapsed_ms=self._elapsed_ms(),
            )
            # WAIT is a suspension with a wake condition, not a busy loop: the host resumes
            # when the environment has something new, and a resume that changes nothing is
            # caught by the frame-revisit guard.
            self._suspend(StopReason.AWAITING_RESULT, detail="waiting for content to load")
            return self._step_result(events=self._new_events(before), control=control)

        # REFRESH: nothing external happens; the next step observes again.
        self._emit(
            EventKind.STEP,
            step=step_no,
            at=self.clock(),
            frame_id=frame.frame_id,
            observation=self._state.revision,
            control=control.value,
            note="refresh requested",
            elapsed_ms=self._elapsed_ms(),
        )
        return self._step_result(events=self._new_events(before), control=control)

    def _validate_candidate(self, candidate: ActionCandidate) -> str | None:
        if candidate.key in self._state.dead_actions:
            return f"{candidate.key!r} had no observable effect on this observation"
        if candidate.key in self._state.unresolved_keys():
            return f"{candidate.key!r} has an unresolved operation"
        if candidate.capability not in self._state.task.authorization and self._state.task.authorization:
            return f"capability {candidate.capability!r} is not authorized"
        reason = default_validate(candidate, self._state)
        if reason is not None:
            return reason
        validator = getattr(self.environment, "validate", None)
        if validator is not None:
            reason = validator(candidate, self._state)
            if reason is not None:
                return reason
        if self.authorize is not None:
            return self.authorize(candidate, self._state)
        return None

    def _verify(self) -> None:
        if self.verifier is None:
            self._emit(
                EventKind.VERIFICATION, step=self._state.step, at=self.clock(),
                verdict=VerifyVerdict.UNKNOWN.value, detail="no verifier",
            )
            self._suspend(StopReason.AWAITING_EVIDENCE, detail="no verifier configured")
            return
        try:
            result = self.verifier.verify(self._state)
        except Exception as error:
            self._emit(
                EventKind.VERIFICATION,
                step=self._state.step,
                at=self.clock(),
                verdict=VerifyVerdict.UNKNOWN.value,
                detail=f"verifier error: {error!r}",
            )
            self._suspend(StopReason.AWAITING_EVIDENCE, detail="verifier failed")
            return
        self._emit(
            EventKind.VERIFICATION,
            step=self._state.step,
            at=self.clock(),
            verdict=result.verdict.value,
            detail=result.detail,
            evidence=dict(result.evidence),
        )
        if result.verdict is VerifyVerdict.SATISFIED:
            self._finish(RunStatus.SUCCEEDED, None, detail=result.detail)
            return
        if result.verdict is VerifyVerdict.UNSATISFIED:
            if len(self._state.verifications) >= self.config.max_verifications:
                self._finish(RunStatus.FAILED, StopReason.VERIFICATION_EXHAUSTED, detail=result.detail)
            return
        self._suspend(StopReason.AWAITING_EVIDENCE, detail=result.detail or "evidence not sufficient")

    # -- helpers -----------------------------------------------------------------------

    def _operation_id(self, step_no: int, candidate: ActionCandidate) -> str:
        digest = hashlib.sha256(f"{candidate.key}|{sorted(map(str, candidate.args.items()))}".encode()).hexdigest()[:10]
        return f"{self._state.run_id}.{step_no}.{digest}"

    def _elapsed_ms(self) -> float:
        return (self.clock() - self._state.started_at) * 1000.0

    def _over_budget(self) -> bool:
        limit = self.config.max_seconds
        return bool(limit) and (self.clock() - self._state.started_at) > limit

    def _reject(self, step_no: int, candidate_id: str | None, reason: str) -> None:
        self._emit(EventKind.DECISION_REJECTED, step=step_no, at=self.clock(), candidate_id=candidate_id, reason=reason)

    def _emit_guard(self, step_no: int, outcome: GuardOutcome) -> None:
        self._emit(
            EventKind.GUARD,
            step=step_no,
            at=self.clock(),
            kind=outcome.kind.value,
            detail=outcome.detail,
            dead_keys=sorted(outcome.dead_keys),
            escalate=outcome.escalate,
            fatal=outcome.fatal,
        )

    def _suspend(self, reason: StopReason, *, detail: str = "") -> None:
        self._emit(EventKind.SUSPENDED, step=self._state.step, at=self.clock(), reason=reason.value, detail=detail)

    def _guard_failure(self, kind: GuardKind, detail: str) -> bool:
        """Turn repeated guard escalations into a bounded, named failure."""
        if self._state.guard_escalations < self.config.max_guard_escalations:
            return False
        reason = StopReason.NO_PROGRESS if kind is GuardKind.NO_PROGRESS else StopReason.CYCLE
        self._finish(RunStatus.FAILED, reason, detail=detail)
        return True

    def _finish(self, status: RunStatus, reason: StopReason | None, *, detail: str = "") -> None:
        self._emit(
            EventKind.RUN_FINISHED,
            step=self._state.step,
            at=self.clock(),
            status=status.value,
            stop_reason=reason.value if reason else None,
            detail=detail,
            elapsed_ms=self._elapsed_ms(),
        )

    def _emit(self, event_kind: EventKind, **data: Json) -> Event:
        self._seq += 1
        event = Event(
            seq=self._seq,
            step=int(data.get("step", self._state.step)),
            at=float(data.get("at", self.clock())),
            kind=event_kind,
            data={key: value for key, value in data.items() if key not in {"step", "at"}},
        )
        self.store.append(event)
        self._state = apply(self._state, event)
        return event

    def _new_events(self, before: int) -> tuple[Event, ...]:
        return tuple(self.store.events()[before:])

    def _step_result(
        self,
        *,
        events: tuple[Event, ...],
        control: Control | None = None,
        candidate_key: str | None = None,
        receipt: ReceiptStatus | None = None,
        guard: GuardKind = GuardKind.NONE,
        detail: str = "",
    ) -> StepResult:
        return StepResult(
            step=self._state.step,
            status=self._state.status,
            stop_reason=self._state.stop_reason,
            control=control,
            candidate_key=candidate_key,
            receipt=receipt,
            guard=guard,
            detail=detail,
            events=events,
        )


def _task_json(task: Any) -> dict[str, Json]:
    return {
        "goal": task.goal,
        "inputs": dict(task.inputs),
        "constraints": list(task.constraints),
        "success_criteria": list(task.success_criteria),
        "authorization": list(task.authorization),
    }
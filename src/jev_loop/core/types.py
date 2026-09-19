"""Core value types for the Jev Loop runtime.

Nothing in this module performs I/O or calls a model. Every type is immutable so the
reducer in :mod:`jev_loop.core.reduce` can be a pure function of (state, event).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Json = Any


# --------------------------------------------------------------------------------------
# Lifecycle vocabulary
# --------------------------------------------------------------------------------------


class RunStatus(StrEnum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


class StopReason(StrEnum):
    MAX_STEPS = "max_steps"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CYCLE = "cycle_detected"
    NO_PROGRESS = "no_progress"
    BLOCKED = "blocked"
    AWAITING_RESULT = "awaiting_result"
    AWAITING_EVIDENCE = "awaiting_evidence"
    VERIFICATION_EXHAUSTED = "verification_exhausted"
    CANCELLED = "cancelled"
    ENVIRONMENT_ERROR = "environment_error"
    POLICY_ERROR = "policy_error"


class Effect(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"

    @property
    def mutates(self) -> bool:
        return self is not Effect.READ_ONLY


class Idempotency(StrEnum):
    """How safe it is to repeat a logical operation.

    ``SAFE``      repeating it cannot add effects (idempotent by construction)
    ``QUERYABLE`` the executor can report whether the operation already happened
    ``NONE``      repetition can double an effect; the runtime must never retry blindly
    """

    SAFE = "safe"
    QUERYABLE = "queryable"
    NONE = "none"


class ReceiptStatus(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    PENDING = "pending"
    UNKNOWN = "unknown"

    @property
    def unresolved(self) -> bool:
        return self in {ReceiptStatus.PENDING, ReceiptStatus.UNKNOWN}


class VerifyVerdict(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNKNOWN = "unknown"


class Control(StrEnum):
    """Runtime-owned actions. They are offered to the policy but never executed as tools."""

    REFRESH = "refresh"
    REQUEST_FINISH = "request_finish"
    BLOCKED = "blocked"
    WAIT = "wait"


class GuardKind(StrEnum):
    NONE = "none"
    NO_PROGRESS = "no_progress"
    REPEAT = "repeat"
    CYCLE = "cycle"


# --------------------------------------------------------------------------------------
# Task, observation, candidates
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSpec:
    """User-owned. Only an explicit ``USER_TASK_UPDATE`` event may change it."""

    goal: str
    inputs: Mapping[str, Json] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    authorization: tuple[str, ...] = ()


@dataclass(frozen=True)
class Observation:
    revision: str
    payload: Mapping[str, Json] = field(default_factory=dict)
    observed_at: float = 0.0
    stale: bool = False


@dataclass(frozen=True)
class ActionCandidate:
    """A concrete operation in the current state, not a tool name.

    ``key`` is the semantic identity used for de-duplication and dead-action tracking;
    ``id`` is frame-scoped and is the only thing the policy is allowed to return.
    """

    key: str
    capability: str
    description: str
    args: Mapping[str, Json] = field(default_factory=dict)
    effect: Effect = Effect.LOCAL_WRITE
    idempotency: Idempotency = Idempotency.SAFE
    observation: str | None = None
    complete: bool = True
    id: str = ""


@dataclass(frozen=True)
class Decision:
    frame_id: str
    candidate_id: str | None = None
    control: Control | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    confidence: float | None = None
    model: str | None = None
    usage: Mapping[str, Json] = field(default_factory=dict)
    latency_ms: float | None = None


@dataclass(frozen=True)
class ExecutionIntent:
    operation_id: str
    run_id: str
    step: int
    key: str
    args: Mapping[str, Json] = field(default_factory=dict)
    effect: Effect = Effect.LOCAL_WRITE
    idempotency: Idempotency = Idempotency.NONE


@dataclass(frozen=True)
class ExecutionReceipt:
    operation_id: str
    status: ReceiptStatus
    detail: str = ""
    evidence: Mapping[str, Json] = field(default_factory=dict)


@dataclass(frozen=True)
class UnresolvedOperation:
    operation_id: str
    key: str
    status: ReceiptStatus
    step: int
    args: Mapping[str, Json] = field(default_factory=dict)
    idempotency: Idempotency = Idempotency.NONE


@dataclass(frozen=True)
class Attempt:
    observation: str
    key: str
    status: ReceiptStatus
    step: int


@dataclass(frozen=True)
class VerificationRecord:
    verdict: VerifyVerdict
    detail: str = ""
    evidence: Mapping[str, Json] = field(default_factory=dict)
    step: int = 0


@dataclass(frozen=True)
class VerifierResult:
    verdict: VerifyVerdict
    detail: str = ""
    evidence: Mapping[str, Json] = field(default_factory=dict)


@dataclass(frozen=True)
class StepRecord:
    step: int
    frame_id: str
    observation: str
    candidate_key: str | None = None
    control: Control | None = None
    receipt: ReceiptStatus | None = None
    changed: bool = False
    note: str = ""


# --------------------------------------------------------------------------------------
# Runtime state and configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeState:
    run_id: str
    task: TaskSpec
    status: RunStatus = RunStatus.RUNNING
    stop_reason: StopReason | None = None
    step: int = 0
    state_version: int = 0
    observation: Observation | None = None
    observation_stale: bool = False
    observations: int = 0
    unresolved: tuple[UnresolvedOperation, ...] = ()
    dead_actions: frozenset[str] = frozenset()
    attempts: tuple[Attempt, ...] = ()
    history: tuple[StepRecord, ...] = ()
    verifications: tuple[VerificationRecord, ...] = ()
    resume_inputs: tuple[Json, ...] = ()
    frame_visits: Mapping[str, int] = field(default_factory=dict)
    guard_escalations: int = 0
    counters: Mapping[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    elapsed_ms: float = 0.0

    @property
    def revision(self) -> str:
        return self.observation.revision if self.observation else "unknown"

    def has_unresolved(self) -> bool:
        return bool(self.unresolved)

    def unresolved_keys(self) -> frozenset[str]:
        return frozenset(op.key for op in self.unresolved)


@dataclass(frozen=True)
class LoopConfig:
    max_steps: int = 40
    max_seconds: float | None = 300.0
    max_verifications: int = 3
    max_frame_visits: int = 2
    max_guard_escalations: int = 3
    post_observe: bool = True
    controls: tuple[Control, ...] = (Control.REFRESH, Control.REQUEST_FINISH, Control.BLOCKED, Control.WAIT)
    max_options: int = 255
    frame_char_budget: int = 120_000
    history_limit: int = 24


@dataclass(frozen=True)
class Frame:
    """Immutable decision snapshot: state revisions, candidates, and their ids bound together."""

    frame_id: str
    run_id: str
    state_version: int
    fingerprint: str
    observation_revisions: Mapping[str, str]
    decision_state: Mapping[str, Json]
    candidates: tuple[ActionCandidate, ...]
    controls: tuple[Control, ...]

    def candidate(self, candidate_id: str) -> ActionCandidate | None:
        for candidate in self.candidates:
            if candidate.id == candidate_id:
                return candidate
        return None

    def option_ids(self) -> tuple[str, ...]:
        return tuple(candidate.id for candidate in self.candidates)


@dataclass(frozen=True)
class GuardOutcome:
    kind: GuardKind = GuardKind.NONE
    detail: str = ""
    dead_keys: frozenset[str] = frozenset()
    escalate: bool = False
    fatal: bool = False


PolicyFn = Callable[[Frame], Decision]
AuthorizeFn = Callable[[ActionCandidate, RuntimeState], str | None]
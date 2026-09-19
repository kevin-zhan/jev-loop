"""jev_loop: an explicit-state decision runtime built on typed choices.

The kernel owns the loop, the state, the guards and the termination; a policy (Jev or
anything else) only chooses inside one immutable frame.
"""

from .core.events import Event, EventKind
from .core.loop import Loop, RunResult, StepResult
from .core.types import (
    ActionCandidate,
    Control,
    Decision,
    Effect,
    ExecutionIntent,
    ExecutionReceipt,
    Frame,
    GuardKind,
    Idempotency,
    LoopConfig,
    Observation,
    ReceiptStatus,
    RunStatus,
    RuntimeState,
    StopReason,
    TaskSpec,
    VerifierResult,
    VerifyVerdict,
)
from .ports import Environment, ExecutionRejected, ObservationError, Policy, Verifier
from .stores.jsonl import JsonlEventStore, MemoryEventStore

__all__ = [
    "ActionCandidate",
    "Control",
    "Decision",
    "Effect",
    "Environment",
    "Event",
    "ExecutionRejected",
    "EventKind",
    "ExecutionIntent",
    "ExecutionReceipt",
    "Frame",
    "GuardKind",
    "Idempotency",
    "JsonlEventStore",
    "Loop",
    "LoopConfig",
    "MemoryEventStore",
    "Observation",
    "ObservationError",
    "Policy",
    "ReceiptStatus",
    "RunResult",
    "RunStatus",
    "RuntimeState",
    "StepResult",
    "StopReason",
    "TaskSpec",
    "Verifier",
    "VerifierResult",
    "VerifyVerdict",
]
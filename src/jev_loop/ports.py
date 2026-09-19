"""Ports: the three contracts plus the two runtime hooks.

Environment  promises which observations it can produce and which concrete actions it can execute.
Policy       promises a structured choice for the frame it was given (Jev, or anything else).
Verifier     promises an independent verdict on completion, using evidence, not the model's word.

The kernel depends on these protocols only; it has no Jev-specific code and no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from .core.events import Event
from .core.types import (
    ActionCandidate,
    Decision,
    ExecutionIntent,
    ExecutionReceipt,
    Frame,
    Observation,
    RuntimeState,
    UnresolvedOperation,
    VerifierResult,
)

Clock = Callable[[], float]


class ObservationError(RuntimeError):
    """The environment could not be read. Carry the previous revision, marked stale."""


class EnvironmentError(RuntimeError):
    """The environment failed in a way the runtime cannot interpret as a receipt."""


class ExecutionRejected(RuntimeError):
    """The action definitely did not happen (preconditions failed, control refused it).

    Raise this instead of a bare exception when the executor knows nothing was attempted;
    any other exception is recorded as an ``unknown`` receipt.
    """


@runtime_checkable
class Environment(Protocol):
    name: str

    def observe(self) -> Observation:
        """Read the environment. Raise :class:`ObservationError` when it cannot be read."""

    def offer(self, state: RuntimeState, observation: Observation) -> Sequence[ActionCandidate]:
        """Build the concrete actions available in this state. Must not call a model."""

    def execute(self, candidate: ActionCandidate, intent: ExecutionIntent) -> ExecutionReceipt:
        """Execute one validated action. ``intent.operation_id`` is the idempotency key."""

    def query(self, operation: UnresolvedOperation) -> ExecutionReceipt | None:
        """Resolve a pending/unknown operation, or return None when it is still unresolved."""

    def validate(self, candidate: ActionCandidate, state: RuntimeState) -> str | None:
        """Deterministic pre-execution re-check. Return a rejection reason, or None."""


@runtime_checkable
class Policy(Protocol):
    def decide(self, frame: Frame) -> Decision:
        """Choose one candidate id from ``frame`` or one control action."""


@runtime_checkable
class Verifier(Protocol):
    def verify(self, state: RuntimeState) -> VerifierResult:
        """Check the success criteria against real evidence."""


@runtime_checkable
class EventStore(Protocol):
    def append(self, event: Event) -> None: ...

    def events(self) -> tuple[Event, ...]: ...


def default_validate(candidate: ActionCandidate, state: RuntimeState) -> str | None:
    """Reject a candidate whose observation binding no longer holds."""
    if candidate.observation is not None and candidate.observation != state.revision:
        return f"observation moved: candidate bound to {candidate.observation}, now {state.revision}"
    if state.observation_stale:
        return "observation is stale"
    return None
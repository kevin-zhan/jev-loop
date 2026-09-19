"""Deterministic toy environment used to exercise the loop without a browser or a model.

The switchboard has a *visible* state (what ``observe`` returns) and a *world* state (what a
verifier reads). Faults are injected per switch:

``NOOP``    the action completes but nothing changes      -> no-progress guard
``TOGGLE``  setting this switch also flips a partner      -> reversible limit cycle
``PENDING`` the effect happens, the receipt is pending    -> unresolved operation, no resubmit
``UNKNOWN`` the effect may or may not have happened       -> must be queried, never retried
``DELAY``   the effect becomes visible n observations later -> observation lag
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.types import (
    ActionCandidate,
    Control,
    Decision,
    Effect,
    ExecutionIntent,
    ExecutionReceipt,
    Frame,
    Idempotency,
    Observation,
    ReceiptStatus,
    RuntimeState,
    VerifierResult,
    VerifyVerdict,
)
from ..ports import ObservationError, default_validate


class FaultKind(StrEnum):
    NOOP = "noop"
    TOGGLE = "toggle"
    PENDING = "pending"
    UNKNOWN = "unknown"
    DELAY = "delay"
    REJECT = "reject"


@dataclass(frozen=True)
class Fault:
    kind: FaultKind
    partner: str | None = None
    count: int = 1


class SwitchboardEnvironment:
    """One candidate per switch; every action is a local write of a boolean."""

    name = "switchboard"

    def __init__(
        self,
        switches: Mapping[str, bool],
        *,
        faults: Mapping[str, Fault] | None = None,
        fail_observe_after: int | None = None,
    ) -> None:
        self._world = dict(switches)
        self._visible = dict(switches)
        self._faults = dict(faults or {})
        self._pending: dict[str, int] = {}
        self._delayed: dict[str, tuple[bool, int]] = {}
        self._observes = 0
        self._fail_after = fail_observe_after
        self.calls: list[dict[str, object]] = []

    # -- environment port ----------------------------------------------------------------

    def observe(self) -> Observation:
        self._observes += 1
        if self._fail_after is not None and self._observes > self._fail_after:
            raise ObservationError("switchboard is not readable right now")
        self._settle()
        return Observation(
            revision=self._revision(),
            payload={"switches": dict(self._visible), "observes": self._observes},
        )

    def offer(self, state: RuntimeState, observation: Observation) -> Sequence[ActionCandidate]:
        candidates = []
        for name in sorted(self._visible):
            current = self._visible[name]
            target = not current
            fault = self._faults.get(name)
            idempotency = (
                Idempotency.NONE if fault and fault.kind in {FaultKind.UNKNOWN, FaultKind.PENDING} else Idempotency.SAFE
            )
            candidates.append(
                ActionCandidate(
                    key=f"switch:{name}",
                    capability="switch",
                    description=f"Turn {name} {'off' if current else 'on'} (currently {'on' if current else 'off'}).",
                    args={"name": name, "value": target},
                    effect=Effect.EXTERNAL_WRITE if idempotency is Idempotency.NONE else Effect.LOCAL_WRITE,
                    idempotency=idempotency,
                    observation=observation.revision,
                )
            )
        return candidates

    def execute(self, candidate: ActionCandidate, intent: ExecutionIntent) -> ExecutionReceipt:
        name = str(candidate.args["name"])
        value = bool(candidate.args["value"])
        fault = self._faults.get(name)
        record = {"key": candidate.key, "operation_id": intent.operation_id, "value": value, "status": "completed"}
        self.calls.append(record)

        if fault and fault.kind is FaultKind.NOOP:
            record["status"] = "noop"
            return ExecutionReceipt(
                intent.operation_id, ReceiptStatus.COMPLETED, detail="completed with no visible effect"
            )

        if fault and fault.kind is FaultKind.UNKNOWN:
            record["status"] = "unknown"
            return ExecutionReceipt(intent.operation_id, ReceiptStatus.UNKNOWN, detail="provider did not answer")

        if fault and fault.kind is FaultKind.REJECT:
            record["status"] = "rejected"
            return ExecutionReceipt(
                intent.operation_id, ReceiptStatus.REJECTED, detail="the control refused the action"
            )

        if fault and fault.kind is FaultKind.PENDING:
            self._world[name] = value
            self._visible[name] = value
            self._pending[name] = fault.count
            record["status"] = "pending"
            return ExecutionReceipt(
                intent.operation_id, ReceiptStatus.PENDING, detail="submitted; confirmation pending"
            )

        if fault and fault.kind is FaultKind.DELAY:
            self._world[name] = value
            self._delayed[name] = (value, fault.count)
            record["status"] = "completed"
            return ExecutionReceipt(
                intent.operation_id, ReceiptStatus.COMPLETED, detail="applied; may take effect later"
            )

        self._world[name] = value
        self._visible[name] = value
        if fault and fault.kind is FaultKind.TOGGLE and fault.partner:
            self._world[fault.partner] = value
            self._visible[fault.partner] = value
        return ExecutionReceipt(intent.operation_id, ReceiptStatus.COMPLETED, detail="applied")

    def query(self, operation) -> ExecutionReceipt | None:
        name = str(operation.args.get("name", ""))
        if name in self._pending:
            self._pending[name] -= 1
            if self._pending[name] <= 0:
                del self._pending[name]
                return ExecutionReceipt(operation.operation_id, ReceiptStatus.COMPLETED, detail="confirmed by query")
            return None
        fault = self._faults.get(name)
        if fault and fault.kind is FaultKind.UNKNOWN:
            return None  # stays unresolved forever: the runtime must not retry it
        return ExecutionReceipt(operation.operation_id, ReceiptStatus.COMPLETED, detail="no pending effect")

    def validate(self, candidate: ActionCandidate, state: RuntimeState) -> str | None:
        return default_validate(candidate, state)

    # -- inspection ----------------------------------------------------------------------

    @property
    def world(self) -> dict[str, bool]:
        """Ground truth, as an independent verifier would read it."""
        return dict(self._world)

    @property
    def visible(self) -> dict[str, bool]:
        return dict(self._visible)

    def external_change(self, name: str, value: bool) -> None:
        """Simulate something changing the world while the run is suspended."""
        self._world[name] = value
        self._visible[name] = value

    def _settle(self) -> None:
        for name, (value, remaining) in list(self._delayed.items()):
            remaining -= 1
            if remaining <= 0:
                self._visible[name] = value
                del self._delayed[name]
            else:
                self._delayed[name] = (value, remaining)

    def _revision(self) -> str:
        payload = ",".join(f"{name}={int(value)}" for name, value in sorted(self._visible.items()))
        return "rev-" + hashlib.sha256(payload.encode()).hexdigest()[:10]


@dataclass
class SwitchboardVerifier:
    """Reads ground truth, not the observation, so it can disagree with the loop."""

    environment: SwitchboardEnvironment
    required: Mapping[str, bool]

    def verify(self, state: RuntimeState) -> VerifierResult:
        world = self.environment.world
        missing = {name: value for name, value in self.required.items() if world.get(name) != value}
        if missing:
            return VerifierResult(
                VerifyVerdict.UNSATISFIED, detail=f"not satisfied: {missing}", evidence={"world": world}
            )
        return VerifierResult(VerifyVerdict.SATISFIED, detail="all criteria hold", evidence={"world": world})


@dataclass
class GoalPolicy:
    """Deterministic oracle: pick the offered action that moves a required switch to its target.

    Used to test loop mechanics. It is *not* a model and it does not see dead actions; if it
    has nothing useful to do it requests verification, exactly as a real policy would.
    """

    required: Mapping[str, bool]
    confidence: float = 1.0
    frames: list[Frame] = field(default_factory=list)

    def decide(self, frame: Frame) -> Decision:
        self.frames.append(frame)
        for candidate in frame.candidates:
            name = candidate.args.get("name")
            if name in self.required and bool(candidate.args.get("value")) == self.required[name]:
                return Decision(frame_id=frame.frame_id, candidate_id=candidate.id, confidence=self.confidence)
        return Decision(frame_id=frame.frame_id, control=Control.REQUEST_FINISH, confidence=self.confidence)
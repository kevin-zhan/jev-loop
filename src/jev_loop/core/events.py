"""Append-only event vocabulary.

Every state change in the runtime goes through an event. Events are the only input to
:func:`jev_loop.core.reduce.apply`, which keeps state reconstruction and replay honest:
no component may mutate ``RuntimeState`` directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Json = Any


class EventKind(StrEnum):
    RUN_STARTED = "run_started"
    TASK_UPDATED = "task_updated"
    OBSERVED = "observed"
    OFFERED = "offered"
    FRAME_BUILT = "frame_built"
    GUARD = "guard"
    DECISION = "decision"
    DECISION_REJECTED = "decision_rejected"
    EXECUTION_INTENT = "execution_intent"
    EXECUTION_RECEIPT = "execution_receipt"
    OPERATION_UNRESOLVED = "operation_unresolved"
    OPERATION_RESOLVED = "operation_resolved"
    VERIFICATION = "verification"
    SUSPENDED = "suspended"
    RESUMED = "resumed"
    RUN_FINISHED = "run_finished"
    STEP = "step"
    ERROR = "error"


@dataclass(frozen=True)
class Event:
    seq: int
    step: int
    at: float
    kind: EventKind
    data: Mapping[str, Json] = field(default_factory=dict)

    def to_json(self) -> dict[str, Json]:
        return {"seq": self.seq, "step": self.step, "at": self.at, "kind": self.kind.value, "data": dict(self.data)}

    @classmethod
    def from_json(cls, raw: Mapping[str, Json]) -> Event:
        return cls(
            seq=int(raw["seq"]),
            step=int(raw["step"]),
            at=float(raw["at"]),
            kind=EventKind(raw["kind"]),
            data=dict(raw.get("data") or {}),
        )
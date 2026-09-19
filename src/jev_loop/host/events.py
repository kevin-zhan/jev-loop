"""Append-only event vocabulary for managed runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Json = Any


class ManagedEventKind(StrEnum):
    RUN_CREATED = "run_created"
    WORKER_STARTED = "worker_started"
    HEARTBEAT = "heartbeat"
    CONTROLLER_SNAPSHOT = "controller_snapshot"
    TASK_UPDATED = "task_updated"
    COGNITION_REQUESTED = "cognition_requested"
    COGNITION_COMPLETED = "cognition_completed"
    COGNITION_CANCELLED = "cognition_cancelled"
    COGNITION_EXPIRED = "cognition_expired"
    STOP_REQUESTED = "stop_requested"
    RESOURCES_RELEASED = "resources_released"
    RUN_FINISHED = "run_finished"
    ERROR = "error"


@dataclass(frozen=True)
class ManagedEvent:
    seq: int
    at: float
    kind: ManagedEventKind
    data: Mapping[str, Json] = field(default_factory=dict)

    def to_json(self) -> dict[str, Json]:
        return {"seq": self.seq, "at": self.at, "kind": self.kind.value, "data": dict(self.data)}

    @classmethod
    def from_json(cls, raw: Mapping[str, Json]) -> ManagedEvent:
        return cls(
            seq=int(raw["seq"]),
            at=float(raw["at"]),
            kind=ManagedEventKind(str(raw["kind"])),
            data=dict(raw.get("data") or {}),
        )

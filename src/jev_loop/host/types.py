"""Immutable value types for the managed Jev Loop host.

The host is deliberately separate from the decision kernel.  It owns process lifetime,
leases, cognition jobs and the bridge to an agent session; a bundle still owns its domain
adapter and its actual :class:`jev_loop.Loop` (or another controller implementing the same
managed contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Json = Any


class ManagedStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            ManagedStatus.SUCCEEDED,
            ManagedStatus.FAILED,
            ManagedStatus.CANCELLED,
            ManagedStatus.EXPIRED,
        }


class CognitionStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self is not CognitionStatus.PENDING


@dataclass(frozen=True)
class CognitionJob:
    job_id: str
    version: int
    question: str
    context: dict[str, Json] = field(default_factory=dict)
    output_schema: dict[str, Json] = field(default_factory=dict)
    resource_keys: tuple[str, ...] = ()
    requested_at: float = 0.0
    deadline_at: float | None = None
    status: CognitionStatus = CognitionStatus.PENDING
    result: Json = None
    evidence: dict[str, Json] = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> dict[str, Json]:
        return {
            "job_id": self.job_id,
            "version": self.version,
            "question": self.question,
            "context": dict(self.context),
            "output_schema": dict(self.output_schema),
            "resource_keys": list(self.resource_keys),
            "requested_at": self.requested_at,
            "deadline_at": self.deadline_at,
            "status": self.status.value,
            "result": self.result,
            "evidence": dict(self.evidence),
            "error": self.error,
        }


@dataclass(frozen=True)
class ManagedState:
    run_id: str
    owner_id: str
    project_root: str
    bundle: str
    task: dict[str, Json]
    resource_keys: tuple[str, ...]
    detached: bool
    lease_seconds: float | None
    max_runtime_seconds: float
    stop_grace_seconds: float
    created_at: float
    updated_at: float
    status: ManagedStatus = ManagedStatus.STARTING
    stop_reason: str | None = None
    resources_released: bool = False
    config_version: int = 1
    event_seq: int = 0
    pid: int | None = None
    heartbeat_at: float | None = None
    lease_deadline: float | None = None
    controller: dict[str, Json] = field(default_factory=dict)
    cognition: tuple[CognitionJob, ...] = ()
    output: Json = None
    last_error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    def job(self, job_id: str) -> CognitionJob | None:
        return next((job for job in self.cognition if job.job_id == job_id), None)

    def pending_jobs(self) -> tuple[CognitionJob, ...]:
        return tuple(job for job in self.cognition if job.status is CognitionStatus.PENDING)

    def to_json(self, *, worker_alive: bool | None = None) -> dict[str, Json]:
        payload: dict[str, Json] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "project_root": self.project_root,
            "bundle": self.bundle,
            "task": dict(self.task),
            "resource_keys": list(self.resource_keys),
            "detached": self.detached,
            "lease_seconds": self.lease_seconds,
            "max_runtime_seconds": self.max_runtime_seconds,
            "stop_grace_seconds": self.stop_grace_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "stop_reason": self.stop_reason,
            "resources_released": self.resources_released,
            "config_version": self.config_version,
            "event_seq": self.event_seq,
            "pid": self.pid,
            "heartbeat_at": self.heartbeat_at,
            "lease_deadline": self.lease_deadline,
            "controller": dict(self.controller),
            "cognition": [
                job.to_json()
                if job.status is CognitionStatus.PENDING
                else {
                    "job_id": job.job_id,
                    "version": job.version,
                    "status": job.status.value,
                    "deadline_at": job.deadline_at,
                    "error": job.error,
                }
                for job in self.cognition[-100:]
            ],
            "pending_cognition": [job.to_json() for job in self.pending_jobs()],
            "output": self.output,
            "last_error": self.last_error,
        }
        if worker_alive is not None:
            payload["worker_alive"] = worker_alive
        return payload


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    owner_id: str
    project_root: str
    bundle: str
    task: dict[str, Json]
    resource_keys: tuple[str, ...] = ()
    detached: bool = False
    lease_seconds: float | None = 30.0
    max_runtime_seconds: float = 300.0
    stop_grace_seconds: float = 3.0
    bundle_config: dict[str, Json] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: dict[str, Json]) -> RunSpec:
        return cls(
            run_id=str(raw["run_id"]),
            owner_id=str(raw["owner_id"]),
            project_root=str(raw["project_root"]),
            bundle=str(raw["bundle"]),
            task=dict(raw["task"]),
            resource_keys=tuple(str(item) for item in raw.get("resource_keys") or ()),
            detached=bool(raw.get("detached", False)),
            lease_seconds=float(raw["lease_seconds"]) if raw.get("lease_seconds") is not None else None,
            max_runtime_seconds=float(raw.get("max_runtime_seconds", 300.0)),
            stop_grace_seconds=float(raw.get("stop_grace_seconds", 3.0)),
            bundle_config=dict(raw.get("bundle_config") or {}),
        )

    def to_json(self) -> dict[str, Json]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "project_root": self.project_root,
            "bundle": self.bundle,
            "task": dict(self.task),
            "resource_keys": list(self.resource_keys),
            "detached": self.detached,
            "lease_seconds": self.lease_seconds,
            "max_runtime_seconds": self.max_runtime_seconds,
            "stop_grace_seconds": self.stop_grace_seconds,
            "bundle_config": dict(self.bundle_config),
        }


@dataclass(frozen=True)
class ControllerResult:
    status: ManagedStatus
    reason: str | None = None
    output: Json = None
    resources_released: bool = True

    def __post_init__(self) -> None:
        if not self.status.terminal:
            raise ValueError("controller result must be terminal")

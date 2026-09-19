"""Pure reducer for managed-run lifecycle and cognition state."""

from __future__ import annotations

from dataclasses import replace

from .events import ManagedEvent, ManagedEventKind
from .types import CognitionJob, CognitionStatus, ManagedState, ManagedStatus


def _replace_job(state: ManagedState, job_id: str, **changes) -> tuple[CognitionJob, ...]:
    jobs: list[CognitionJob] = []
    found = False
    for job in state.cognition:
        if job.job_id == job_id:
            jobs.append(replace(job, **changes))
            found = True
        else:
            jobs.append(job)
    if not found:
        raise ValueError(f"unknown cognition job {job_id!r}")
    return tuple(jobs)


def apply(state: ManagedState | None, event: ManagedEvent) -> ManagedState:
    data = event.data
    if event.kind is ManagedEventKind.RUN_CREATED:
        if state is not None:
            raise ValueError("run_created must be the first event")
        lease_seconds = data.get("lease_seconds")
        detached = bool(data.get("detached", False))
        resource_keys = tuple(str(item) for item in data.get("resource_keys") or ())
        return ManagedState(
            run_id=str(data["run_id"]),
            owner_id=str(data["owner_id"]),
            project_root=str(data["project_root"]),
            bundle=str(data["bundle"]),
            task=dict(data["task"]),
            resource_keys=resource_keys,
            detached=detached,
            lease_seconds=float(lease_seconds) if lease_seconds is not None else None,
            max_runtime_seconds=float(data["max_runtime_seconds"]),
            stop_grace_seconds=float(data["stop_grace_seconds"]),
            created_at=event.at,
            updated_at=event.at,
            event_seq=event.seq,
            heartbeat_at=event.at,
            lease_deadline=None if detached or lease_seconds is None else event.at + float(lease_seconds),
            resources_released=not resource_keys,
        )

    if state is None:
        raise ValueError(f"{event.kind.value} cannot precede run_created")

    common = {"updated_at": event.at, "event_seq": event.seq}

    if event.kind is ManagedEventKind.WORKER_STARTED:
        if state.status is not ManagedStatus.STARTING:
            raise ValueError(f"cannot start worker from {state.status.value}")
        lease_deadline = state.lease_deadline
        if not state.detached and state.lease_seconds is not None:
            lease_deadline = event.at + state.lease_seconds
        return replace(
            state,
            **common,
            status=ManagedStatus.RUNNING,
            pid=int(data["pid"]),
            heartbeat_at=event.at,
            lease_deadline=lease_deadline,
        )

    if event.kind is ManagedEventKind.HEARTBEAT:
        lease_deadline = state.lease_deadline
        if not state.detached and state.lease_seconds is not None:
            lease_deadline = event.at + state.lease_seconds
        return replace(state, **common, heartbeat_at=event.at, lease_deadline=lease_deadline)

    if event.kind is ManagedEventKind.CONTROLLER_SNAPSHOT:
        return replace(state, **common, controller=dict(data.get("snapshot") or {}))

    if event.kind is ManagedEventKind.TASK_UPDATED:
        task = {**state.task, **dict(data.get("task") or {})}
        return replace(state, **common, task=task, config_version=state.config_version + 1)

    if event.kind is ManagedEventKind.COGNITION_REQUESTED:
        if state.job(str(data["job_id"])) is not None:
            raise ValueError(f"duplicate cognition job {data['job_id']!r}")
        deadline = data.get("deadline_at")
        job = CognitionJob(
            job_id=str(data["job_id"]),
            version=1,
            question=str(data["question"]),
            context=dict(data.get("context") or {}),
            output_schema=dict(data.get("output_schema") or {}),
            resource_keys=tuple(str(item) for item in data.get("resource_keys") or ()),
            requested_at=event.at,
            deadline_at=float(deadline) if deadline is not None else None,
        )
        return replace(state, **common, cognition=(*state.cognition, job))

    if event.kind is ManagedEventKind.COGNITION_COMPLETED:
        current = state.job(str(data["job_id"]))
        if current is None or current.status is not CognitionStatus.PENDING:
            raise ValueError(f"cognition job {data['job_id']!r} is not pending")
        version = int(data["version"])
        if version != current.version + 1:
            raise ValueError(f"cognition job {current.job_id!r} completion version must be {current.version + 1}")
        jobs = _replace_job(
            state,
            current.job_id,
            version=version,
            status=CognitionStatus.COMPLETED,
            result=data.get("result"),
            evidence=dict(data.get("evidence") or {}),
            error=None,
        )
        return replace(state, **common, cognition=jobs)

    if event.kind in {ManagedEventKind.COGNITION_CANCELLED, ManagedEventKind.COGNITION_EXPIRED}:
        status = (
            CognitionStatus.CANCELLED
            if event.kind is ManagedEventKind.COGNITION_CANCELLED
            else CognitionStatus.EXPIRED
        )
        current = state.job(str(data["job_id"]))
        if current is None:
            raise ValueError(f"unknown cognition job {data['job_id']!r}")
        if current.status is not CognitionStatus.PENDING:
            raise ValueError(f"cognition job {current.job_id!r} is already {current.status.value}")
        jobs = _replace_job(
            state,
            current.job_id,
            version=current.version + 1,
            status=status,
            error=str(data.get("error") or status.value),
        )
        return replace(state, **common, cognition=jobs)

    if event.kind is ManagedEventKind.STOP_REQUESTED:
        if state.terminal:
            return replace(state, **common)
        return replace(
            state,
            **common,
            status=ManagedStatus.STOPPING,
            stop_reason=str(data.get("reason") or "stop_requested"),
        )

    if event.kind is ManagedEventKind.RESOURCES_RELEASED:
        return replace(state, **common, resources_released=True)

    if event.kind is ManagedEventKind.RUN_FINISHED:
        status = ManagedStatus(str(data["status"]))
        if not status.terminal:
            raise ValueError("run_finished requires a terminal status")
        if state.terminal:
            raise ValueError(f"run is already {state.status.value}")
        return replace(
            state,
            **common,
            status=status,
            stop_reason=str(data["reason"]) if data.get("reason") is not None else None,
            output=data.get("output"),
        )

    if event.kind is ManagedEventKind.ERROR:
        return replace(state, **common, last_error=str(data.get("error") or "unknown error"))

    return replace(state, **common)


def replay(events: tuple[ManagedEvent, ...] | list[ManagedEvent]) -> ManagedState:
    state: ManagedState | None = None
    previous_seq = 0
    for event in events:
        if event.seq != previous_seq + 1:
            raise ValueError(f"managed event sequence moved from {previous_seq} to {event.seq}")
        state = apply(state, event)
        previous_seq = event.seq
    if state is None:
        raise ValueError("managed journal is empty")
    return state

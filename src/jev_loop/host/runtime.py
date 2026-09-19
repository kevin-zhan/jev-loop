"""Bundle contract, cognition broker and adapters for managed runs."""

from __future__ import annotations

import importlib
import json
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.loop import Loop
from ..core.types import RunStatus
from .events import ManagedEventKind
from .schema import SchemaValidationError
from .schema import validate as validate_schema
from .store import ManagedRecorder
from .types import CognitionJob, CognitionStatus, ControllerResult, ManagedStatus, RunSpec

Json = Any
MAX_PENDING_COGNITION = 8
MAX_COGNITION_QUESTION_CHARS = 4_000
MAX_COGNITION_CONTEXT_BYTES = 32_000
MAX_COGNITION_SCHEMA_BYTES = 16_000
MAX_COGNITION_RESULT_BYTES = 200_000
MAX_COGNITION_EVIDENCE_BYTES = 50_000


class BundleError(RuntimeError):
    """A bundle manifest or controller violates the managed-host contract."""


class CognitionError(RuntimeError):
    """A cognition result is stale, late, or belongs to another job."""


@runtime_checkable
class ManagedController(Protocol):
    """Thread-aware controller contract loaded from a bundle.

    ``run`` executes on the engine thread.  The other three methods may be called by the
    coordinator thread and therefore must be thread-safe.  ``request_stop`` must release
    held external inputs synchronously; it must not wait for a model or a network response.
    """

    def run(self, stop_event: threading.Event) -> ControllerResult: ...

    def snapshot(self) -> Mapping[str, Json]: ...

    def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None: ...

    def request_stop(self, reason: str) -> None: ...


class CognitionBroker:
    """Create asynchronous cognition jobs and accept version-bound results."""

    def __init__(self, recorder: ManagedRecorder, changed: threading.Event) -> None:
        self._recorder = recorder
        self._changed = changed
        self._lock = threading.RLock()

    def request(
        self,
        question: str,
        *,
        context: Mapping[str, Json] | None = None,
        output_schema: Mapping[str, Json] | None = None,
        resource_keys: tuple[str, ...] = (),
        deadline_seconds: float | None = 120.0,
        dedupe_key: str | None = None,
    ) -> str:
        if not question.strip():
            raise ValueError("cognition question cannot be empty")
        if len(question) > MAX_COGNITION_QUESTION_CHARS:
            raise ValueError(f"cognition question exceeds {MAX_COGNITION_QUESTION_CHARS} characters")
        deadline = float(deadline_seconds) if deadline_seconds is not None else None
        if deadline is not None and not 1 <= deadline <= 3600:
            raise ValueError("cognition deadline_seconds must be between 1 and 3600")
        if dedupe_key is not None and len(dedupe_key) > 200:
            raise ValueError("cognition dedupe_key exceeds 200 characters")
        if len(resource_keys) > 16 or any(not key or len(key) > 200 for key in resource_keys):
            raise ValueError("cognition resource_keys must contain at most 16 non-empty keys of 200 characters")
        job_context = dict(context or {})
        schema = dict(output_schema or {})
        if dedupe_key:
            job_context["dedupe_key"] = dedupe_key
        _ensure_json_size(job_context, MAX_COGNITION_CONTEXT_BYTES, "cognition context")
        _ensure_json_size(schema, MAX_COGNITION_SCHEMA_BYTES, "cognition output schema")
        with self._lock:
            if dedupe_key:
                for job in self._recorder.state.cognition:
                    if job.context.get("dedupe_key") == dedupe_key and job.status is CognitionStatus.PENDING:
                        return job.job_id
            if len(self._recorder.state.pending_jobs()) >= MAX_PENDING_COGNITION:
                raise ValueError(f"run already has {MAX_PENDING_COGNITION} pending cognition jobs")
            job_id = "job_" + uuid.uuid4().hex[:12]
            now = time.time()
            self._recorder.emit(
                ManagedEventKind.COGNITION_REQUESTED,
                {
                    "job_id": job_id,
                    "question": question,
                    "context": job_context,
                    "output_schema": schema,
                    "resource_keys": list(resource_keys),
                    "deadline_at": now + deadline if deadline is not None else None,
                },
                at=now,
            )
            self._changed.set()
            return job_id

    def get(self, job_id: str) -> CognitionJob | None:
        return self._recorder.state.job(job_id)

    def completed(self, job_id: str) -> CognitionJob | None:
        job = self.get(job_id)
        return job if job and job.status is CognitionStatus.COMPLETED else None

    def result(self, job_id: str) -> Json:
        """Return a completed value, or ``None`` while incomplete.

        Use :meth:`completed` when JSON ``null`` is a valid result and the distinction
        matters.
        """
        job = self.completed(job_id)
        return job.result if job else None

    def complete(
        self,
        job_id: str,
        *,
        expected_version: int,
        result: Json,
        evidence: Mapping[str, Json] | None = None,
    ) -> CognitionJob:
        with self._lock:
            job = self._recorder.state.job(job_id)
            if job is None:
                raise CognitionError(f"unknown cognition job {job_id!r}")
            if job.status is not CognitionStatus.PENDING:
                raise CognitionError(f"cognition job {job_id!r} is already {job.status.value}")
            if job.version != expected_version:
                raise CognitionError(
                    f"cognition job {job_id!r} version moved from {expected_version} to {job.version}"
                )
            if job.deadline_at is not None and time.time() > job.deadline_at:
                self._recorder.emit(
                    ManagedEventKind.COGNITION_EXPIRED,
                    {"job_id": job_id, "error": "result arrived after deadline"},
                )
                self._changed.set()
                raise CognitionError(f"cognition job {job_id!r} expired before the result arrived")
            result_evidence = dict(evidence or {})
            _ensure_json_size(result, MAX_COGNITION_RESULT_BYTES, "cognition result")
            _ensure_json_size(result_evidence, MAX_COGNITION_EVIDENCE_BYTES, "cognition evidence")
            try:
                validate_schema(result, job.output_schema)
            except SchemaValidationError as error:
                raise CognitionError(f"cognition result for {job_id!r} does not match its schema: {error}") from error
            self._recorder.emit(
                ManagedEventKind.COGNITION_COMPLETED,
                {
                    "job_id": job_id,
                    "version": job.version + 1,
                    "result": result,
                    "evidence": result_evidence,
                },
            )
            self._changed.set()
            completed = self._recorder.state.job(job_id)
            if completed is None:  # reducer invariant
                raise AssertionError("completed cognition job disappeared")
            return completed

    def expire_due(self, *, now: float | None = None) -> tuple[str, ...]:
        expired: list[str] = []
        instant = time.time() if now is None else now
        with self._lock:
            for job in self._recorder.state.pending_jobs():
                if job.deadline_at is not None and instant > job.deadline_at:
                    self._recorder.emit(
                        ManagedEventKind.COGNITION_EXPIRED,
                        {"job_id": job.job_id, "error": "cognition deadline elapsed"},
                        at=instant,
                    )
                    expired.append(job.job_id)
            if expired:
                self._changed.set()
        return tuple(expired)

    def cancel_pending(self, reason: str) -> None:
        with self._lock:
            for job in self._recorder.state.pending_jobs():
                self._recorder.emit(
                    ManagedEventKind.COGNITION_CANCELLED,
                    {"job_id": job.job_id, "error": reason},
                )
            self._changed.set()


@dataclass
class RuntimeServices:
    recorder: ManagedRecorder
    cognition: CognitionBroker
    changed: threading.Event

    @property
    def state(self):
        return self.recorder.state

    def wait_for_change(self, timeout: float) -> bool:
        changed = self.changed.wait(timeout)
        self.changed.clear()
        return changed


@dataclass(frozen=True)
class WakeResult:
    input: Json = None
    task_update: Mapping[str, Json] | None = None


def _ensure_json_size(value: Json, maximum: int, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not JSON-serializable: {error}") from error
    if len(encoded) > maximum:
        raise ValueError(f"{label} is {len(encoded)} bytes, over limit {maximum}")


class LoopController:
    """Run the existing synchronous kernel under the managed host.

    A suspended loop does not consume steps.  It wakes only for a task update or when
    ``on_wake`` says newly available external/cognition state is relevant.  This adapter
    never calls ``Loop.cancel`` concurrently with ``Loop.step``.
    """

    def __init__(
        self,
        loop: Loop,
        services: RuntimeServices,
        *,
        on_wake: Callable[[Loop, RuntimeServices], WakeResult | None] | None = None,
        on_update: Callable[[Loop, Mapping[str, Json], Mapping[str, Json]], None] | None = None,
        release: Callable[[str], None] | None = None,
        snapshot_extra: Callable[[], Mapping[str, Json]] | None = None,
        idle_poll_seconds: float = 0.1,
    ) -> None:
        self.loop = loop
        self.services = services
        self.on_wake = on_wake
        self.on_update = on_update
        self.release = release
        self.snapshot_extra = snapshot_extra
        self.idle_poll_seconds = idle_poll_seconds
        self._lock = threading.RLock()
        self._updates: list[tuple[dict[str, Json], dict[str, Json]]] = []
        self._stop_reason: str | None = None

    def run(self, stop_event: threading.Event) -> ControllerResult:
        try:
            while not stop_event.is_set():
                self._drain_updates()
                if self.loop.state.status is RunStatus.RUNNING:
                    self.loop.step()
                    continue
                if self.loop.state.status.terminal:
                    return _loop_result(self.loop)
                self.services.wait_for_change(self.idle_poll_seconds)
                self._drain_updates()
                if self.loop.state.status is not RunStatus.SUSPENDED or self.on_wake is None:
                    continue
                wake = self.on_wake(self.loop, self.services)
                if wake is not None:
                    self.loop.resume(wake.input, task_update=wake.task_update)
            self.loop.cancel()
            return ControllerResult(ManagedStatus.CANCELLED, self._stop_reason or "stop_requested")
        finally:
            self._release(self._stop_reason or "controller_exit")

    def snapshot(self) -> Mapping[str, Json]:
        state = self.loop.state
        snapshot: dict[str, Json] = {
            "kernel_status": state.status.value,
            "kernel_stop_reason": state.stop_reason.value if state.stop_reason else None,
            "step": state.step,
            "observation_revision": state.revision,
            "executions": state.counters.get("executions", 0),
            "decisions": state.counters.get("decisions", 0),
        }
        if self.snapshot_extra is not None:
            snapshot.update(dict(self.snapshot_extra()))
        return snapshot

    def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None:
        with self._lock:
            self._updates.append((dict(task), dict(patch)))
        self.services.changed.set()

    def request_stop(self, reason: str) -> None:
        with self._lock:
            self._stop_reason = reason
        self._release(reason)
        self.services.changed.set()

    def _drain_updates(self) -> None:
        with self._lock:
            updates, self._updates = self._updates, []
        for task, patch in updates:
            if self.on_update is not None:
                self.on_update(self.loop, task, patch)
            elif not self.loop.state.status.terminal:
                self.loop.resume(input={"source": "host_update"}, task_update=task)

    def _release(self, reason: str) -> None:
        if self.release is not None:
            self.release(reason)


def _loop_result(loop: Loop) -> ControllerResult:
    status = loop.state.status
    mapping = {
        RunStatus.SUCCEEDED: ManagedStatus.SUCCEEDED,
        RunStatus.FAILED: ManagedStatus.FAILED,
        RunStatus.CANCELLED: ManagedStatus.CANCELLED,
    }
    managed = mapping.get(status, ManagedStatus.FAILED)
    return ControllerResult(
        managed,
        loop.state.stop_reason.value if loop.state.stop_reason else None,
        {
            "steps": loop.state.step,
            "observations": loop.state.observations,
            "executions": loop.state.counters.get("executions", 0),
        },
    )


def load_controller(spec: RunSpec, services: RuntimeServices) -> ManagedController:
    if spec.bundle == "diagnostic":
        from .diagnostic import DiagnosticController

        return DiagnosticController(spec, services)

    manifest_path = Path(spec.bundle).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleError(f"cannot read bundle manifest {manifest_path}: {error}") from error
    if manifest.get("schema_version") != 1:
        raise BundleError("bundle schema_version must be 1")
    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, str) or ":" not in entrypoint:
        raise BundleError("bundle entrypoint must be 'module:function'")
    python_path = (manifest_path.parent / str(manifest.get("python_path", "."))).resolve()
    if not python_path.is_dir():
        raise BundleError(f"bundle python_path is not a directory: {python_path}")
    module_name, function_name = entrypoint.split(":", 1)
    # Keep the trusted bundle path available for lazy imports during the run.
    if str(python_path) not in sys.path:
        sys.path.insert(0, str(python_path))
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, function_name)
        config = {**dict(manifest.get("config") or {}), **spec.bundle_config}
        controller = factory(spec, services, config)
    except Exception as error:
        raise BundleError(f"bundle factory {entrypoint!r} failed: {error!r}") from error
    if not isinstance(controller, ManagedController):
        raise BundleError(f"bundle factory {entrypoint!r} did not return a ManagedController")
    return controller

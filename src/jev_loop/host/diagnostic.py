"""Built-in diagnostic bundle used to prove non-blocking cognition and safe stop.

This is intentionally not a toy website adapter.  It is a contract probe: an independent
world clock and maintained input continue while a cognition job is pending, and every exit
path releases that input synchronously.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from .runtime import RuntimeServices
from .types import ControllerResult, ManagedStatus, RunSpec

Json = Any


class DiagnosticController:
    def __init__(self, spec: RunSpec, services: RuntimeServices) -> None:
        self.spec = spec
        self.services = services
        self._lock = threading.RLock()
        self._desired_forward = False
        self._confirmed_forward = False
        self._world_ticks = 0
        self._decisions_while_waiting = 0
        self._job_id: str | None = None
        self._stop_reason: str | None = None
        self._task = dict(spec.task)

    def run(self, stop_event: threading.Event) -> ControllerResult:
        with self._lock:
            self._desired_forward = True
            self._confirmed_forward = True
        self._job_id = self.services.cognition.request(
            "Return a short plan for completing the diagnostic run.",
            context={"goal": self._task.get("goal"), "note": "world ticks must continue while this is pending"},
            output_schema={"type": "object", "required": ["plan"], "properties": {"plan": {"type": "string"}}},
            resource_keys=("diagnostic-plan",),
            deadline_seconds=float(self.spec.bundle_config.get("cognition_deadline_seconds", 30.0)),
            dedupe_key="diagnostic-plan",
        )

        interval = float(self.spec.bundle_config.get("tick_seconds", 0.05))
        minimum = int(self.spec.bundle_config.get("minimum_decisions_while_waiting", 3))
        while not stop_event.wait(interval):
            with self._lock:
                self._world_ticks += 1  # advances independently of the cognition result
                completed = self.services.cognition.completed(self._job_id)
                if completed is None:
                    self._decisions_while_waiting += 1
                enough_work = self._decisions_while_waiting >= minimum
            if completed is not None and enough_work:
                self._release_inputs()
                return ControllerResult(
                    ManagedStatus.SUCCEEDED,
                    output={
                        "cognition": completed.result,
                        "world_ticks": self._world_ticks,
                        "decisions_while_waiting": self._decisions_while_waiting,
                        "inputs_released": not self._confirmed_forward,
                    },
                )

            job = self.services.cognition.get(self._job_id)
            if job is not None and job.status.value in {"expired", "cancelled"}:
                self._release_inputs()
                return ControllerResult(ManagedStatus.FAILED, f"cognition_{job.status.value}")

        self._release_inputs()
        return ControllerResult(ManagedStatus.CANCELLED, self._stop_reason or "stop_requested")

    def snapshot(self) -> Mapping[str, Json]:
        with self._lock:
            return {
                "world_ticks": self._world_ticks,
                "decisions_while_waiting": self._decisions_while_waiting,
                "desired_inputs": {"forward": self._desired_forward},
                "confirmed_inputs": {"forward": self._confirmed_forward},
                "cognition_job_id": self._job_id,
            }

    def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None:
        with self._lock:
            self._task = dict(task)

    def request_stop(self, reason: str) -> None:
        with self._lock:
            self._stop_reason = reason
        self._release_inputs()  # synchronous safety boundary; never waits for the engine thread

    def _release_inputs(self) -> None:
        with self._lock:
            self._desired_forward = False
            self._confirmed_forward = False

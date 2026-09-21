"""Scaffold controller for the __BUNDLE_NAME__ bundle.

This file is a starting point, not an implementation.  ``run`` raises until the real
environment adapter and the independent verifier exist.  Read the bundle contract in
``BUNDLE.md`` and the Jev Bundle Specification before filling it in.

Rules that do not change when you implement this:

- ``run`` executes on the engine thread.  The other three methods may be called from the
  coordinator thread and must be thread-safe.
- ``request_stop`` must release held external inputs *synchronously* and must never wait
  for a model or the network.
- Return ``resources_released=True`` only when every external input is confirmed released.
  If that cannot be confirmed, return ``False``: the host keeps the resource claim
  quarantined instead of pretending the stop was safe.
- Keep ``snapshot`` small and JSON-serializable.  Full evidence belongs in the run's
  ``artifacts/`` directory.
- Ask slow questions through ``services.cognition.request(...)`` instead of blocking the
  loop; the loop must keep making progress while the answer is pending.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from jev_loop.host import ControllerResult, ManagedStatus

Json = Any


def build(spec, services, config) -> ExampleController:
    """Entrypoint declared by ``bundle.json`` as ``bundle_controller:build``."""
    return ExampleController(spec, services, dict(config))


class ExampleController:
    def __init__(self, spec, services, config: Mapping[str, Json]) -> None:
        self.spec = spec
        self.services = services
        self.config = dict(config)
        self._lock = threading.RLock()
        self._stop_reason: str | None = None
        self._inputs_held = False
        self._task = dict(spec.task)

    def run(self, stop_event: threading.Event) -> ControllerResult:
        # Replace this with the real observe -> decide -> act -> update cycle, for example
        # by hosting a jev_loop.Loop through jev_loop.host.LoopController.
        raise NotImplementedError(
            "this bundle is still a scaffold: implement the environment adapter, the independent "
            "verifier and the loop, then set scaffold=false in bundle.json"
        )

    def snapshot(self) -> Mapping[str, Json]:
        with self._lock:
            return {
                "scaffold": True,
                "inputs_held": self._inputs_held,
                "goal": self._task.get("goal"),
                "config": dict(self.config),
            }

    def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None:
        with self._lock:
            self._task = dict(task)

    def request_stop(self, reason: str) -> None:
        with self._lock:
            self._stop_reason = reason
        self._release_inputs()

    def _release_inputs(self) -> None:
        # Release every external input here, synchronously.  This scaffold holds none, so
        # there is nothing to release; a real driver must confirm each release.
        with self._lock:
            self._inputs_held = False

    def _request_plan(self) -> str:
        """Example of a bounded slow question; delete it if the bundle does not need one."""
        return self.services.cognition.request(
            "Replace this with one bounded question the runtime cannot answer locally.",
            context={"goal": self._task.get("goal")},
            output_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}},
            deadline_seconds=90,
            dedupe_key="__BUNDLE_NAME__-plan",
        )

    def _succeeded(self, output: Json) -> ControllerResult:
        return ControllerResult(ManagedStatus.SUCCEEDED, output=output, resources_released=not self._inputs_held)

    def _cancelled(self) -> ControllerResult:
        return ControllerResult(
            ManagedStatus.CANCELLED,
            self._stop_reason or "stop_requested",
            resources_released=not self._inputs_held,
        )

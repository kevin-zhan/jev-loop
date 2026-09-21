"""Managed-host (pi-jev) bundle for the live-files reference example.

The manifest points at ``controller:build`` with ``python_path: "."`` so the sibling
``environment`` module is importable.  The workspace is always run-private
(``run_dir/artifacts/workspace``): two runs can never share or overwrite a directory, and
no resource claim is needed for the filesystem itself.  The verifier evidence is written to
``run_dir/artifacts/verification.json`` and mirrored into the run snapshot, so ``inspect``
shows the verdict and the kernel's terminal detail without trusting ``start`` returning ok.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from environment import (
    GOAL,
    SUCCESS_CRITERIA,
    LiveFilesEnvironment,
    LiveFilesVerifier,
    prepare_workspace,
    seed_workspace,
)

from jev_loop import Loop, LoopConfig, TaskSpec
from jev_loop.host import LoopController
from jev_loop.policies.config import (
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    validate_api_url,
    validate_model,
    validate_timeout,
)
from jev_loop.policies.jev import JevPolicy, default_request_fn

ALLOWED_CONFIG = frozenset({"model", "api_url", "timeout", "max_steps"})
DEFAULT_MAX_STEPS = 8
MAX_STEPS_LIMIT = 64


def _resolve_max_steps(config) -> int:
    """Read the decision-request budget from config; never fall back silently to the default."""
    value = config.get("max_steps", DEFAULT_MAX_STEPS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_STEPS_LIMIT:
        raise ValueError(f"config max_steps must be an integer between 1 and {MAX_STEPS_LIMIT}")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _run_finished_detail(loop: Loop) -> str:
    for event in reversed(loop.timeline()):
        if event.kind.value == "run_finished":
            return str(event.data.get("detail") or "")
    return ""


class _RecordingVerifier:
    """Wrap the verifier so the last verdict is visible to the host and on disk."""

    def __init__(self, inner: LiveFilesVerifier, evidence_path: Path) -> None:
        self.inner = inner
        self.evidence_path = evidence_path
        self.last = None

    def verify(self, state):
        result = self.inner.verify(state)
        self.last = result
        _write_json(
            self.evidence_path,
            {
                "example": "live-files",
                "verdict": result.verdict.value,
                "detail": result.detail,
                "evidence": dict(result.evidence or {}),
            },
        )
        return result


def build(spec, services, config):
    unknown = sorted(set(config) - ALLOWED_CONFIG)
    if unknown:
        raise ValueError(
            f"unknown config keys {unknown}; the live-files bundle reads only {sorted(ALLOWED_CONFIG)} "
            "and always uses its run-private workspace"
        )
    # Pass the raw config values to the shared validators: an absent key falls back to the default,
    # while an explicit null or a wrong type is refused by the validator instead of being coerced
    # into a plausible-looking string.
    endpoint = validate_api_url(config.get("api_url", DEFAULT_API_URL))
    model = validate_model(config.get("model", DEFAULT_MODEL))
    timeout = validate_timeout(config.get("timeout", DEFAULT_TIMEOUT))
    max_steps = _resolve_max_steps(config)
    # Credential first: a missing key must fail before the workspace is created.
    request_fn = default_request_fn(url=endpoint, timeout=timeout)

    workspace = prepare_workspace(services.recorder.run_dir / "artifacts" / "workspace")
    seed_workspace(workspace)
    evidence_path = services.recorder.run_dir / "artifacts" / "verification.json"

    environment = LiveFilesEnvironment(workspace)
    verifier = _RecordingVerifier(LiveFilesVerifier(workspace), evidence_path)
    policy = JevPolicy(request_fn=request_fn, model=model)
    loop = Loop(
        task=TaskSpec(
            goal=str(spec.task.get("goal") or GOAL),
            inputs=dict(spec.task.get("inputs") or {}),
            constraints=tuple(spec.task.get("constraints") or ()),
            success_criteria=tuple(spec.task.get("success_criteria") or SUCCESS_CRITERIA),
            authorization=tuple(spec.task.get("authorization") or ()),
        ),
        environment=environment,
        policy=policy,
        verifier=verifier,
        config=LoopConfig(max_steps=max_steps, max_seconds=float(spec.max_runtime_seconds)),
    )

    def snapshot_extra() -> Mapping[str, Any]:
        last = verifier.last
        return {
            "workspace": str(workspace),
            "evidence_path": str(evidence_path),
            "kernel_detail": _run_finished_detail(loop),
            "max_steps": max_steps,
            "decision_requests": len(policy.requests),
            "verification": None
            if last is None
            else {
                "verdict": last.verdict.value,
                "detail": last.detail,
                "evidence": dict(last.evidence or {}),
            },
        }

    return LoopController(loop, services, snapshot_extra=snapshot_extra)

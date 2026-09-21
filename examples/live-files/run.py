"""Run the live Jev + real-filesystem reference example (synthetic data).

    uv run --env-file .env python examples/live-files/run.py --workspace <new-or-empty-dir>

This script makes real Jev decision requests (one per loop step, bounded by ``--max-steps``)
against a private workspace it creates itself.  It is a reference example, not a benchmark
and not a production adapter; the offline tests exercise the same environment with a fake
requester instead of a paid API.  The credential is read from the process environment only
(``TYPESAFE_API_KEY`` by default) and is never printed.  The workspace is never deleted
automatically: the run prints where it is, and removing it is a manual step.

The evidence path is protected: an existing file or symlink is never overwritten, a path
reserved by the example (the workspace itself, its ``inbox/``/``archive/`` data) is refused,
and the file is created exclusively.  Failures **before the run starts** print a fixed,
secret-free message; with ``--json`` they also produce the machine-readable not-started
envelope (``status=not_started``, ``failure_kind``, ``decision_requests=0``, ``verified=false``).
A failure **after** the run (the evidence file could not be written) keeps the real
request/usage/verification counters and reports ``failure_kind=evidence_write_failed``.

Exit codes: 0 the verifier confirmed success, 1 the loop did not succeed, 2 credential
missing/blank/unusable or rejected by the service, 3 configuration, usage, workspace or
evidence-path problem.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from environment import (
    GOAL,
    SUCCESS_CRITERIA,
    LiveFilesEnvironment,
    LiveFilesVerifier,
    WorkspaceError,
    prepare_workspace,
    seed_workspace,
)

from jev_loop import Loop, LoopConfig, RunStatus, TaskSpec
from jev_loop.policies.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    ConfigError,
    CredentialError,
    validate_api_url,
    validate_model,
    validate_timeout,
)
from jev_loop.policies.jev import JevPolicy, JevRequestError, default_request_fn

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_CREDENTIAL = 2
EXIT_CONFIG = 3

TRACKED_EVENTS = (
    "observed",
    "decision",
    "execution_receipt",
    "guard",
    "verification",
    "suspended",
    "run_finished",
    "step",
)

RESERVED_WORKSPACE_DIRS = frozenset({"inbox", "archive"})
CLEANUP_NOTE = (
    "manual: review the workspace, then remove the directory yourself; "
    "this example never deletes anything automatically"
)
WORKSPACE_ERROR_DETAIL = (
    "the workspace must be a new or empty directory that is not a symlink; "
    "this example never overwrites or deletes existing content"
)
EVIDENCE_TARGET_DETAIL = (
    "the evidence path must be a new file that is not the workspace itself or its inbox/archive data; "
    "this example never overwrites or replaces an existing file"
)
EVIDENCE_WRITE_DETAIL = "the evidence file could not be written exclusively; nothing was overwritten"
USAGE_ERROR_DETAIL = "invalid command-line usage; run with --help"


class _UsageError(Exception):
    """Command-line usage problem, reported as a configuration failure (exit 3)."""


class EvidenceError(RuntimeError):
    """The requested evidence path cannot be written without overwriting or shadowing data."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="run.py",
        description=(
            "Live Jev + real filesystem reference example with synthetic data. "
            "Creates a private workspace and makes real (paid) Jev decision requests."
        ),
        epilog=(
            "exit codes: 0 success, 1 run failed, 2 credential missing or rejected, "
            "3 configuration/workspace/evidence/usage problem"
        ),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="new or empty directory to use; default: a fresh temporary directory under the system temp dir",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="where to write the verifier evidence JSON; default: <workspace>/verification.json",
    )
    parser.add_argument("--max-steps", type=int, default=8, help="hard decision/step budget (default 8)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="endpoint; https only except a loopback test URL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model name (default {DEFAULT_MODEL})")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-request timeout (default {DEFAULT_TIMEOUT:g}s)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON summary instead of the human trace")
    return parser


def _usage_totals(responses: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for response in responses:
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[str(key)] = totals.get(str(key), 0.0) + float(value)
    return totals


def _run_finished_detail(loop: Loop) -> str:
    for event in reversed(loop.timeline()):
        if event.kind.value == "run_finished":
            return str(event.data.get("detail") or "")
    return ""


def _failure_payload(*, failure_kind: str, detail: str) -> dict[str, Any]:
    """Fixed, secret-free envelope for failures that happen before a run result exists."""
    return {
        "example": "live-files",
        "mode": "live",
        "status": "not_started",
        "stop_reason": None,
        "failure_kind": failure_kind,
        "detail": detail,
        "steps": 0,
        "executions": 0,
        "decisions": 0,
        "decision_requests": 0,
        "usage": {},
        "verification": None,
        "verified": False,
        "workspace": None,
        "evidence_path": None,
        "evidence_written": False,
        "cleanup": CLEANUP_NOTE,
    }


def _fail(
    failure_kind: str,
    detail: str,
    *,
    human: str,
    json_mode: bool,
    exit_code: int,
) -> int:
    print(f"{failure_kind}: {human}", file=sys.stderr)
    if json_mode:
        payload = _failure_payload(failure_kind=failure_kind, detail=detail)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def _check_evidence_parent(target: Path) -> None:
    """Refuse an evidence path whose nearest existing ancestor is not a directory.

    A path like ``<regular-file>/report.json`` can never be created, so it is refused during
    preflight (before any request) instead of surfacing as an OS error at write time.
    """
    parent = target.parent
    while True:
        if parent.exists():
            if not parent.is_dir():
                raise EvidenceError("the evidence path has a parent that is not a directory")
            return
        if parent == parent.parent:
            return
        parent = parent.parent


def _check_evidence_target(path: Path, workspace: Path | None) -> Path:
    """Refuse an evidence path that already exists or that shadows the example's own data.

    Checked before the workspace is created, seeded or any request is made, so a mistake costs
    nothing.  ``workspace`` is the intended workspace path (it may not exist yet); the reserved
    data check is skipped when there is no workspace path to compare against yet.
    """
    target = Path(path).expanduser()
    if target.is_symlink() or target.exists():
        raise EvidenceError("the evidence path already exists")
    _check_evidence_parent(target)
    if workspace is None:
        return target
    resolved_target = target.resolve()
    resolved_workspace = Path(workspace).expanduser().resolve()
    if resolved_target == resolved_workspace:
        raise EvidenceError("the evidence path is the workspace directory itself")
    if resolved_workspace in resolved_target.parents:
        relative = resolved_target.relative_to(resolved_workspace)
        if relative.parts and relative.parts[0] in RESERVED_WORKSPACE_DIRS:
            raise EvidenceError("the evidence path is inside the example's inbox/ or archive/ data")
    return target


def _evidence_preflight(path: Path, workspace: Path | None) -> Path:
    """Run the evidence target checks, mapping every expected path failure safely.

    The path checks themselves touch the filesystem (``exists``/``is_dir``/``resolve``), which can
    raise ``OSError``, ``RuntimeError`` (symlink loops) or ``ValueError`` (embedded NUL).  Those
    become one fixed :class:`EvidenceError` with ``from None``: no raw exception text and no path
    reaches a log or a traceback.  A deliberate :class:`EvidenceError` passes through unchanged.
    """
    try:
        return _check_evidence_target(path, workspace)
    except EvidenceError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise EvidenceError("the evidence path could not be verified") from None


def _write_evidence(path: Path, payload: Mapping[str, Any]) -> None:
    """Write exclusively: never replace, never follow a symlink, no predictable temp file.

    Every expected OS failure (creating the parent directory, opening, writing, flushing,
    syncing) becomes a fixed :class:`EvidenceError` with ``from None``, so no raw exception text
    or path can reach a log or a traceback.  Cleanup only ever removes the file this call created,
    and it never raises over the primary error.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise EvidenceError("the evidence path could not be prepared") from None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise EvidenceError("the evidence path already exists") from None
    except OSError:
        raise EvidenceError("the evidence path could not be created") from None

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # never mask the primary failure, and only ever remove the file we created
        raise EvidenceError("the evidence file could not be written completely") from None


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    try:
        args = _parser().parse_args(raw)
    except _UsageError:
        # argparse messages can contain the raw argument; report a fixed message instead.
        return _fail(
            "usage_error",
            USAGE_ERROR_DETAIL,
            human=USAGE_ERROR_DETAIL,
            json_mode=json_mode,
            exit_code=EXIT_CONFIG,
        )

    # 1. configuration and credential first: nothing is created or modified before they pass.
    try:
        if args.max_steps < 1:
            raise ConfigError("--max-steps must be a positive integer")
        endpoint = validate_api_url(args.api_url)
        model = validate_model(args.model)
        seconds = validate_timeout(args.timeout)
        request_fn = default_request_fn(url=endpoint, timeout=seconds)
    except ConfigError as error:
        detail = str(error)
        return _fail(
            "configuration_error",
            detail,
            human=detail,
            json_mode=json_mode,
            exit_code=EXIT_CONFIG,
        )
    except CredentialError as error:
        detail = str(error)
        return _fail(
            "credential_error",
            detail,
            human=detail,
            json_mode=json_mode,
            exit_code=EXIT_CREDENTIAL,
        )

    # Record a safe, typed failure category.  The kernel still reports POLICY_ERROR; these flags
    # let the exit code distinguish a rejected credential (2) from a genuine transport failure (1)
    # without matching exception or stop-reason strings.
    outcome = {"credential_rejected": False, "transport_error": False}

    def recording_request_fn(payload):
        try:
            return request_fn(payload)
        except CredentialError:
            outcome["credential_rejected"] = True
            raise
        except JevRequestError:
            outcome["transport_error"] = True
            raise

    # 2. evidence target for an explicit --evidence path, before anything is created: a bad path
    #    must cost nothing (no workspace, no seed, no request).
    if args.evidence is not None:
        intended_workspace = Path(args.workspace).expanduser() if args.workspace is not None else None
        try:
            _evidence_preflight(args.evidence, intended_workspace)
        except EvidenceError as error:
            return _fail(
                "evidence_error",
                EVIDENCE_TARGET_DETAIL,
                human=f"{error}; {EVIDENCE_TARGET_DETAIL}",
                json_mode=json_mode,
                exit_code=EXIT_CONFIG,
            )

    # 3. workspace: new or confirmed empty, and only after the configuration is known good.
    if args.workspace is None:
        workspace = Path(tempfile.mkdtemp(prefix="jev-live-files-"))
    else:
        try:
            workspace = prepare_workspace(args.workspace)
        except WorkspaceError as error:
            return _fail(
                "workspace_error",
                WORKSPACE_ERROR_DETAIL,
                human=str(error),
                json_mode=json_mode,
                exit_code=EXIT_CONFIG,
            )

    # 4. defensive re-check of the final target (also covers the default path inside the new
    #    workspace) before seeding.
    evidence_path = args.evidence or (workspace / "verification.json")
    try:
        evidence_path = _evidence_preflight(evidence_path, workspace)
    except EvidenceError as error:
        return _fail(
            "evidence_error",
            EVIDENCE_TARGET_DETAIL,
            human=f"{error}; {EVIDENCE_TARGET_DETAIL}",
            json_mode=json_mode,
            exit_code=EXIT_CONFIG,
        )

    try:
        seed_workspace(workspace)
    except WorkspaceError as error:
        return _fail(
            "workspace_error",
            WORKSPACE_ERROR_DETAIL,
            human=str(error),
            json_mode=json_mode,
            exit_code=EXIT_CONFIG,
        )

    environment = LiveFilesEnvironment(workspace)
    verifier = LiveFilesVerifier(workspace)
    policy = JevPolicy(request_fn=recording_request_fn, model=model)
    loop = Loop(
        task=TaskSpec(goal=GOAL, success_criteria=SUCCESS_CRITERIA),
        environment=environment,
        policy=policy,
        verifier=verifier,
        config=LoopConfig(max_steps=args.max_steps),
    )
    if not args.json:
        print("live Jev + real filesystem reference example (synthetic data)")
        print(f"workspace: {workspace}")
        print(f"endpoint:  {endpoint}  model={model}  timeout={seconds:g}s")
        print(f"credential: {DEFAULT_API_KEY_ENV} from the process environment (value never shown)")
        print()

    result = loop.run()

    verification = result.verifications[-1] if result.verifications else None
    failure_kind: str | None = None
    if result.status is not RunStatus.SUCCEEDED:
        if outcome["credential_rejected"]:
            failure_kind = "credential_rejected"
        elif outcome["transport_error"]:
            failure_kind = "transport_error"
        else:
            failure_kind = result.stop_reason.value if result.stop_reason else "unknown"
    summary: dict[str, Any] = {
        "example": "live-files",
        "mode": "live",
        "endpoint": endpoint,
        "model": model,
        "workspace": str(workspace),
        "goal": GOAL,
        "failure_kind": failure_kind,
        "status": result.status.value,
        "stop_reason": result.stop_reason.value if result.stop_reason else None,
        "detail": _run_finished_detail(loop),
        "steps": result.steps,
        "executions": result.executions,
        "decisions": result.decisions,
        "decision_requests": len(policy.requests),
        "usage": _usage_totals(policy.responses),
        "verification": None
        if verification is None
        else {
            "verdict": verification.verdict.value,
            "detail": verification.detail,
            "evidence": dict(verification.evidence or {}),
        },
        "verified": bool(verification is not None and verification.verdict.value == "satisfied"),
        "evidence_path": str(evidence_path),
        # This artifact is written by the call below; whenever the file exists, this field is true
        # in the artifact and in stdout.  A write failure flips it to false in the stdout report.
        "evidence_written": True,
        "cleanup": CLEANUP_NOTE,
    }
    try:
        _write_evidence(evidence_path, summary)
    except EvidenceError:
        summary["evidence_written"] = False
        summary["failure_kind"] = "evidence_write_failed"
        print(f"evidence error: {EVIDENCE_WRITE_DETAIL}", file=sys.stderr)
        if json_mode:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return EXIT_CONFIG

    if outcome["credential_rejected"]:
        print(
            "credential error: the Jev API rejected the credential (HTTP 401/403); "
            "the response body was not read.",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return _exit_code(result, outcome)

    for event in loop.timeline():
        if event.kind.value in TRACKED_EVENTS:
            detail = {key: value for key, value in event.data.items() if key not in {"payload", "probabilities"}}
            print(f"step {event.step:>2} {event.kind.value:<18} {detail}")

    print()
    print(f"status={result.status.value} stop_reason={result.stop_reason.value if result.stop_reason else None}")
    print(f"steps={result.steps} executions={result.executions} decisions={result.decisions}")
    if verification is not None:
        print(f"verifier={verification.verdict.value} detail={verification.detail!r}")
    print(f"decision_requests={len(policy.requests)} usage={summary['usage']}")
    print(f"evidence: {evidence_path}")
    print(f"inspect the workspace yourself: find {shlex.quote(str(workspace))} -type f")
    print(f"cleanup ({workspace}): {CLEANUP_NOTE}")
    return _exit_code(result, outcome)


def _exit_code(result, outcome: Mapping[str, bool]) -> int:
    """0 success, 2 when the credential was refused, 1 for every other genuine run failure."""
    if result.status is RunStatus.SUCCEEDED:
        return EXIT_OK
    if outcome.get("credential_rejected"):
        return EXIT_CREDENTIAL
    return EXIT_RUN_FAILED


if __name__ == "__main__":
    raise SystemExit(main())

"""Filesystem-backed host service and one-process-per-run worker."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .events import ManagedEventKind
from .runtime import CognitionBroker, CognitionError, RuntimeServices, load_controller
from .store import ManagedRecorder, atomic_write_json, load_events, load_status
from .types import ControllerResult, ManagedStatus, RunSpec

Json = Any
ACTIVE_STATUSES = {"starting", "running", "stopping"}
MAX_RPC_BYTES = 1_000_000
MAX_CONTROLLER_SNAPSHOT_BYTES = 100_000
MAX_CONTROLLER_OUTPUT_BYTES = 500_000


def default_home() -> Path:
    configured = os.environ.get("JEV_LOOP_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".local" / "state" / "jev-loop").resolve()


def dispatch(request: Mapping[str, Json], *, home: Path | None = None) -> dict[str, Json]:
    root = (home or default_home()).resolve()
    action = str(request.get("action") or "")
    if action == "start":
        return start_run(root, request)
    if action == "list":
        return list_runs(root, owner_id=_required_string(request, "owner_id"))
    if action == "inspect":
        return inspect_run(root, _required_string(request, "run_id"), _required_string(request, "owner_id"))
    if action == "events":
        return read_run_events(
            root,
            _required_string(request, "run_id"),
            _required_string(request, "owner_id"),
            after=int(request.get("after") or 0),
            limit=int(_bounded_float(request.get("limit", 100), 1, 500, "limit")),
        )
    if action == "release_resources":
        return confirm_resources_released(root, request)
    if action in {"heartbeat", "update", "stop", "respond"}:
        return send_command(root, request)
    raise ValueError(f"unknown host action {action!r}")


def start_run(home: Path, request: Mapping[str, Json]) -> dict[str, Json]:
    owner_id = _required_string(request, "owner_id")
    idempotency_key = _required_string(request, "idempotency_key")
    project_root = Path(_required_string(request, "project_root")).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError(f"project_root is not a directory: {project_root}")
    bundle = _validate_bundle(str(request.get("bundle") or ""), project_root)
    task = dict(request.get("task") or {})
    resource_keys = _validate_resource_keys(request.get("resource_keys") or ())
    goal = task.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("task.goal must be a non-empty string")

    detached = bool(request.get("detached", False))
    lease_seconds = None if detached else _bounded_float(request.get("lease_seconds", 30), 10, 300, "lease_seconds")
    max_runtime = _bounded_float(request.get("max_runtime_seconds", 300), 1, 86_400, "max_runtime_seconds")
    stop_grace = _bounded_float(request.get("stop_grace_seconds", 3), 0.1, 30, "stop_grace_seconds")
    bundle_config = dict(request.get("bundle_config") or {})
    _json_value(task, label="task", maximum=200_000)
    _json_value(bundle_config, label="bundle_config", maximum=100_000)

    _secure_dir(home)
    runs_dir = home / "runs"
    idempotency_dir = home / "idempotency"
    _secure_dir(runs_dir)
    _secure_dir(idempotency_dir)
    digest = hashlib.sha256(f"{owner_id}\0{project_root}\0{idempotency_key}".encode()).hexdigest()
    mapping_path = idempotency_dir / f"{digest}.json"

    run_id = "run_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ_") + secrets.token_hex(4)
    mapping_fd: int | None = None
    try:
        mapping_fd = os.open(mapping_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _wait_for_idempotent_start(home, request, mapping_path, owner_id)

    run_dir = runs_dir / run_id
    claims_acquired = False
    process: subprocess.Popen | None = None
    try:
        _acquire_resource_claims(home, run_id, owner_id, resource_keys)
        claims_acquired = True
        run_dir.mkdir(mode=0o700)
        for child in ("inbox", "acks", "artifacts"):
            (run_dir / child).mkdir(mode=0o700)
        spec = RunSpec(
            run_id=run_id,
            owner_id=owner_id,
            project_root=str(project_root),
            bundle=bundle,
            task=task,
            resource_keys=resource_keys,
            detached=detached,
            lease_seconds=lease_seconds,
            max_runtime_seconds=max_runtime,
            stop_grace_seconds=stop_grace,
            bundle_config=bundle_config,
        )
        atomic_write_json(run_dir / "spec.json", spec.to_json())
        recorder = ManagedRecorder(run_dir)
        recorder.emit(
            ManagedEventKind.RUN_CREATED,
            {
                "run_id": run_id,
                "owner_id": owner_id,
                "project_root": str(project_root),
                "bundle": bundle,
                "task": task,
                "resource_keys": list(resource_keys),
                "detached": detached,
                "lease_seconds": lease_seconds,
                "max_runtime_seconds": max_runtime,
                "stop_grace_seconds": stop_grace,
            },
        )
        assert mapping_fd is not None
        with os.fdopen(mapping_fd, "w", encoding="utf-8") as mapping_handle:
            mapping_fd = None
            mapping_handle.write(json.dumps({"run_id": run_id, "created_at": time.time()}) + "\n")
            mapping_handle.flush()
            os.fsync(mapping_handle.fileno())

        log_handle = (run_dir / "worker.log").open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "jev_loop.host", "worker", "--run-dir", str(run_dir)],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=dict(os.environ),
            )
        finally:
            log_handle.close()

        deadline = time.monotonic() + 5.0
        status = load_status(run_dir)
        while time.monotonic() < deadline:
            status = load_status(run_dir)
            if status["status"] != "starting" or process.poll() is not None:
                break
            time.sleep(0.025)
        if status["status"] == "starting" and process.poll() is not None:
            recorder = ManagedRecorder.open(run_dir)
            recorder.emit(
                ManagedEventKind.ERROR,
                {"error": f"worker exited during startup with code {process.returncode}"},
            )
            recorder.emit(ManagedEventKind.RESOURCES_RELEASED, {"reason": "worker_never_started"})
            recorder.emit(
                ManagedEventKind.RUN_FINISHED,
                {"status": ManagedStatus.FAILED.value, "reason": "worker_start_failed"},
            )
            status = load_status(run_dir)
            _release_resource_claims(home, run_id, resource_keys)
        return {"ok": True, "reused": False, "run": status}
    except Exception as error:
        if mapping_fd is not None:
            os.close(mapping_fd)
        worker_may_be_running = process is not None and process.poll() is None
        if not worker_may_be_running:
            mapping_path.unlink(missing_ok=True)
            if (run_dir / "events.jsonl").is_file():
                try:
                    failed = ManagedRecorder.open(run_dir)
                    if not failed.state.resources_released:
                        failed.emit(ManagedEventKind.RESOURCES_RELEASED, {"reason": "start_failed"})
                    if not failed.state.terminal:
                        failed.emit(
                            ManagedEventKind.ERROR,
                            {"error": f"host start failed: {error!r}"},
                        )
                        failed.emit(
                            ManagedEventKind.RUN_FINISHED,
                            {"status": ManagedStatus.FAILED.value, "reason": "host_start_failed"},
                        )
                except Exception:
                    pass
            if claims_acquired:
                _release_resource_claims(home, run_id, resource_keys)
        raise


def inspect_run(home: Path, run_id: str, owner_id: str) -> dict[str, Json]:
    status = _owned_status(home, run_id, owner_id)
    startup_grace = status["status"] == "starting" and time.time() - float(status.get("created_at") or 0) < 30
    if status["status"] in ACTIVE_STATUSES and not status.get("worker_alive") and not startup_grace:
        recorder = ManagedRecorder.open(_run_dir(home, run_id))
        if not recorder.state.terminal:
            recorder.emit(ManagedEventKind.ERROR, {"error": "managed worker is no longer alive"})
            if not recorder.state.terminal:  # another inspector may have reconciled while we waited for the lock
                recorder.emit(
                    ManagedEventKind.RUN_FINISHED,
                    {"status": ManagedStatus.FAILED.value, "reason": "worker_exited"},
                )
        status = load_status(_run_dir(home, run_id))
        if status.get("resources_released") is True:
            _release_resource_claims(home, run_id, tuple(status.get("resource_keys") or ()))
    return {"ok": True, "run": status}


def confirm_resources_released(home: Path, request: Mapping[str, Json]) -> dict[str, Json]:
    run_id = _required_string(request, "run_id")
    owner_id = _required_string(request, "owner_id")
    if request.get("confirmed") is not True:
        raise ValueError("release_resources requires confirmed=true after external release was verified")
    status = _owned_status(home, run_id, owner_id)
    if status.get("status") in ACTIVE_STATUSES:
        raise ValueError("cannot clear resource claims while the run is active")
    recorder = ManagedRecorder.open(_run_dir(home, run_id))
    if not recorder.state.resources_released:
        recorder.emit(ManagedEventKind.RESOURCES_RELEASED, {"reason": "operator_verified_release"})
    _release_resource_claims(home, run_id, recorder.state.resource_keys)
    return {"ok": True, "run": load_status(_run_dir(home, run_id))}


def list_runs(home: Path, *, owner_id: str) -> dict[str, Json]:
    runs: list[dict[str, Json]] = []
    runs_dir = home / "runs"
    if runs_dir.is_dir():
        for run_dir in sorted(runs_dir.iterdir(), reverse=True):
            if not run_dir.is_dir() or not (run_dir / "status.json").is_file():
                continue
            try:
                status = load_status(run_dir)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if status.get("owner_id") == owner_id:
                if status.get("status") in ACTIVE_STATUSES and not status.get("worker_alive"):
                    status = inspect_run(home, str(status["run_id"]), owner_id)["run"]
                runs.append(_list_projection(status))
    return {"ok": True, "runs": runs}


def _list_projection(status: Mapping[str, Json]) -> dict[str, Json]:
    keys = (
        "run_id",
        "owner_id",
        "project_root",
        "bundle",
        "task",
        "resource_keys",
        "detached",
        "status",
        "stop_reason",
        "resources_released",
        "config_version",
        "event_seq",
        "worker_alive",
        "lease_deadline",
        "controller",
        "pending_cognition",
        "last_error",
    )
    return {key: status.get(key) for key in keys}


def read_run_events(
    home: Path,
    run_id: str,
    owner_id: str,
    *,
    after: int = 0,
    limit: int = 100,
) -> dict[str, Json]:
    _owned_status(home, run_id, owner_id)
    events = load_events(_run_dir(home, run_id) / "events.jsonl")
    available = [event for event in events if event.seq > after]
    selected = available[:limit]
    return {
        "ok": True,
        "events": [event.to_json() for event in selected],
        "next_seq": selected[-1].seq if selected else after,
        "journal_last_seq": events[-1].seq if events else 0,
        "has_more": len(available) > len(selected),
    }


def send_command(home: Path, request: Mapping[str, Json]) -> dict[str, Json]:
    run_id = _required_string(request, "run_id")
    owner_id = _required_string(request, "owner_id")
    run_dir = _run_dir(home, run_id)
    status = _owned_status(home, run_id, owner_id)
    action = str(request["action"])
    if status["status"] not in ACTIVE_STATUSES:
        if action == "stop":
            return {"ok": True, "accepted": False, "confirmed": True, "run": status}
        raise ValueError(f"run {run_id} is already {status['status']}")
    if status["status"] == "stopping" and action not in {"heartbeat", "stop"}:
        raise ValueError(f"run {run_id} is stopping; {action} is no longer accepted")
    if not status.get("worker_alive"):
        return inspect_run(home, run_id, owner_id)

    command_id = secrets.token_hex(12)
    command: dict[str, Json] = {
        "command_id": command_id,
        "action": action,
        "owner_id": owner_id,
        "sent_at": time.time(),
    }
    if action == "update":
        command["expected_config_version"] = int(request.get("expected_config_version") or 0)
        command["task_patch"] = dict(request.get("task_patch") or {})
    elif action == "respond":
        command["job_id"] = _required_string(request, "job_id")
        command["expected_job_version"] = int(request.get("expected_job_version") or 0)
        command["result"] = request.get("result")
        command["evidence"] = dict(request.get("evidence") or {})
    elif action == "stop":
        command["reason"] = str(request.get("reason") or "host_stop")

    command_path = run_dir / "inbox" / f"{time.time_ns()}-{command_id}.json"
    atomic_write_json(command_path, command)
    ack_path = run_dir / "acks" / f"{command_id}.json"
    deadline = time.monotonic() + _bounded_float(request.get("timeout_seconds", 5), 0.1, 30, "timeout_seconds")
    while time.monotonic() < deadline:
        if ack_path.is_file():
            ack = json.loads(ack_path.read_text(encoding="utf-8"))
            ack_path.unlink(missing_ok=True)
            if not ack.get("ok"):
                raise ValueError(str(ack.get("error") or "command rejected"))
            if action == "stop":
                wait_deadline = time.monotonic() + float(request.get("confirm_seconds") or 5.0)
                latest = load_status(run_dir)
                while time.monotonic() < wait_deadline and latest["status"] in ACTIVE_STATUSES:
                    time.sleep(0.025)
                    latest = load_status(run_dir)
                return {
                    "ok": True,
                    "accepted": True,
                    "confirmed": latest["status"] not in ACTIVE_STATUSES,
                    "run": latest,
                }
            return {"ok": True, **dict(ack.get("data") or {}), "run": load_status(run_dir)}
        time.sleep(0.025)
    raise TimeoutError(f"worker did not acknowledge {action!r} within the command timeout")


def run_worker(run_dir: Path) -> int:
    run_dir = run_dir.resolve()
    spec = RunSpec.from_json(json.loads((run_dir / "spec.json").read_text(encoding="utf-8")))
    recorder = ManagedRecorder.open(run_dir)
    if recorder.state.status is not ManagedStatus.STARTING:
        return 2  # managed runs are never implicitly replayed or reactivated
    recorder.emit(ManagedEventKind.WORKER_STARTED, {"pid": os.getpid()})
    changed = threading.Event()
    cognition = CognitionBroker(recorder, changed)
    services = RuntimeServices(recorder=recorder, cognition=cognition, changed=changed)
    stop_event = threading.Event()
    release_confirmed = threading.Event()
    result_queue: queue.Queue[ControllerResult] = queue.Queue(maxsize=1)
    controller = None
    requested_result: ControllerResult | None = None
    stop_requested_at: float | None = None

    def ask_to_stop(result: ControllerResult) -> None:
        nonlocal requested_result, stop_requested_at
        if requested_result is not None or recorder.state.terminal:
            return
        requested_result = result
        stop_requested_at = time.monotonic()
        recorder.emit(ManagedEventKind.STOP_REQUESTED, {"reason": result.reason})
        try:
            if controller is not None:
                controller.request_stop(result.reason or result.status.value)
                release_confirmed.set()
        except BaseException as error:
            requested_result = ControllerResult(
                ManagedStatus.FAILED,
                "stop_release_failed",
                {"requested_reason": result.reason, "error": repr(error)},
                resources_released=False,
            )
            recorder.emit(
                ManagedEventKind.ERROR,
                {"error": f"controller request_stop failed: {error!r}"},
            )
        finally:
            stop_event.set()
            changed.set()

    def on_signal(signum, _frame) -> None:
        ask_to_stop(ControllerResult(ManagedStatus.CANCELLED, f"signal_{signum}"))

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        controller = load_controller(spec, services)
    except Exception as error:
        recorder.emit(ManagedEventKind.ERROR, {"error": repr(error), "traceback": traceback.format_exc()})
        recorder.emit(ManagedEventKind.RESOURCES_RELEASED, {"reason": "controller_never_started"})
        recorder.emit(
            ManagedEventKind.RUN_FINISHED,
            {"status": ManagedStatus.FAILED.value, "reason": "bundle_load_failed"},
        )
        _release_resource_claims(run_dir.parent.parent, spec.run_id, spec.resource_keys)
        return 1

    def engine() -> None:
        try:
            result = controller.run(stop_event)
            if not isinstance(result, ControllerResult):
                raise TypeError(f"controller.run returned {type(result).__name__}, expected ControllerResult")
            if result.resources_released:
                release_confirmed.set()
            result_queue.put(result)
        except BaseException as error:  # keep the coordinator alive long enough to record and release
            try:
                controller.request_stop("controller_error")
                release_confirmed.set()
            finally:
                recorder.emit(
                    ManagedEventKind.ERROR,
                    {"error": repr(error), "traceback": traceback.format_exc()},
                )
                result_queue.put(
                    ControllerResult(
                        ManagedStatus.FAILED,
                        "controller_error",
                        resources_released=release_confirmed.is_set(),
                    )
                )

    thread = threading.Thread(target=engine, name=f"jev-loop-{spec.run_id}", daemon=True)
    thread.start()
    last_snapshot = ""
    last_snapshot_at = 0.0

    try:
        while True:
            now = time.time()
            monotonic_now = time.monotonic()
            _process_commands(run_dir, spec, recorder, cognition, controller, ask_to_stop)
            cognition.expire_due(now=now)

            state = recorder.state
            if not state.detached and state.lease_deadline is not None and now > state.lease_deadline:
                ask_to_stop(ControllerResult(ManagedStatus.EXPIRED, "lease_expired"))
            if now - state.created_at > state.max_runtime_seconds:
                ask_to_stop(ControllerResult(ManagedStatus.FAILED, "max_runtime_exceeded"))

            if monotonic_now - last_snapshot_at >= 0.1:
                snapshot, encoded = _safe_controller_snapshot(controller)
                if encoded != last_snapshot:
                    if "snapshot_error" in snapshot:
                        recorder.emit(ManagedEventKind.ERROR, {"error": snapshot["snapshot_error"]})
                        ask_to_stop(ControllerResult(ManagedStatus.FAILED, "snapshot_failed"))
                    recorder.emit(ManagedEventKind.CONTROLLER_SNAPSHOT, {"snapshot": snapshot})
                    last_snapshot = encoded
                last_snapshot_at = monotonic_now

            try:
                engine_result = result_queue.get_nowait()
            except queue.Empty:
                engine_result = None
            if engine_result is not None:
                final = requested_result or engine_result
                cognition.cancel_pending(final.reason or final.status.value)
                if release_confirmed.is_set() and not recorder.state.resources_released:
                    recorder.emit(ManagedEventKind.RESOURCES_RELEASED, {"reason": final.reason or final.status.value})
                snapshot, _ = _safe_controller_snapshot(controller)
                recorder.emit(ManagedEventKind.CONTROLLER_SNAPSHOT, {"snapshot": snapshot})
                try:
                    output = _json_value(
                        final.output,
                        label="controller output",
                        maximum=MAX_CONTROLLER_OUTPUT_BYTES,
                    )
                except ValueError as error:
                    recorder.emit(ManagedEventKind.ERROR, {"error": str(error)})
                    final = ControllerResult(ManagedStatus.FAILED, "invalid_controller_output")
                    output = {"error": str(error)}
                recorder.emit(
                    ManagedEventKind.RUN_FINISHED,
                    {"status": final.status.value, "reason": final.reason, "output": output},
                )
                return 0 if final.status is ManagedStatus.SUCCEEDED else 1

            if stop_requested_at is not None and monotonic_now - stop_requested_at > state.stop_grace_seconds:
                cognition.cancel_pending(requested_result.reason if requested_result else "stop_timeout")
                if release_confirmed.is_set() and not recorder.state.resources_released:
                    recorder.emit(
                        ManagedEventKind.RESOURCES_RELEASED,
                        {"reason": requested_result.reason if requested_result else "stop_timeout"},
                    )
                snapshot, _ = _safe_controller_snapshot(controller)
                recorder.emit(ManagedEventKind.CONTROLLER_SNAPSHOT, {"snapshot": snapshot})
                final = requested_result or ControllerResult(ManagedStatus.FAILED, "stop_timeout")
                recorder.emit(
                    ManagedEventKind.ERROR,
                    {"error": "controller did not exit before stop grace elapsed"},
                )
                recorder.emit(
                    ManagedEventKind.RUN_FINISHED,
                    {"status": final.status.value, "reason": final.reason, "output": {"forced": True}},
                )
                return 1
            time.sleep(0.025)
    finally:
        if not recorder.state.terminal:
            try:
                controller.request_stop("worker_exit")
                release_confirmed.set()
            except BaseException as error:
                try:
                    recorder.emit(
                        ManagedEventKind.ERROR,
                        {"error": f"controller cleanup failed: {error!r}"},
                    )
                except Exception:
                    pass
            finally:
                stop_event.set()
        if release_confirmed.is_set() and not recorder.state.resources_released:
            try:
                recorder.emit(ManagedEventKind.RESOURCES_RELEASED, {"reason": "worker_exit"})
            except Exception:
                pass
        if recorder.state.resources_released:
            _release_resource_claims(run_dir.parent.parent, spec.run_id, spec.resource_keys)


def _process_commands(run_dir, spec, recorder, cognition, controller, ask_to_stop) -> None:
    for command_path in sorted((run_dir / "inbox").glob("*.json")):
        command_id = command_path.stem.split("-")[-1]
        ack_path = run_dir / "acks" / f"{command_id}.json"
        try:
            command = json.loads(command_path.read_text(encoding="utf-8"))
            if command.get("owner_id") != spec.owner_id:
                raise ValueError("command owner does not own this run")
            action = str(command.get("action") or "")
            if recorder.state.status is ManagedStatus.STOPPING and action not in {"heartbeat", "stop"}:
                raise ValueError(f"run is stopping; {action} is no longer accepted")
            data: dict[str, Json] = {}
            if action == "heartbeat":
                recorder.emit(ManagedEventKind.HEARTBEAT, {"owner_id": spec.owner_id})
                data["lease_deadline"] = recorder.state.lease_deadline
            elif action == "update":
                expected = int(command.get("expected_config_version") or 0)
                if expected != recorder.state.config_version:
                    current_version = recorder.state.config_version
                    raise ValueError(
                        f"config version moved from {expected} to {current_version}; inspect before updating"
                    )
                patch = dict(command.get("task_patch") or {})
                _validate_task_patch(patch)
                next_task = {**recorder.state.task, **patch}
                controller.update(next_task, patch)  # validate/accept before committing the event
                recorder.emit(ManagedEventKind.TASK_UPDATED, {"task": patch})
                data["config_version"] = recorder.state.config_version
            elif action == "respond":
                completed = cognition.complete(
                    str(command.get("job_id") or ""),
                    expected_version=int(command.get("expected_job_version") or 0),
                    result=command.get("result"),
                    evidence=dict(command.get("evidence") or {}),
                )
                data["job"] = completed.to_json()
            elif action == "stop":
                reason = str(command.get("reason") or "host_stop")
                ask_to_stop(ControllerResult(ManagedStatus.CANCELLED, reason))
                data["accepted"] = True
            else:
                raise ValueError(f"unknown worker command {action!r}")
            atomic_write_json(ack_path, {"ok": True, "data": data})
        except (OSError, ValueError, CognitionError, json.JSONDecodeError) as error:
            atomic_write_json(ack_path, {"ok": False, "error": str(error)})
        finally:
            command_path.unlink(missing_ok=True)


def _json_value(value: Json, *, label: str, maximum: int) -> Json:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not JSON-serializable: {error}") from error
    if len(encoded) > maximum:
        raise ValueError(f"{label} is {len(encoded)} bytes, over limit {maximum}")
    return value


def _safe_controller_snapshot(controller) -> tuple[dict[str, Json], str]:
    try:
        snapshot = dict(controller.snapshot())
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode()) > MAX_CONTROLLER_SNAPSHOT_BYTES:
            raise ValueError(f"snapshot exceeds {MAX_CONTROLLER_SNAPSHOT_BYTES} bytes")
        return snapshot, encoded
    except BaseException as error:
        message = f"controller snapshot failed: {error!r}"
        snapshot = {"snapshot_error": message}
        return snapshot, json.dumps(snapshot, sort_keys=True)


def _wait_for_idempotent_start(
    home: Path,
    request: Mapping[str, Json],
    mapping_path: Path,
    owner_id: str,
) -> dict[str, Json]:
    deadline = time.monotonic() + 5.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if not mapping_path.exists():
            return start_run(home, request)
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            existing = str(mapping["run_id"])
            status_path = home / "runs" / existing / "status.json"
            if status_path.is_file():
                status = inspect_run(home, existing, owner_id)["run"]
                return {"ok": True, "reused": True, "run": status}
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.025)
    raise TimeoutError(f"idempotent start did not become readable: {last_error or mapping_path}")


def _validate_bundle(bundle: str, project_root: Path) -> str:
    if bundle == "diagnostic":
        return bundle
    if not bundle:
        raise ValueError("bundle is required (use 'diagnostic' only for the built-in contract probe)")
    path = Path(bundle)
    if not path.is_absolute():
        path = project_root / path
    path = path.resolve()
    if not path.is_relative_to(project_root):
        raise ValueError("bundle manifest must be inside project_root")
    if not path.is_file():
        raise ValueError(f"bundle manifest does not exist: {path}")
    return str(path)


def _validate_resource_keys(raw: Json) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("resource_keys must be an array of strings")
    keys: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("resource keys must be non-empty strings")
        key = item.strip()
        if len(key) > 200 or "\0" in key:
            raise ValueError("resource keys must be at most 200 characters and contain no NUL")
        if key not in keys:
            keys.append(key)
    return tuple(keys)


@contextmanager
def _claim_lock(home: Path) -> Iterator[None]:
    claims_dir = home / "resource-claims"
    _secure_dir(claims_dir)
    with (claims_dir / ".lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _claim_path(home: Path, resource_key: str) -> Path:
    digest = hashlib.sha256(resource_key.encode()).hexdigest()
    return home / "resource-claims" / f"{digest}.json"


def _acquire_resource_claims(home: Path, run_id: str, owner_id: str, resource_keys: tuple[str, ...]) -> None:
    if not resource_keys:
        return
    with _claim_lock(home):
        for key in resource_keys:
            path = _claim_path(home, key)
            if not path.is_file():
                continue
            try:
                claim = json.loads(path.read_text(encoding="utf-8"))
                claimed_run = str(claim["run_id"])
                claimed_dir = home / "runs" / claimed_run
                status = load_status(claimed_dir) if (claimed_dir / "status.json").is_file() else None
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                status = None
                claimed_run = "unknown"
            if status is None:
                raise ValueError(f"resource {key!r} has an unreadable quarantined claim from run {claimed_run}")
            if status.get("resources_released") is not True:
                suffix = "active" if status.get("status") in ACTIVE_STATUSES else "quarantined"
                raise ValueError(f"resource {key!r} is {suffix} under run {claimed_run}")
            path.unlink(missing_ok=True)
        for key in resource_keys:
            atomic_write_json(
                _claim_path(home, key),
                {"resource_key": key, "run_id": run_id, "owner_id": owner_id, "claimed_at": time.time()},
            )


def _release_resource_claims(home: Path, run_id: str, resource_keys: tuple[str, ...]) -> None:
    if not resource_keys:
        return
    with _claim_lock(home):
        for key in resource_keys:
            path = _claim_path(home, key)
            try:
                claim = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if claim.get("run_id") == run_id:
                path.unlink(missing_ok=True)


def _validate_task_patch(patch: Mapping[str, Json]) -> None:
    allowed = {"goal", "inputs", "constraints", "success_criteria", "authorization"}
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"unknown task update fields: {sorted(unknown)}")
    if "goal" in patch and (not isinstance(patch["goal"], str) or not str(patch["goal"]).strip()):
        raise ValueError("updated goal must be a non-empty string")


def _owned_status(home: Path, run_id: str, owner_id: str) -> dict[str, Json]:
    status = load_status(_run_dir(home, run_id))
    if status.get("owner_id") != owner_id:
        raise ValueError(f"run {run_id!r} is not owned by this agent session")
    return status


def _run_dir(home: Path, run_id: str) -> Path:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
    if not run_id.startswith("run_") or any(character not in allowed for character in run_id):
        raise ValueError("invalid run_id")
    path = home / "runs" / run_id
    if not path.is_dir():
        raise ValueError(f"unknown run {run_id!r}")
    return path


def _required_string(request: Mapping[str, Json], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _bounded_float(value: Json, minimum: float, maximum: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a number") from error
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass

"""Durable managed-run journal and atomic status projection."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .events import ManagedEvent, ManagedEventKind
from .reduce import apply, replay
from .types import ManagedState

Json = Any


def atomic_write_json(path: Path, payload: Mapping[str, Json]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class ManagedRecorder:
    """Serialize event appends, reduce them, then refresh the status cache.

    ``events.jsonl`` is authoritative.  ``status.json`` is an atomic projection used by
    clients so inspection never has to race a partially written JSONL record.
    """

    def __init__(self, run_dir: Path, state: ManagedState | None = None) -> None:
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.status_path = run_dir / "status.json"
        self.lock_path = run_dir / ".journal.lock"
        self._lock = threading.RLock()
        self._state = state
        self._known_size = self.events_path.stat().st_size if self.events_path.exists() else 0

    @classmethod
    def open(cls, run_dir: Path) -> ManagedRecorder:
        events = load_events(run_dir / "events.jsonl")
        return cls(run_dir, replay(events))

    @property
    def state(self) -> ManagedState:
        with self._lock:
            if self._state is None:
                raise ValueError("recorder has no state")
            return self._state

    def emit(
        self,
        kind: ManagedEventKind,
        data: Mapping[str, Json] | None = None,
        *,
        at: float | None = None,
    ) -> ManagedEvent:
        with self._lock:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._synchronize_from_disk()
                    seq = 1 if self._state is None else self._state.event_seq + 1
                    event = ManagedEvent(
                        seq=seq,
                        at=time.time() if at is None else at,
                        kind=kind,
                        data=dict(data or {}),
                    )
                    next_state = apply(self._state, event)
                    with self.events_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(event.to_json(), ensure_ascii=False, sort_keys=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._known_size = self.events_path.stat().st_size
                    self._state = next_state
                    atomic_write_json(self.status_path, next_state.to_json(worker_alive=_pid_alive(next_state.pid)))
                    return event
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def refresh_status(self) -> None:
        with self._lock:
            with self.lock_path.open("a+") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._synchronize_from_disk()
                    state = self.state
                    atomic_write_json(self.status_path, state.to_json(worker_alive=_pid_alive(state.pid)))
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _synchronize_from_disk(self) -> None:
        size = self.events_path.stat().st_size if self.events_path.exists() else 0
        if size == self._known_size:
            return
        events = load_events(self.events_path)
        self._state = replay(events) if events else None
        self._known_size = size


def load_events(path: Path) -> tuple[ManagedEvent, ...]:
    if not path.exists():
        return ()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    events: list[ManagedEvent] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            events.append(ManagedEvent.from_json(json.loads(line)))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            # A process kill may leave only the final append truncated. Earlier corruption is
            # never hidden, and a complete final line remains strict.
            is_truncated_tail = line_number == len(lines) and not line.endswith("\n")
            if is_truncated_tail:
                break
            raise ValueError(f"invalid managed event at {path}:{line_number}: {error}") from error
    return tuple(events)


def load_status(run_dir: Path) -> dict[str, Json]:
    path = run_dir / "status.json"
    if not path.is_file():
        recorder = ManagedRecorder.open(run_dir)
        recorder.refresh_status()
    raw = json.loads(path.read_text(encoding="utf-8"))
    pid = raw.get("pid")
    raw["worker_alive"] = _pid_alive(int(pid)) if isinstance(pid, int) else False
    return raw


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            fields = proc_stat.read_text(encoding="utf-8").split()
            return len(fields) < 3 or fields[2] != "Z"
        except OSError:
            return True
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    state = result.stdout.strip()
    return result.returncode == 0 and bool(state) and not state.startswith("Z")

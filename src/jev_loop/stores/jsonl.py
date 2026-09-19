"""Event stores. The event log is the run's source of truth; state is derived from it."""

from __future__ import annotations

import json
from pathlib import Path

from ..core.events import Event


class MemoryEventStore:
    """In-process store, used by tests and short-lived runs."""

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, event: Event) -> None:
        self._events.append(event)

    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)


class JsonlEventStore:
    """Append-only JSONL store. One line per event, flushed immediately."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("")

    def append(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")

    def events(self) -> tuple[Event, ...]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return tuple(Event.from_json(json.loads(line)) for line in lines if line.strip())
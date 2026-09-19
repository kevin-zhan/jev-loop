"""Deterministic policies for tests and replay.

A scripted policy is how the loop is exercised without spending a model call, and it is
also how the deterministic-policy failure modes (fixed point, limit cycle) are reproduced
on purpose: given the same frame it always answers the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.types import Control, Decision, Frame


@dataclass
class ScriptedPolicy:
    """Prefer the first candidate whose key contains one of ``prefer``, in order."""

    prefer: tuple[str, ...] = ()
    fallback: str = "first"  # first | request_finish | blocked | wait | refresh | raise
    confidence: float | None = 0.9
    frames: list[Frame] = field(default_factory=list)

    def decide(self, frame: Frame) -> Decision:
        self.frames.append(frame)
        for pattern in self.prefer:
            for candidate in frame.candidates:
                if pattern in candidate.key:
                    return Decision(frame_id=frame.frame_id, candidate_id=candidate.id, confidence=self.confidence)
        if self.fallback == "first" and frame.candidates:
            return Decision(frame_id=frame.frame_id, candidate_id=frame.candidates[0].id, confidence=self.confidence)
        if self.fallback in {"request_finish", "blocked", "wait", "refresh"}:
            control = Control(self.fallback)
            if control in frame.controls:
                return Decision(frame_id=frame.frame_id, control=control, confidence=self.confidence)
        if self.fallback == "raise":
            raise RuntimeError("scripted policy asked to fail")
        return Decision(frame_id=frame.frame_id, candidate_id=None, control=Control.BLOCKED, confidence=self.confidence)


class RecordingPolicy:
    """Wrap another policy and keep every frame/decision pair for assertions."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.seen: list[tuple[Frame, Decision]] = []

    def decide(self, frame: Frame) -> Decision:
        decision = self.inner.decide(frame)
        self.seen.append((frame, decision))
        return decision
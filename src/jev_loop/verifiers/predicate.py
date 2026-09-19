"""Verifiers. Code-owned, independent of the model's DONE.

``REQUEST_FINISH`` from the policy is a request to verify, never a completion. Only a
SATISFIED verdict ends the run successfully; UNKNOWN stays UNKNOWN and suspends instead of
being rounded to success or failure.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.types import RuntimeState, VerifierResult, VerifyVerdict

Json = Any


@dataclass
class PredicateVerifier:
    """Wrap a deterministic check: True, False, or None (unknown)."""

    check: Callable[[RuntimeState], bool | None]
    detail: str = ""
    evidence: Mapping[str, Json] = field(default_factory=dict)

    def verify(self, state: RuntimeState) -> VerifierResult:
        verdict_value = self.check(state)
        if verdict_value is None:
            return VerifierResult(
                VerifyVerdict.UNKNOWN, detail=self.detail or "no evidence yet", evidence=dict(self.evidence)
            )
        if verdict_value:
            return VerifierResult(VerifyVerdict.SATISFIED, detail=self.detail, evidence=dict(self.evidence))
        return VerifierResult(VerifyVerdict.UNSATISFIED, detail=self.detail, evidence=dict(self.evidence))


@dataclass
class RejectingVerifier:
    """Always unknown: proves that the runtime does not treat DONE as success."""

    reason: str = "no verifier configured"

    def verify(self, state: RuntimeState) -> VerifierResult:
        return VerifierResult(VerifyVerdict.UNKNOWN, detail=self.reason)
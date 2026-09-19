"""Progress guards: the part that decides whether the loop is actually moving.

Jev is self-consistent: the same frame produces the same answer. So any repeated frame is
either a fixed point (nothing visible changed) or a limit cycle (visible but reversible
changes). The model cannot notice either one. The runtime therefore needs code-owned
detectors, and it must *change the frame* when it detects stagnation — otherwise a
deterministic policy keeps re-selecting the same action until the step budget is gone.

Three detectors, in increasing subtlety:

``NO_PROGRESS``  a write completed but the observation revision did not change.
``REPEAT``       the same action was already attempted on the same observation.
``CYCLE``        the same frame content was built again (state A -> B -> A ...).

All three produce *dead keys*: semantic action keys excluded from ``offer`` for as long as
the observation revision does not change. That exclusion is what makes the next frame
different, which is what lets a deterministic policy escape.
"""

from __future__ import annotations

from .types import GuardKind, GuardOutcome, LoopConfig, ReceiptStatus, RuntimeState

_ORDER = {GuardKind.NONE: 0, GuardKind.NO_PROGRESS: 1, GuardKind.REPEAT: 2, GuardKind.CYCLE: 3}


def _worst(*outcomes: GuardOutcome) -> GuardOutcome:
    best = GuardOutcome()
    for outcome in outcomes:
        if _ORDER[outcome.kind] > _ORDER[best.kind]:
            best = outcome
    return best


def assess_frame(state: RuntimeState, fingerprint: str, config: LoopConfig) -> GuardOutcome:
    """Detect a repeated frame *before* spending a model call on it.

    The current frame has already been counted by ``FRAME_BUILT``, so this asks whether the
    frame content has now been seen ``max_frame_visits`` times in total.
    """
    visits = state.frame_visits.get(fingerprint, 0)
    if visits >= config.max_frame_visits:
        return GuardOutcome(
            kind=GuardKind.CYCLE,
            detail=f"frame content revisited ({visits}x); a deterministic policy would repeat itself",
            escalate=True,
        )
    return GuardOutcome()


def assess_effect(
    state: RuntimeState,
    *,
    key: str,
    status: ReceiptStatus,
    changed: bool,
    effect_mutates: bool,
) -> GuardOutcome:
    """A completed write that left the observation unchanged is a no-effect action."""
    if status is ReceiptStatus.COMPLETED and effect_mutates and not changed:
        return GuardOutcome(
            kind=GuardKind.NO_PROGRESS,
            detail=f"{key!r} completed but the observation did not change",
            dead_keys=frozenset({key}),
            escalate=True,
        )
    return GuardOutcome()


def assess_attempts(state: RuntimeState, *, observation: str, key: str, config: LoopConfig) -> GuardOutcome:
    """Same action on the same observation, twice, is a fixed point in the making."""
    seen = sum(1 for attempt in state.attempts if attempt.observation == observation and attempt.key == key)
    if seen >= 1:
        return GuardOutcome(
            kind=GuardKind.REPEAT,
            detail=f"{key!r} already attempted on observation {observation}",
            dead_keys=frozenset({key}),
            escalate=True,
        )
    return GuardOutcome()


def escalate(dead_keys: frozenset[str]) -> GuardOutcome:
    """Guard response used when the loop chooses to continue with a narrowed candidate set."""
    return GuardOutcome(
        kind=GuardKind.NO_PROGRESS, detail="escalated: candidate excluded", dead_keys=dead_keys, escalate=True
    )


def combine(outcomes: tuple[GuardOutcome, ...]) -> GuardOutcome:
    result = _worst(*outcomes)
    if result.kind is GuardKind.NONE:
        return result
    dead = frozenset().union(*(outcome.dead_keys for outcome in outcomes))
    escalate_flag = any(outcome.escalate for outcome in outcomes)
    return GuardOutcome(kind=result.kind, detail=result.detail, dead_keys=dead, escalate=escalate_flag)
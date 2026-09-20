# Design: kernel invariants

**English** | [简体中文](design.zh-CN.md)

The kernel guarantees exactly three things: **only a validated choice is executed**, **state is derived
from real events only**, and **completion is declared only when the evidence satisfies it**. Making the
loop actually live (rather than merely stop) requires a stronger set of invariants.

## Why guards are needed

For a deterministic policy — the same observation and candidate set always produce the same answer,
with no sampling randomness to escape through — the following holds:

> As soon as an (observation, candidate set) pair occurs again, the same answer necessarily occurs
> again: either a fixed point or a limit cycle. The model will never notice on its own that it is
> spinning.

So the driving force of the loop can only come from code: **every step either changes the frame or
stops**. The kernel implements three escapes:

1. **Narrow the candidate set**: remove actions that had no effect or were already attempted
   (`dead_actions`). The frame changes, which forces the deterministic policy to choose differently.
2. **Suspend**: when there is nothing left to narrow (the frame reappeared but no action can be
   excluded), suspend with a wake condition instead of asking the model again.
3. **Name the failure**: after repeated escalations (`max_guard_escalations`), end with
   `cycle_detected` / `no_progress`.

| Guard | Trigger | Handling |
|---|---|---|
| `NO_PROGRESS` | a write action `completed`, but the observation revision did not change | mark the action as having no effect under that observation and remove it from the candidate set |
| `REPEAT` | the same action under the same observation was already attempted | block execution and add it to the dead keys |
| `CYCLE` | the frame content (observation + candidates + exclusion set) reappears | nothing left to narrow → suspend and wait for new information; keep escalating → fail with `cycle_detected` |

Dead keys are scoped to the **current observation**: once the observation changes, the exclusion set is
cleared. That lets a deterministic policy escape a repeat without permanently blinding it after one
no-effect action.

## Invariants

| # | Invariant | Symptom when violated | Enforced by |
|---|---|---|---|
| I1 | Every step starts from a fresh observation; a failed read keeps the old revision and marks it stale | deciding on a stale world | `OBSERVED` + `observation_stale` |
| I2 | Re-validation before execution: frame membership, observation revision, not-yet-excluded, authorization | executing an unauthorized or nonexistent action | `_validate_candidate` / `default_validate` |
| I3 | Actions with side effects write intent before executing; receipts enter the event stream in four states | after a crash, it is impossible to tell whether it happened | `EXECUTION_INTENT` → `EXECUTION_RECEIPT` |
| I4 | Pending/unknown may only be queried, never resent; `idempotency=NONE` is never retried blindly | duplicated side effects | `unresolved` + `build_frame` filtering |
| I5 | `dead_actions` are scoped to the current observation and cleared when it changes | one no-effect action blinds the loop forever | the `OBSERVED` branch of `reduce.apply` |
| I6 | The frame fingerprint covers only what can make progress (observation, candidates, exclusion set, goal), not history | cycles are never detected because history keeps growing | `frame.build_frame` |
| I7 | The reducer is pure; state is derived from events only | state cannot be replayed or explained | `reduce.apply` |
| I8 | Only a verifier's `satisfied` counts as success; `unknown` stays unknown | the model's DONE is treated as completion | `_verify` |
| I9 | The goal text changes only through an explicit `TASK_UPDATED` | page or tool output rewrites the task | `reduce.apply` |
| I10 | Steps, wall clock, candidate count, frame size and verification count all have hard limits | runaway cost | `LoopConfig` |

## Receipt semantics

| Receipt | Meaning | Runtime behavior |
|---|---|---|
| `completed` | The result is definite (it may still be a failure) | Record it; check for effect (did the revision change?) |
| `rejected` | Definitely did not start | Record it; it may be considered again once conditions are fixed |
| `pending` | Accepted, outcome undecided | Becomes unresolved; the next step may only query |
| `unknown` | Cannot determine whether it happened | Becomes unresolved; query only, **never retry** |

"A tool call returned" ≠ "the action took effect" ≠ "the task is done". The kernel records and judges
these three separately.

## Known boundaries

- Single environment, single-writer execution. Multi-environment parallel writes, sub-loops and
  automatic planning are not in the first version.
- Candidate coverage (the right action simply not being in the candidate set) cannot be detected by the
  kernel; it depends on the adapter and on evaluation. The kernel offers a `complete` flag and a
  `blocked` exit, and records whether the candidate set claimed completeness when blocked.
- Observation delay (an effect arriving after the observation) is counted as one no-effect step. That is
  deliberately conservative: better to mark an action once than to submit it twice. When the effect
  finally appears, the observation revision changes and the action returns to the candidate set
  automatically.
- The loop itself does not produce correctness: any candidate can still be chosen wrongly for semantic
  reasons, and the final verdict still depends on an independent verifier.

## Related documents

- [`spec/questions.md`](../spec/questions.md) and [`spec/state.md`](../spec/state.md): the wire
  specifications (v0.1) for the question sent to the model and the state it sees, including the source
  of each rule and size limits; [`spec/fixture-dark-mode.json`](../spec/fixture-dark-mode.json) is the
  standard fixture for the same specification. The current implementation is a subset of that
  specification; the mapping table is at the end of `state.md`.
- [`pi-integration.md`](pi-integration.md): managed host, bundle contract, cognition and lifecycle
  semantics.
- [`getting-started.html`](getting-started.html): walkthrough for external engineers.

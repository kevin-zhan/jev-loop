# Jev Loop

This is an independent project with its own Git history and dependencies; read this file and `README.md`
before working in it.

## What this project is

An explicit-state decision runtime: **code owns the loop, the state and the termination; a policy
(Jev or any implementation) only chooses inside one frame.** It covers the responsibilities that fall
outside `state + questions → answer`. The design rationale is in `README.md`; the first kernel version
does single-environment, single-writer execution only — no multi-environment work, automatic planning
or sub-loops.

## Boundaries

- The kernel (`src/jev_loop/core/`) must not depend on Jev, HTTP, a browser or any external state beyond
  the clock; all model-facing code lives in `src/jev_loop/policies/`. New environment behavior goes into
  adapters, not the core. pi lifecycle and the cognition bridge stay in `src/jev_loop/host/`,
  `extensions/` and bundles, and must not leak back into the kernel.
- Events are the single source of truth: every state change must go through the events in
  `core/events.py` and the pure reducer in `core/reduce.py`. Never mutate `RuntimeState` directly, and
  never do I/O or call a model inside the reducer.
- A candidate id returned by a policy is valid only inside its frame; the observation and preconditions
  must be re-checked before execution. `request_finish` only requests verification, and only a verifier
  returning `satisfied` counts as success; `unknown` must not be rounded to success or failure.
- Do not remove the guards (no-effect / repeat / cycle) to "make the loop smoother": for a deterministic
  policy they are the only escape valves. Changing guard semantics requires updating the tests and the
  guard table in `docs/design.md`.
- Operations in `pending` / `unknown` may only be queried, never resent; `idempotency=NONE` actions are
  never retried blindly.
- Never write real API keys, cookies or page login sessions into code or tests. The only networking code
  in the kernel is `http_request_fn` in `policies/jev.py`. A bundle's own network adapter must stay in
  the bundle, and secrets must never reach host events or snapshots.
- A managed controller's `request_stop` must be thread-safe and release held inputs synchronously; real
  devices also need an out-of-process watchdog. `accepted` is not a stop confirmation: only terminal
  status, `resources_released=true` and the executor's confirmation mean stopped. When a worker dies
  unexpectedly, its resource claims stay quarantined and must not be taken over automatically by a new
  run.

## Documentation language

Documentation in this repository is English-canonical, with information-equal Chinese copies named
`*.zh-CN.md` / `*.zh-CN.html` next to the English file. `AGENTS.md` and `skills/pi-jev/SKILL.md` are
English only. Code comments, runtime messages and commit messages are English.

## Verification

```sh
uv sync
uv run pytest          # everything is offline: mock environment, deterministic policy, fake requester
uv run ruff check .
npm test                                # real pi RPC load, no model calls
node --experimental-strip-types --check extensions/pi-jev.ts
uv run jev-loop-demo --scenario clean   # all three scenarios: clean / noop / cycle
```

- Tests must not call paid APIs; when a real wire shape needs verification, write an explicit script and
  state its cost — such scripts do not belong in pytest.
- Expected coverage: a rejected frame binding, the four receipt states, unresolved operations never
  being resent, an action with no effect still allowing progress after the candidate set narrows, a
  reversible cycle suspending by default (`awaiting_evidence`) instead of burning the step budget,
  failing with `cycle_detected` only after escalations hit the limit, verification `unknown` not
  counting as success, and reducer purity.

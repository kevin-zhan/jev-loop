# Jev Loop

This is an independent project with its own Git history and dependencies; read this file and `README.md`
before working in it.

## What this project is

A runtime for high-speed intelligent loops: **Jev's fast, structured judgments make the continuous
observe → decide → act → update cycle practical; code owns the loop, the state and the termination, and a
policy (Jev or any implementation) only chooses inside one frame.** It covers the responsibilities that
fall outside `state + questions → answer`. The design rationale is in `README.md`; the first kernel version
does single-environment, single-writer execution only — no multi-environment work, automatic planning
or sub-loops.

The repository also carries the **Jev Bundle Specification (manifest v2)**: a host-neutral, implementable
standard for the environment-specific half of a loop, so any agent with a shell can create, discover,
inspect, validate and run a bundle without pi. `spec/bundle-standard.md` is the normative text and
`src/jev_loop/bundles/` is its only implementation.

## Boundaries

- The kernel (`src/jev_loop/core/`) must not depend on Jev, HTTP, a browser or any external state beyond
  the clock; all model-facing code lives in `src/jev_loop/policies/`. New environment behavior goes into
  adapters, not the core. pi lifecycle and the cognition bridge stay in `src/jev_loop/host/`,
  `extensions/` and bundles, and must not leak back into the kernel. `core/` must not import
  `bundles/` either.
- The bundle contract lives in `src/jev_loop/bundles/` (standard library only): manifest parsing,
  discovery, resolution, static validation, scaffolding and conformance. The CLI, the doctor and the host
  all call it — never add a second parser, resolver or trust-root check. Discovery and validation are
  inert: they must not import or execute bundle code, install a dependency, write inside a bundle or make
  a network request, and a test proves it with an audit-hook child process.
- Manifest text, `BUNDLE.md` and cognition context are data, not authority: nothing inside a bundle grants
  a permission. Declared `authorizations`, `resources`, `dependencies`, `stop`, `verification` and
  `output` are advisory and must be reported as such; the enforced contract is the version, required
  fields, closed field set, name/directory match, `BUNDLE.md`, `entrypoint`, the `python_path` bound, the
  declared schemas (as definitions and against inputs/config), `credential_env` names, template tokens and
  the scaffold refusal. Do not add security-looking fields that nothing enforces.
- `bundle validate` and a run `start` are the **same static gate**: `start` refuses the same error-level
  findings before any run directory, journal, resource claim or worker exists, reusing one resolution and
  one implementation (`validate_resolved`). Warnings and advisory declarations never block a start. Do not
  add a second judgment or a second parser for either path.
- The token scan behind that gate must stay bounded and symlink-free: it may not follow a symlink, read
  outside the bundle, or silently claim completeness after hitting its budget (`token_scan_incomplete`,
  `symlinks_not_scanned`). A `BUNDLE.md` that resolves outside the bundle is an error.
- `config` holds defaults merged shallowly with the caller's `bundle_config`: static validation checks each
  declared default, the merged object is checked strictly at start, and no implicit deep merge is added.
- A scaffold (`scaffold: true`) is refused by default and proves plumbing only; `allow_scaffold` is a
  test-only switch, is never set on a user's behalf, and a scaffold run is never evidence that a user task
  was done. The same holds for the built-in `diagnostic` probe. The pi extension is an optional client of
  the same runtime and must not become the place where bundle semantics are defined.
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
  in the kernel is `http_request_fn` in `policies/jev.py`. Live credentials come from the process
  environment (`TYPESAFE_API_KEY` by default) through `default_request_fn()`, or from an explicit
  `request_fn` injection; a missing or unusable credential fails before the first action and never falls
  back to a mock. `uv run jev-loop-doctor` is the offline preflight and must not print a credential
  value, length or hash. A bundle's own network adapter must stay in the bundle, and secrets must never
  reach host events or snapshots.
- A managed controller's `request_stop` must be thread-safe and release held inputs synchronously; real
  devices also need an out-of-process watchdog. `accepted` is not a stop confirmation: only terminal
  status, `resources_released=true` and the executor's confirmation mean stopped. When a worker dies
  unexpectedly, its resource claims stay quarantined and must not be taken over automatically by a new
  run.

## Documentation language

Documentation in this repository is English-canonical, with information-equal Chinese copies named
`*.zh-CN.md` / `*.zh-CN.html` next to the English file. `AGENTS.md`, `skills/jev-loop/SKILL.md` and
`skills/jev-bundle-creator/SKILL.md` are English only. Code comments, runtime messages and commit
messages are English. The skills are copyable: keep every link inside its own skill directory so it still
resolves after being copied to `.agents/skills/`.

## Verification

```sh
uv sync
uv run pytest          # everything is offline: mock environment, deterministic policy, fake requester
uv run ruff check .
uv run jev-loop-doctor --offline   # local configuration preflight, no network request, no key needed
npm test                                # real pi RPC load, no model calls
node --experimental-strip-types --check extensions/pi-jev.ts
uv run jev-loop-demo --scenario clean   # all three scenarios: clean / noop / cycle
uv run jev-loop bundle conformance project:offline-switchboard --run-tests   # reference bundle
scripts/wheel-smoke.sh                  # packaging: build, install outside the checkout, init, run
```

- Tests must not call paid APIs; when a real wire shape needs verification, write an explicit script and
  state its cost — such scripts do not belong in pytest. Offline transport coverage uses a loopback
  HTTP fixture (auth failure, timeout, redirect refusal, error-body redaction).
- Live Jev runs are explicit and budgeted: `TYPESAFE_API_KEY` must already be in the process
  environment (no `.env` parsing, keychain or other-project lookup), the request count is bounded and
  reported, failures and timeouts count against the budget, and nothing is retried automatically.
- Bundle work additionally has to keep: a bundle reference that is both a name and a relative file being
  refused as ambiguous, an unsupported `schema_version` failing before execution, a symlink or path
  escaping the project root being refused, declared inputs/config being enforced before a run exists and
  on `update`, and the legacy `schema_version: 1` manifest plus the `jev-loop-host rpc` protocol still
  working unchanged.
- Expected coverage: a rejected frame binding, the four receipt states, unresolved operations never
  being resent, an action with no effect still allowing progress after the candidate set narrows, a
  reversible cycle suspending by default (`awaiting_evidence`) instead of burning the step budget,
  failing with `cycle_detected` only after escalations hit the limit, verification `unknown` not
  counting as success, a missing or header-unsafe credential failing before any action, and reducer
  purity.

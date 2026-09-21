# Authoring a Jev Loop bundle

**English** | [简体中文](bundle-authoring.zh-CN.md)

This is the practical guide for writing a bundle: the creator skill, the templates, the
offline tests and the conformance path. The normative contract is
[spec/bundle-standard.md](../spec/bundle-standard.md); this document does not restate it.

## 1. Decide whether a bundle is the right shape

A bundle is worth writing when the environment has to **keep moving** while the agent reasons:
repeated observe-then-choose-the-next-action steps, a reliable current state, clear next
actions, quick feedback, and a need to stop safely. Reading a file, fetching one page, running
a test or editing code is a tool call, not a bundle.

Before writing code, answer: which state can be read reliably? Which action moves it? What is
the *independent* evidence that the goal holds? Which external input must be held
exclusively? If the verifier cannot be independent of the model, the bundle is not ready.

## 2. Use the creator skill

The repository ships two Agent Skills:

- `skills/jev-bundle-creator/` — the authoring workflow, the manifest contract, the checklist
  and the offline-testing guidance.
- `skills/jev-loop/` — the runtime/consumer side: discovery, validation, the request protocol,
  heartbeat, cognition and safe stop.

Any harness that reads Agent Skills can use them; copy a skill directory into
`.agents/skills/` (project) or `~/.agents/skills/` (user) for harnesses that scan those
locations. Nothing in the repository changes your global agent configuration.

```sh
# the scaffold, from an installed package anywhere
jev-loop bundle init review-notes --description "triage the review inbox"
```

That writes `<project_root>/.agents/jev-bundle/review-notes/` with:

| File | Purpose |
|---|---|
| `bundle.json` | the manifest v2 contract, `scaffold: true` |
| `BUNDLE.md` | what an agent reads before starting it |
| `bundle_controller.py` | the controller skeleton; `run` raises until implemented |
| `tests/test_bundle_offline.py` | stdlib `unittest` tests for the factory shape and the manifest contract |

`init` never overwrites anything: it refuses an existing target path outright, creates every
file exclusively, and cleans up exactly what it created if it fails. Templates ship inside the
package, so the same command works from a wheel outside this checkout.

Use a unique controller module name (the template uses `bundle_controller`) — the host puts
the bundle's own directory on `sys.path`, so two bundles that both expose `controller` would
collide inside one process.

## 3. Fill in the contract

- Type the inputs and the config, with bounds and `additionalProperties: false` where a typo
  would be dangerous. They are enforced before a run exists. `config` holds *defaults* that are
  merged shallowly with the caller's `bundle_config`; the static check validates each default you
  declare without demanding that the defaults alone satisfy `required`, while the merged object
  is checked strictly at start.
- Declare the resource keys the bundle actually holds, and pass the same keys in
  `resource_keys` when starting it. The declaration is advisory; the run's claim is real.
- Declare credential **variable names** only. Never a value, never a path to someone's private
  file.
- Keep `python_path` inside the bundle directory.
- Describe the stop and verification intent, and keep `max_steps`/deadlines honest.
- Leave `scaffold: true` until the work below is done.

## 4. Implement the controller

Four methods, and the rules that do not change:

```python
def run(self, stop_event: threading.Event) -> ControllerResult: ...
def snapshot(self) -> Mapping[str, Json]: ...
def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None: ...
def request_stop(self, reason: str) -> None: ...
```

- `run` executes on the engine thread; the other three may be called from the coordinator
  thread and must be thread-safe.
- `request_stop` releases held external inputs **synchronously** and never waits for a model
  or a network response. Return `resources_released=True` only when release is confirmed;
  otherwise the host quarantines the claim, which is the honest outcome.
- `snapshot` is bounded and JSON-serializable; full evidence belongs in the run's
  `artifacts/`.
- Long questions go through `services.cognition.request(...)`; the loop keeps making progress
  while the answer is pending, and a result is adopted only after re-checking the world
  preconditions it was about.
- Hosting an existing `jev_loop.Loop` is the short path: use `jev_loop.host.LoopController`
  (see the reference bundle below).

## 5. Test it offline

Write the tests that need no model, no network and no device: the factory shape, the manifest
contract, the adapter's observe/offer/execute behavior (including a no-effect action and a
failing precondition), the verifier saying `satisfied` *and* `unsatisfied` from ground truth,
and the loop ending with a named reason instead of spinning.

```sh
cd .agents/jev-bundle/<name>
python3 -m unittest discover -s tests -v
```

The unrendered-token scan that guards against half-rendered templates is deliberately bounded:
it never follows a symlink (so it cannot read outside your bundle), prunes hidden and cache
directories, and anchors the walk to pinned directory descriptors so a substituted directory or file
cannot be followed. It reports `token_scan_incomplete` when it hits a budget, cannot read a file, or
sees a file change while reading it — and then it produces no fingerprint rather than a partial one.
Change detection is best-effort: a writer can still race the checks. Keep bundle files regular files (a FIFO or a
link is skipped and reported), keep them from changing mid-scan, and keep `BUNDLE.md` inside the
bundle (a link pointing outside it is an error).

Then run the reusable conformance path:

```sh
jev-loop bundle validate project:<name>
jev-loop bundle conformance project:<name> --run-tests
```

`conformance` combines the static contract, a child-process proof that discovery imported
nothing and touched nothing, and your own tests under a bounded timeout. Its runtime-semantics
section stays `not_checked` — and it never claims the factory shape, dependencies or
credentials were verified statically. Conformance itself runs that one isolated probe
subprocess (plus your tests when you ask for them); inside the probe, discovery and validation
start no further process.

`--run-tests` is **your** code in a child process with a timeout: trusted code, not a network
sandbox, and nothing about it proves your tests have no side effects. Keep them offline by
construction.

`bundle validate` and `start` are the **same gate**: an error-level finding refuses a start
before any run directory, journal or resource claim exists, so a green validation is a faithful
pre-check. Warnings and advisory declarations never block a start.

`bundle init` prints follow-up commands that match where the bundle actually landed: a bundle in
the discovery root is addressed by name, one inside the project but elsewhere is addressed by
explicit path, and one created outside the project root gets only the author-test command plus a
note, because the trust root would refuse to validate or start it there.

## 6. Finish and run it once

1. Set `"scaffold": false` only after the controller is implemented and the tests pass. Until
   then the host refuses to start the bundle by name or path (an explicit
   `allow_scaffold: true` exists for plumbing checks only, and such a run proves nothing about
   a user task).
2. Run it once through the protocol and read the evidence:

```sh
printf '%s' '{"action":"start","owner_id":"author-check","idempotency_key":"first-run",
  "project_root":"'"$PWD"'","bundle":"project:<name>",
  "task":{"goal":"<goal>"},"max_runtime_seconds":120}' | jev-loop rpc
```

3. Poll `inspect`, answer any `pending_cognition`, then `stop` and require a terminal status
   plus `resources_released: true`. The verifier's verdict is what counts as the result.

Do that acceptance run against an **isolated synthetic or fixture environment with a fake
requester** first. Reaching the bundle's real environment — a real model, a paid API, a
website, a device — is a separate step that needs the user's explicit authorization, scope and
budget for that run. A missing or unusable credential must fail before the first action; it
never justifies a mock or a synthetic success, and an offline acceptance run is never reported
as the real task having been done.

## 7. The worked example in this repository

`.agents/jev-bundle/offline-switchboard/` is a complete, discoverable, offline reference:
manifest v2 with full metadata, `BUNDLE.md`, a controller that hosts the real kernel through
`LoopController`, and author tests that run the kernel in process.

```sh
jev-loop bundle list
jev-loop bundle show project:offline-switchboard
jev-loop bundle conformance project:offline-switchboard --run-tests
```

It deliberately does not exercise runtime cognition: that is the built-in `diagnostic` bundle's
job, and it is started by the host test suite. Keeping the two proofs separate is intentional —
a simple reference bundle should stay simple.

## 8. Working outside a checkout

The CLI, the templates and both skills work from an installed package:

```sh
uv build                                  # or: python -m build
python3 -m venv /tmp/jev-loop-venv
/tmp/jev-loop-venv/bin/pip install dist/jev_loop-*.whl
/tmp/jev-loop-venv/bin/jev-loop bundle init my-bundle --project-root /tmp/my-project
```

`scripts/wheel-smoke.sh` runs exactly that end to end (build, install outside the checkout,
init, validate, author tests, conformance) as the packaging acceptance check.

## 9. Common mistakes

| Symptom | Cause |
|---|---|
| `no bundle named '<x>'` | the directory is not under `<project_root>/.agents/jev-bundle/`, or its name differs from the manifest |
| `is not usable: missing BUNDLE.md` | the instructions file is missing next to `bundle.json` |
| `schema_version 1 is the legacy manifest format` | a v1 manifest sits in the standard location; run it by path or migrate it |
| `is ambiguous` | a bare name that is also a relative file; use `project:<name>` or `./path/bundle.json` |
| `python_path ... outside the bundle directory` | the bundle imports code from outside itself |
| `declares scaffold=true and is refused by default` | finish the bundle and set `scaffold: false` |
| `task.inputs does not match the bundle's inputs_schema` | the caller's inputs and the declared contract disagree |
| `declared schema ... not a supported schema` | an unsupported keyword or an unenforceable shape; see the closed subset in the specification |
| `unrendered_template_token` | a template placeholder was never rendered; re-run `bundle init` instead of editing a template in place |

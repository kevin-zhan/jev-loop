---
name: jev-bundle-creator
description: Create, extend and validate a Jev Loop behavior bundle from a real template instead of prose. Use when a project needs a new .agents/jev-bundle/<name>/ bundle, when an existing bundle must adopt the standard manifest v2 metadata and typed input/config contract, when a bundle needs an independent verifier, offline tests or a conformance check, or when a scaffold has to be completed so the host stops refusing to start it.
compatibility: Requires Python 3.12+ and the jev-loop package (its jev-loop CLI). Templates ship inside the installed package, so it works outside a source checkout. Scaffolding, static validation and the author test loop are offline — no model call, no network, no dependency install. Verifying the bundle's own real environment is a separate, explicitly authorized step; author tests are trusted code, not a network sandbox.
---

# jev-bundle-creator

A bundle is the environment-specific half of a Jev Loop: the manifest declares what it is
and what it needs, `BUNDLE.md` tells an agent when to use it, and the controller owns the
environment adapter, the policy injection and the independent verifier. The runtime owns the
loop, the guards, the resource claims, the lease and the stop semantics.

Create real, checkable structure — not a description of one.

## Workflow

1. **Understand the concrete environment.** Which observable state can be read reliably?
   Which action moves it? What is the *independent* evidence that the goal holds? What
   external input must be held exclusively? If the task is one tool call, stop: a bundle is
   the wrong shape.
2. **Scaffold it.**

   ```sh
   jev-loop bundle init <name> --description "<one line>"
   # writes <project_root>/.agents/jev-bundle/<name>/{bundle.json,BUNDLE.md,bundle_controller.py,tests/}
   ```

   It refuses to touch an existing path and never overwrites anything. Read the generated
   files: the manifest is a real contract with `"scaffold": true`.
3. **Fill in the contract.** Typed `inputs_schema` and `config_schema`, the declared
   resource keys, the credential variable **names**, the stop and verification intent. See
   `references/bundle-contract.md` for every field, what is enforced and what is advisory.
4. **Implement the controller.** Keep the four managed methods (`run`, `snapshot`,
   `update`, `request_stop`) and make `request_stop` release held inputs synchronously. If
   you host a `jev_loop.Loop`, use `LoopController`.
5. **Write the offline tests.** Start from the generated `tests/test_bundle_offline.py`:
   factory shape, manifest contract, adapter behaviour, verifier disagreement in both
   directions. Everything must run without a model, a network or a device.
6. **Verify, then finish.**

   ```sh
   jev-loop bundle validate project:<name>
   jev-loop bundle conformance project:<name> --run-tests
   ```

   When the tests pass and the verifier is genuinely independent, set `"scaffold": false`.
   Only then can the bundle be started by name.
7. **Accept it on an isolated run first.** The default acceptance run uses a synthetic or
   fixture environment with a fake requester: `jev-loop rpc` with `start` against a
   local/offline adapter, then poll, answer cognition and `stop`, confirming terminal status
   plus `resources_released: true`. Use the **jev-loop** skill for the request/response
   details.
8. **Only then, and only with the user's authorization, run it against the real
   environment.** A run that reaches a real model, a paid API, a website or a device needs the
   user's explicit scope and budget for *that* run, agreed beforehand. A missing or unusable
   credential must fail honestly — never fall back to a mock, a stub or a synthetic success,
   and never present an offline acceptance run as if the real task had been done.

## Non-negotiables

- **A scaffold is not a result.** `scaffold: true` means the plumbing is in place. The host
  refuses to start it by default; never report a user task as done from a scaffold or from
  the built-in `diagnostic` bundle.
- **Verification must be independent.** Re-read the environment in the verifier; never let
  the model's own "done" satisfy it. `unknown` stays unknown.
- **Receipts are typed.** `pending` and `unknown` operations may only be queried, never
  resent; `idempotency=NONE` is never retried.
- **Bounds exist for a reason.** Keep `max_steps`, deadlines and the stop grace honest and
  small. A run that cannot end is a bug.
- **Stop and release are two different things.** `request_stop` releases held inputs
  synchronously and never waits for a model or the network; report
  `resources_released=false` whenever release is not confirmed.
- **Author tests are not a sandbox.** `--run-tests` runs your own code in a child process
  with a timeout; it does not block network access or prove the absence of side effects. Keep
  the tests offline by construction (fake requesters, fixtures, temporary directories), and
  never point them at a real service or device without the user's authorization for that.
- **No secrets in the bundle.** Declare credential variable names only
  (`dependencies.credential_env`); values come from the process environment. Never write a
  key, cookie or page session into the manifest, code, tests, task or events.
- **Cognition freshness.** A cognition answer is a suggestion about a past observation: the
  bundle re-checks its world preconditions and resource versions before adopting it.
- **Self-contained.** `python_path` must stay inside the bundle directory; declare
  dependencies instead of installing them; no absolute author paths.

## References

- `references/bundle-contract.md` — every manifest v2 field, enforced versus advisory.
- `references/authoring-checklist.md` — the checklist to run before flipping `scaffold`.
- `references/offline-testing.md` — which tests to write and what conformance does and does
  not prove.

Scripts (thin wrappers over the installed CLI; they fail with a clear message if `jev-loop`
is not on `PATH`):

```sh
scripts/init-bundle.sh <name> [--project-root DIR]
scripts/validate-bundle.sh <ref> [--project-root DIR] [--run-tests]
```

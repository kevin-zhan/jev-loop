# Authoring checklist

Run this before setting `"scaffold": false`. Each line is something a reviewer can check.

## Contract

- [ ] `name` equals the directory name; `version` and `description` are filled in.
- [ ] `when_to_use` names the concrete situation — and the situations where this bundle is
      *not* the right tool.
- [ ] `inputs_schema` describes every input the controller reads, with
      `additionalProperties: false` where a typo would be dangerous; `config_schema` covers
      every config key with real bounds; `config` defaults satisfy it.
- [ ] `resources.keys` lists exactly the exclusive external inputs the bundle holds, and the
      caller passes the same keys in `resource_keys`.
- [ ] `dependencies.credential_env` contains variable names only; no value, no `.env` path,
      no other project's config.
- [ ] `python_path` stays inside the bundle directory and the bundle imports nothing from
      outside it except declared packages.
- [ ] `BUNDLE.md` is short, specific and honest about what the bundle does not do.

## Controller

- [ ] `run` executes the observe → decide → act → update cycle and returns a terminal
      `ControllerResult`.
- [ ] `request_stop` releases held external inputs synchronously and never waits for a model
      or a network response; `resources_released` is only `true` when release is confirmed.
- [ ] `snapshot` is bounded, JSON-serializable and carries what a reviewer needs
      (progress, held inputs, verification verdict).
- [ ] `update` validates what it can and refuses changes it cannot honour safely.
- [ ] Long model questions go through `services.cognition.request(...)` instead of blocking
      the loop; the environment keeps moving while the answer is pending.
- [ ] A cognition result is adopted only after re-checking the world preconditions and
      resource versions it was about.
- [ ] Guards, budgets and stop conditions are left to the kernel: no hidden retry loop that
      resends a `pending`/`unknown` operation or an `idempotency=NONE` action.

## Verification

- [ ] The verifier re-reads the environment itself; it does not consume the policy's or the
      observation's claim.
- [ ] `unknown` stays unknown; it is not rounded to success or failure.
- [ ] Evidence is written where the caller can read it and mirrored into the snapshot.
- [ ] The happy path, a no-effect action and a failing precondition are all covered offline.

## Offline tests

- [ ] `python3 -m unittest discover -s tests -v` passes without network, credentials or a
      device.
- [ ] The factory shape is asserted (the static validator does not check it).
- [ ] At least one test proves the adapter's observation contains every field the
      preconditions and the verifier rely on.
- [ ] At least one test proves the loop ends honestly when the environment misbehaves.
- [ ] For real devices: an out-of-process watchdog exists, and the checklist entry above
      about synchronous release is tested.

## Before declaring done

- [ ] The default acceptance run was made against an isolated synthetic/fixture environment
      with a fake requester, and any run against the real environment had the user's explicit
      authorization, scope and budget. A missing credential failed honestly (no mock, no
      synthetic success).
- [ ] The offline tests stay offline by construction; `--run-tests` is trusted code, not a
      network sandbox, so nothing relies on it to block network access.
- [ ] `jev-loop bundle validate project:<name>` reports no errors and you have read the
      warnings and advisory entries.
- [ ] `jev-loop bundle conformance project:<name> --run-tests` passes, and you accept that
      its runtime-semantics section is `not_checked`.
- [ ] `"scaffold": false` is set, and nothing in the bundle still claims a goal is satisfied
      by plumbing alone.

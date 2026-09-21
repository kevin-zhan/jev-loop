# offline-switchboard

The discoverable offline reference bundle of the Jev Bundle Specification. It runs the real
kernel against a synthetic switchboard: no network, no model call, no credential, no device.

## When to use it

- To verify that discovery, validation and the managed host work on a machine with no Jev
  credential.
- As a worked example of a complete manifest v2 bundle when you write your own.

It is **not** evidence that any user task was done, and it is not an environment adapter:
the switchboard world is a local in-process dictionary.

## Try it

```sh
# from the repository root
jev-loop bundle list
jev-loop bundle show project:offline-switchboard
jev-loop bundle validate project:offline-switchboard
jev-loop bundle conformance project:offline-switchboard --run-tests
```

Start a managed run through the host request protocol (the same protocol any agent uses):

```sh
printf '%s' '{"action":"start","owner_id":"me","idempotency_key":"offline-1",
  "project_root":"'"$PWD"'","bundle":"project:offline-switchboard",
  "task":{"goal":"turn on every required switch"},"max_runtime_seconds":30}' | jev-loop rpc
```

Then `inspect` the returned run id and confirm `status: succeeded`,
`resources_released: true` and `controller.verification.verdict: satisfied` before calling it
done. A live process or an accepted stop is not a result.

## Contract

| | |
|---|---|
| entrypoint | `bundle_controller:build` (`python_path` is `.`, the bundle's own directory) |
| inputs | optional `required` / `switches` objects that override the config defaults |
| config | `switches`, `required` (name → boolean) and `max_steps` (1..64) |
| resource keys | none: this example holds no external input |
| credentials | none |
| stop | `request_stop` marks the controller stopped and releases through the loop controller synchronously |

## What it deliberately does not do

- It does not exercise runtime cognition. Proving that the runtime keeps working while a
  cognition job is pending is the job of the built-in `diagnostic` bundle, which is started
  by the host test suite — not of this reference.
- It does not touch a browser, a device or an operating-system input channel.

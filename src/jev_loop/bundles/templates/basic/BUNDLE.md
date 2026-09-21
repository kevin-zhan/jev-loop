# __BUNDLE_NAME__

<!-- Progressive disclosure: keep this file short and specific. The manifest
     (bundle.json) is the machine contract; this file is what an agent reads before
     starting the bundle. Nothing here grants permission. -->

## What this bundle is for

Describe the environment this bundle drives and the observable state it reports. Say
plainly what it does **not** do.

## Inputs and configuration

- `inputs_schema.example_input` — what a caller must supply.
- `config.example_setting` — the declared default and its bounds.

## Runtime

- entrypoint: `bundle_controller:build`
- resource keys: none declared yet. Add one (for example `browser:<profile>` or
  `device:<id>`) when the bundle holds an exclusive external input, and pass the same
  key in the caller's `resource_keys`.
- credential environment variables: none. Declare variable **names** only, never values.

## Stop and verification

- A stop request is only a request: the run is stopped only when the status is terminal
  and `resources_released` is true.
- The verifier must re-read the environment itself; the model's own "done" is not
  evidence. Evidence belongs in `artifacts/`, and the snapshot carries the verdict.

## Scaffold status

This directory was produced by `jev-loop bundle init` and is an **unfinished scaffold**:
the controller raises until it is implemented, and the host refuses to start a bundle whose
manifest says `scaffold: true`.

1. Implement the environment adapter and the independent verifier.
2. Write the offline tests (the generated `tests/test_bundle_offline.py` is a starting
   point; add the adapter cases that need no network or device).
3. Run `jev-loop bundle conformance project:__BUNDLE_NAME__ --run-tests`.
4. Only then set `"scaffold": false` in `bundle.json`.

A scaffold proves the plumbing. It is never evidence that a user's goal was implemented or
verified.

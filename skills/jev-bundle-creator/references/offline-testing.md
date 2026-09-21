# Offline testing a bundle

Everything here runs without a model, a network, a credential or a device. That is the point:
a bundle that cannot be checked offline cannot be trusted in a loop.

## What to write

```text
.agents/jev-bundle/<name>/
  tests/
    test_bundle_offline.py     # stdlib unittest; run with: python3 -m unittest discover -s tests -v
```

1. **Manifest contract** — parse `bundle.json`, assert `schema_version`, that `name` equals
   the directory, that the declared `inputs_schema` is what the controller actually reads,
   and that `scaffold` reflects reality.
2. **Factory shape** — load the controller module by file path and call the declared
   entrypoint with a minimal fake spec/services. Assert the returned object has `run`,
   `snapshot`, `update`, `request_stop`, and that `snapshot()` is JSON-serializable. This is
   the only offline way to establish the factory shape, because the static validator
   deliberately reports it as `not_checked`.
3. **Adapter behaviour** — observe/offer/execute against the fake environment, including one
   no-effect action and one failing precondition.
4. **Verifier disagreement** — satisfy and violate the goal, and assert the verifier says
   `satisfied` and `unsatisfied` respectively, from ground truth rather than from the
   observation.
5. **Honest ending** — the loop stops with a named reason instead of spinning when the
   environment stops making progress.

Use the standard library (`unittest`). A bundle author should not have to install a test
framework into the environment that will run the bundle.

## What `conformance` does and does not prove

```sh
jev-loop bundle conformance project:<name> --run-tests
```

| Section | Meaning |
|---|---|
| static contract | the same checks as `bundle validate`: manifest, name/directory, `BUNDLE.md`, paths, declared schemas, config defaults |
| side effects | a child-process probe: no bundle module was imported, no subprocess spawned, no socket opened, no file inside the bundle changed while discovering and validating |
| author tests | your `tests/` were executed in a child process with a bounded timeout — **your trusted code**, not a sandbox |
| runtime semantics | always `not_checked`: cognition flow, guards, resource exclusivity, stop behaviour and verifier independence need a managed run |

The probe asserts what *bundle code* did. It does not claim that the interpreter imported
nothing at all, and running your tests proves nothing about their side effects.

## After the offline tests pass

The parts statics cannot establish still need a managed run: start the bundle, send one
heartbeat, answer a cognition job, stop it, and confirm terminal status plus
`resources_released: true`. Use the **jev-loop** skill for the request shapes.

Do that first against an **isolated synthetic environment with a fake requester** — that is
the default acceptance run. Reaching the bundle's real environment (a real model, a paid API,
a website, a device) is a separate step that needs the user's explicit authorization, scope
and budget for that run. A missing or unusable credential must fail before the first action;
it never justifies a mock or a synthetic success, and an offline acceptance run is never
reported as the real task being done.

`--run-tests` is **your** code running in a child process with a timeout: trusted code, not a
network sandbox. Nothing here proves the tests are free of side effects — keep them offline by
construction.

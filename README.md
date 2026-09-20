# jev-loop

[![CI](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**English** | [简体中文](README.zh-CN.md)

**A Python runtime for AI-driven tasks that keep observing, acting, and checking results.**

`jev-loop` lets a model choose only the next allowed step while code owns execution, state and success
verification; the model never has to remember what was tried or decide when to stop. An optional
[pi](https://github.com/earendil-works/pi) integration (pi-jev) runs a suitable project bundle in its own
worker, so the environment keeps moving while the main agent reasons.

This repository is currently private and source-only: install it by cloning. It is not published to
PyPI or npm.

## The problem

Repeated interactive tasks can run into failure modes that the model itself does not notice:

- an action that already had no effect can be picked again, because nothing forces a different choice;
- a tool returning can be mistaken for the action having worked, and the action working can be mistaken
  for the task being done;
- the loop can stall while the model "thinks", or burn its whole step budget instead of naming the cycle;
- afterwards there may be no replayable record of what was tried, what actually changed, and why success
  was believed.

Putting the loop, the state, the stop conditions and the success verdict in code — and giving the model
a narrow, well-formed choice — is what this project is about.

## Approach

```text
observe → offer → decide → validate → execute → reduce → guard
   ↑                                                       │
   └───────────────────────────────────────────────────────┘
```

- **Code owns the loop.** A policy (Jev or anything else) receives one immutable frame and may answer
  only with a candidate id from that frame. Observations, offered actions, pre-execution validation,
  stop conditions and budgets are all code.
- **Events are the state.** State is derived by a pure reducer over an append-only event log, so a run
  can be replayed and explained rather than reconstructed from chat history.
- **A result is not progress.** Every execution records a four-value receipt
  (`completed / rejected / pending / unknown`); `pending` and `unknown` operations may only be queried,
  never blindly resent, and `idempotency=NONE` is never retried.
- **Verification is independent.** `request_finish` only asks for verification; only a verifier
  returning `satisfied` counts as success, and `unknown` stays unknown.
- **Guards keep the loop alive or stop it honestly.** No-effect writes, repeats on one observation and
  revisited frames narrow the candidate set, then suspend, then end with a named reason. The full guard
  table and the invariants behind it are in [docs/design.md](docs/design.md).

## Use cases

Good fits:

- **Long-running environment work with a slow reasoning model.** The bundle is responsible for keeping
  the environment moving; the host gives it an independent worker and a channel for asking the main agent
  bounded questions asynchronously, so the loop does not block on a single reply.
- **Loops that must be stoppable and accountable.** Exclusive resource claims, leases, a two-phase
  stop, and quarantine when a worker dies without confirming that it released its inputs.
- **Runs that need an audit trail.** An append-only `events.jsonl`, bundle-owned artifacts, and a
  verifier that is not the model.

What this repository actually contains today:

- `uv run jev-loop-demo` scenarios (`clean`, `noop`, `cycle`) — offline kernel semantics with a
  deterministic mock environment. No network, no model calls.
- [`examples/bundles/switchboard`](examples/bundles/switchboard) — a small bundle that runs the kernel
  through pi-jev; a template for your own bundle, not a business adapter.
- A built-in `diagnostic` bundle — proves bridge semantics (keep working while a cognition job is
  pending, release inputs when stopped). It is never evidence that a user task was done.

What this repository does not contain: browser, phone, game or site adapters, and no generic MCP server.
If your environment needs one, you implement and verify a project bundle; see
[docs/pi-integration.md](docs/pi-integration.md).

### A real integration, in a separate project

A read-only United business-upgrade (MUA) availability query has been built as a task-specific bundle
in a separate private project (`united-pz-jev`), not in this repository. That bundle drives a dedicated,
isolated browser profile the site has already accepted, is strictly read-only, and reports only what
independent evidence confirms. Two boundaries from that work are worth repeating here: the upgrade
availability a site shows is not the same as numeric PZ inventory, and when inventory cannot be read the
answer must stay unknown rather than being written as zero.

## Quick start

Prerequisites: Python 3.12+ on Linux, macOS or WSL (the host uses Unix `fcntl`);
[uv](https://docs.astral.sh/uv/getting-started/installation/) is recommended and used below. The runtime
has zero third-party dependencies.

```sh
git clone https://github.com/kevin-zhan/jev-loop.git   # private repository: GitHub access required
cd jev-loop
uv sync
uv run jev-loop-demo --scenario clean
```

Expected output:

```text
status=succeeded stop_reason=None
steps=3 executions=2 decisions=3 world={'a': True, 'b': True}
verifications=['satisfied'] (only 'satisfied' counts as success)
```

The demo is fully offline: no API key, no network, no model calls — it is a semantics smoke test, not a
paid-Jev or real-business validation. `--scenario noop` and `--scenario cycle` exercise the other two
guard paths (no-effect narrowing and suspension instead of burning the step budget); both are explained
in [docs/getting-started.html](docs/getting-started.html).

Without uv (the runtime has no dependencies):

```sh
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/jev-loop-demo --scenario clean
```

## Integration paths

### A. As a Python library

```python
from jev_loop import Loop, TaskSpec
from jev_loop.policies.jev import JevPolicy, http_request_fn

loop = Loop(
    task=TaskSpec(
        goal="Turn on dark mode",
        success_criteria=("the appearance page reports dark mode as enabled",),
    ),
    environment=my_adapter,                  # observe / offer / execute / query / validate
    policy=JevPolicy(request_fn=http_request_fn(api_key=key)),
    verifier=my_verifier,                    # independent evidence; ignores the model's DONE
)
result = loop.run()
print(result.status, result.stop_reason)
```

This is an integration sketch, not a standalone runnable example: `my_adapter`, `my_verifier` and `key`
are yours to provide — an `Environment` implementation, an independent `Verifier`, and a TypeSafe/Jev API
key.

The kernel depends on three protocols only — `Environment`, `Policy`, `Verifier` — and
`policies/jev.py` is the only module that talks to the network (through an injectable `request_fn`, so
tests stay offline). Kernel entry points and ports are exported from `jev_loop`; see
[docs/getting-started.html](docs/getting-started.html) for a walked-through example.

### B. In pi, with project bundles

The repository root is itself a pi package (`package.json` declares `extensions/pi-jev.ts` and
`skills/pi-jev`). Install pi using its official installation instructions (see the
[pi repository](https://github.com/earendil-works/pi)) and make sure Python 3.12+ is available:

```sh
git clone https://github.com/kevin-zhan/jev-loop.git   # access required
cd jev-loop
pi install .            # register this local package with pi (references the clone in place)
# optional, unpinned remote install (private repository access required):
# pi install git:github.com/kevin-zhan/jev-loop
```

`/reload` an open session afterwards. The package provides the `jev_loop` tool
(`start / list / inspect / events / update / respond / stop / release_resources`), `/jev-runs`,
`/jev-self-test`, `/jev-stop-all` and the `pi-jev` skill. The extension calls `python3` by default
(`JEV_LOOP_PYTHON` overrides it) and puts the package's `src/` on the worker's `PYTHONPATH`, so the
Python package does not have to be installed globally.

Bundle manifest, controller protocol, runtime cognition, lifecycle and safety semantics:
[docs/pi-integration.md](docs/pi-integration.md).

### C. With your own host

No pi required. With the environment synced, `uv run jev-loop-host rpc` reads one JSON request from
stdin and writes one JSON response to stdout
(`start / list / inspect / events / heartbeat / update / stop / respond / release_resources`);
the host API is exported from `jev_loop.host`. A worked example of the JSON is in
[docs/getting-started.html](docs/getting-started.html).

## Limits and safety

- **Single environment, single writer.** The first kernel version has no multi-environment parallel
  writes, no automatic planning and no sub-loops. Each resource key has at most one active owner;
  overlapping claims conflict.
- **Adapters are yours.** The mock environment in this repository is a test double. A real adapter must
  prove its observations contain every field its preconditions and verifier rely on.
- **Bundle manifests are code.** A manifest loads project code with your privileges; only run reviewed
  manifests inside a trusted project root. Runtime cognition context is untrusted data, not user
  authorization.
- **Stopping is two-phase.** An accepted stop request is not proof that inputs were released; require a
  terminal status plus `resources_released=true`, and keep an out-of-process watchdog for real devices.
- **Liveness is not success.** A live process or a green diagnostic says nothing about the task; only
  the bundle's verifier evidence and a terminal run status do.
- **The core is synchronous; the worker is not.** `Loop.step()` executes one action at a time. pi-jev's
  managed host keeps the environment moving in a worker while a cognition job is pending — it does not
  turn the synchronous kernel into an async one.
- **Source-only distribution.** Not published to PyPI or npm; no version tags beyond the version fields
  in `pyproject.toml` and `package.json`.

## Documentation

| Document | English | 简体中文 |
|---|---|---|
| External-engineer walkthrough (three paths, bundle contract, safety, pitfalls) | [getting-started.html](docs/getting-started.html) | [getting-started.zh-CN.html](docs/getting-started.zh-CN.html) |
| pi integration and bundle contract | [pi-integration.md](docs/pi-integration.md) | [pi-integration.zh-CN.md](docs/pi-integration.zh-CN.md) |
| Kernel invariants and guard table | [design.md](docs/design.md) | [design.zh-CN.md](docs/design.zh-CN.md) |
| Question wire format (v0.1), design/spec document | [questions.md](spec/questions.md) | [questions.zh-CN.md](spec/questions.zh-CN.md) |
| State wire format (v0.1), design/spec document | [state.md](spec/state.md) | [state.zh-CN.md](spec/state.zh-CN.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) | [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md) |

The v0.1 documents describe the wire-format design; the current kernel implements a subset of them
(the mapping is at the end of the state specification). The English documents are canonical; the Chinese
files are kept information-equal. Code comments and runtime strings are English.

## Contributing and license

Contributions are welcome; the development environment, required checks and design constraints are in
[CONTRIBUTING.md](CONTRIBUTING.md). Tests never call paid APIs.

This project is released under the [MIT License](LICENSE).

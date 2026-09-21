# jev-loop

[![CI](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml/badge.svg)](https://github.com/kevin-zhan/jev-loop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**English** | [简体中文](README.zh-CN.md)

**High-speed intelligent loops powered by Jev.**

Jev makes hundred-millisecond-scale AI judgments practical.[^latency] `jev-loop` turns that capability
into a continuous cycle: observe the task state, choose the next action with Jev, execute it, update the
state from what was actually observed, and decide again. High-frequency local decisions no longer have to
occupy a full, slow agent reasoning turn.

Code still owns the loop, the execution and the stop conditions — the model chooses the next step from an
explicit offer, and every cycle stays bounded, replayable and verifiable.

A **bundle** is the environment-specific half: a directory under
`<project_root>/.agents/jev-bundle/<name>/` that declares what it drives, what it needs and how it is
verified. Bundles are a host-neutral standard with a CLI — any agent with a shell can create, discover,
inspect, validate and run one — and the
[Jev Bundle Specification (manifest v2)](spec/bundle-standard.md) is the normative contract. An optional
[pi](https://github.com/earendil-works/pi) integration (pi-jev) is one client of that same runtime, so the
environment keeps moving while the main agent reasons.

[^latency]: Latency note: the public example project
    [`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs/flights-measurement.json)
    records its Jev decision requests at commit `1231850a`: 17 decision requests with a median of 178 ms
    in that example's `docs/flights-measurement.json`. That is decision-request timing for one external
    example and workload — not a TypeSafe SLA, not a general latency or hard-real-time guarantee, and not
    whole-task duration; observation, execution, network and input size all affect how fast a loop
    actually runs.

This repository is currently private and source-only: install it by cloning. It is not published to
PyPI or npm.

## Why this exists

Interactive work does not wait. When a reasoning model re-plans the whole task before every step, each
local decision costs a full agent turn — and by the time the choice comes back, the page, screen or
process has already moved on. That is a timeliness problem: repeated local decisions are exactly where a
slow reasoning loop cannot keep up.

It is an efficiency problem too. A full turn of reasoning, context re-reading and text generation is a lot
of latency and tokens to spend on one bounded step. Jev answers that end of the job: a System One model
returns typed decisions and probabilities software consumes directly, so the judgment path can run inside
the loop rather than around it.

So the work is split by cost. Jev supplies the fast local judgment over the current state; the loop
executes the chosen step and folds the observed result back into the state. Complex planning, content
generation or anomalies can still go back to the slow main agent, bundles that can make progress on their
own keep working, and a step whose dependencies are unmet waits instead of acting blindly.

## How it works

```text
task state → Jev decision → action → observed state update → next decision
     ↑                                                              │
     └──────────────────────────────────────────────────────────────┘
```

1. **Observe the task state.** The bundle reports the observable state the next decision needs: current
   facts, progress, constraints and recent actions. "Global state" here is scoped to the task — not an
   omniscient world model, and not the raw DOM dumped into a prompt.
2. **Ask Jev to choose the next step.** The policy receives one immutable frame with that state and the
   candidate actions valid right now, and returns a candidate ID with decision metadata; the loop
   validates the choice before executing it.
3. **Execute and observe.** The bundle performs the action; the loop records the receipt and re-reads the
   environment instead of assuming the action worked.
4. **Update the state and decide again.** New observations enter the append-only event log, the state is
   derived again, and the next frame is offered — until an independent verifier confirms the work is done,
   or the loop stops for a named reason.

The executor does not hide the policy: the bundle owns the environment adapter, and the judgment stays
explicit. Code owns what the policy may decide (offered actions, preconditions, budgets, stop conditions),
which is what makes a fast model safe to run continuously.

## Use cases

The loop fits work that can report a **reliable current state**, offer **clear next actions**, and return
**quick feedback** about what changed:

- **Dynamic browser interaction.** A page has to be read and steered step by step, and each step's
  choice should not wait for a full reasoning turn.
- **Games, simulators and devices.** Long-running input that needs a fresh judgment every few hundred
  milliseconds. These are directions, not shipped integrations: each needs a purpose-built bundle with
  its own adapter and, for real devices, an out-of-process watchdog.
- **Long-running environment work with a slow reasoning model.** The bundle is responsible for keeping
  the environment moving; the host gives it an independent worker and a channel for asking the main agent
  bounded questions asynchronously, so the loop does not block on a single reply.
- **Loops that must be stoppable and accountable.** Exclusive resource claims, leases, a two-phase
  stop, and quarantine when a worker dies without confirming that it released its inputs.
- **Runs that need an audit trail.** An append-only `events.jsonl`, bundle-owned artifacts, and a
  verifier that is not the model.

What this repository actually contains today:

- **The Jev Bundle Specification (manifest v2) and its CLI**: `jev-loop bundle list / show / validate /
  init / conformance` plus `jev-loop rpc` — discover, inspect, validate, scaffold and run bundles with no
  pi and no agent-specific glue. `jev-loop-host` keeps working unchanged.
- [`.agents/jev-bundle/offline-switchboard`](.agents/jev-bundle/offline-switchboard) — a discoverable,
  offline reference bundle: a real manifest v2, an independent verifier, and author tests that run the
  kernel in process.
- Two Agent Skills: [`skills/jev-bundle-creator`](skills/jev-bundle-creator) for authoring a bundle from a
  real template, and [`skills/jev-loop`](skills/jev-loop) for discovering, validating and running one.
- `uv run jev-loop-demo` scenarios (`clean`, `noop`, `cycle`) — offline kernel semantics with a
  deterministic mock environment. No network, no model calls.
- `uv run jev-loop-doctor` — an offline preflight of the local Jev configuration: no network
  request, no credential value printed; `--bundle <ref>` also checks a bundle's manifest contract.
- [`examples/live-files`](examples/live-files) — a runnable live reference: real Jev decision
  requests, a real filesystem environment with synthetic content, and an independent verifier.
- [`examples/bundles/switchboard`](examples/bundles/switchboard) — a small legacy-format bundle that runs
  the kernel through the managed host; a template for your own bundle, not a business adapter.
- A built-in `diagnostic` bundle — proves bridge semantics (keep working while a cognition job is
  pending, release inputs when stopped). It is never evidence that a user task was done.

What this repository does not contain: browser, phone, game or site adapters, and no generic MCP server.
If your environment needs one, you implement and verify a project bundle; see
[spec/bundle-standard.md](spec/bundle-standard.md) and [docs/bundle-authoring.md](docs/bundle-authoring.md).
No third-party product is claimed to implement the bundle format already: adopting it means loading the
skill or driving the CLI.

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

Ready for real Jev decisions? [Connecting a real Jev service](#connecting-a-real-jev-service) has the
preflight and the runnable reference example.

Without uv (the runtime has no dependencies):

```sh
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/jev-loop-demo --scenario clean
```

Discover and validate the bundle that ships in this repository (offline, no key, no network):

```sh
uv run jev-loop bundle list
uv run jev-loop bundle show project:offline-switchboard
uv run jev-loop bundle conformance project:offline-switchboard --run-tests
```

## Integration paths

### A. Write and run a bundle with the standard CLI (any agent, no pi)

A bundle lives in `<project_root>/.agents/jev-bundle/<name>/` with a `bundle.json` contract and a
`BUNDLE.md` for the agent that will run it. Everything below is host-neutral: it needs Python 3.12+ and
a shell, not pi and not any particular agent product.

```sh
jev-loop bundle init review-notes --description "triage the review inbox"   # a real scaffold
jev-loop bundle validate project:review-notes                                # static contract
jev-loop bundle conformance project:review-notes --run-tests                 # + inert-discovery proof
```

The generated scaffold is explicitly unfinished: `scaffold: true` makes the host refuse to start it
until you implement the controller and the independent verifier and set `scaffold: false`. Running a
bundle uses the same one-request/one-response protocol as the host always had:

```sh
printf '%s' '{"action":"start","owner_id":"my-session","idempotency_key":"task-1",
  "project_root":"'"$PWD"'","bundle":"project:review-notes",
  "task":{"goal":"triage today's review inbox"},"max_runtime_seconds":600}' | jev-loop rpc
```

Then `inspect`, `events`, `heartbeat`, `respond` (cognition) and `stop`. The CLI does not heartbeat or
notify for you — `skills/jev-loop/references/lifecycle.md` states exactly what an agent outside pi has
to do. Normative contract: [spec/bundle-standard.md](spec/bundle-standard.md). Authoring guide:
[docs/bundle-authoring.md](docs/bundle-authoring.md). Worked example:
[`.agents/jev-bundle/offline-switchboard`](.agents/jev-bundle/offline-switchboard).

### B. As a Python library

```python
from jev_loop import Loop, TaskSpec
from jev_loop.policies.jev import JevPolicy, default_request_fn

loop = Loop(
    task=TaskSpec(
        goal="Turn on dark mode",
        success_criteria=("the appearance page reports dark mode as enabled",),
    ),
    environment=my_adapter,                  # observe / offer / execute / query / validate
    policy=JevPolicy(request_fn=default_request_fn()),   # TYPESAFE_API_KEY from the process env
    verifier=my_verifier,                    # independent evidence; ignores the model's DONE
)
result = loop.run()
print(result.status, result.stop_reason)
```

This is an integration sketch, not a standalone runnable example: `my_adapter` and `my_verifier`
are yours to provide — an `Environment` implementation and an independent `Verifier`.
`default_request_fn()` reads the credential from the process environment and fails with a named
error before the first action if it is missing or unusable; code that already holds a secret can
keep injecting it explicitly with `http_request_fn(api_key=...)`. For a complete runnable case see
[Connecting a real Jev service](#connecting-a-real-jev-service) below.

The kernel depends on three protocols only — `Environment`, `Policy`, `Verifier` — and
`policies/jev.py` is the only module that talks to the network (through an injectable `request_fn`, so
tests stay offline). Kernel entry points and ports are exported from `jev_loop`; see
[docs/getting-started.html](docs/getting-started.html) for a walked-through example.

### C. In pi, with project bundles (optional client)

The repository root is itself a pi package (`package.json` declares `extensions/pi-jev.ts` and the
`skills/jev-loop` and `skills/jev-bundle-creator` skills). pi is an optional client of the same runtime:
the extension supplies the session id as `owner_id`, heartbeats, and delivers cognition jobs, which is
exactly the part an agent outside pi does itself with `jev-loop rpc`. Install pi using its official
installation instructions (see the [pi repository](https://github.com/earendil-works/pi)) and make sure
Python 3.12+ is available:

```sh
git clone https://github.com/kevin-zhan/jev-loop.git   # access required
cd jev-loop
pi install .            # register this local package with pi (references the clone in place)
# optional, unpinned remote install (private repository access required):
# pi install git:github.com/kevin-zhan/jev-loop
```

`/reload` an open session afterwards. The package provides the `jev_loop` tool
(`start / list / inspect / events / update / respond / stop / release_resources / bundles /
validate_bundle`), `/jev-runs`, `/jev-self-test`, `/jev-stop-all` and both skills. The extension calls
`python3` by default (`JEV_LOOP_PYTHON` overrides it) and puts the package's `src/` on the worker's
`PYTHONPATH`, so the Python package does not have to be installed globally.

Discovery, validation and the trust root are identical to path A because both call the same runtime. The
pi adapter's own details — install, cognition delivery, lifecycle and safety semantics — are in
[docs/pi-integration.md](docs/pi-integration.md).

### D. With your own host

No pi required. With the environment synced, `uv run jev-loop-host rpc` (or `uv run jev-loop rpc`) reads
one JSON request from stdin and writes one JSON response to stdout
(`start / list / inspect / events / heartbeat / update / stop / respond / release_resources /
bundle_list / bundle_show / bundle_validate`); the host API is exported from `jev_loop.host`. A worked
example of the JSON is in [docs/getting-started.html](docs/getting-started.html).

## Connecting a real Jev service

The quick-start demo and the default test suite are offline and never call a paid API. A live run
needs a TypeSafe/Jev API key that **you provide**; this repository provides the supported
configuration entry point, the offline preflight and a runnable reference bundle. The Path B
sketch above is an integration you build yourself, so any live request it makes is your own paid
call.

### Credential

- The supported entry point is the environment variable `TYPESAFE_API_KEY`, read by
  `default_request_fn()` in `jev_loop.policies.jev`. Code that already holds a secret can keep
  injecting it explicitly with `http_request_fn(api_key=...)` or `JevPolicy(request_fn=...)`;
  `default_request_fn(env_name=...)` accepts a different variable name.
- The library reads the **process environment only**: no `.env` parsing, no keychain, no other
  project's configuration. A missing, blank or header-unsafe key raises a named error before the
  first network or environment action, and there is no silent fallback to a mock.
- Never put the key in a command-line flag, a run spec/task, `bundle_config`, an event, a snapshot
  or a cognition message: those are persisted and read back by agents.
- Get the key into the process environment, then run commands directly. Two supported ways:
  - export it in the shell (`export TYPESAFE_API_KEY=...`) and run `uv run jev-loop-doctor`;
  - or keep it in a private file and let uv load that file explicitly. Copy the empty template only when
    you do not already have a private file — never overwrite an existing one:

```sh
# only if you do not already keep this key in a local file:
cp .env.example .env && chmod 600 .env
# if .env already exists, keep it and edit that file instead of copying over it
uv run --env-file .env jev-loop-doctor
```

  `--env-file` takes a real file path and loads it into the process environment before the command
  runs; nothing in the library reads or parses the file, and the value is never printed.

### Offline preflight

```sh
uv run jev-loop-doctor            # human report
uv run jev-loop-doctor --json     # same report for scripts and agents
uv run jev-loop-doctor --offline  # installation/runtime check; no key required
```

The doctor validates Python ≥3.12, the host's Unix `fcntl` requirement, the endpoint/model/timeout
values and credential presence. Its JSON says `mode: "preflight"` and keeps
`authentication`, `connectivity` and `model_availability` as `not_checked`: a green report is a
local configuration statement, not a live-API verification. Exit codes: `0` local configuration
ok, `2` credential missing/blank/unusable, `3` invalid non-secret configuration or unsupported
platform, `1` unexpected error. Test it per run with `--bundle examples/live-files/bundle.json` to
also check the manifest the run will load.

### A runnable live reference

[`examples/live-files`](examples/live-files) is self-contained across machines and needs no private
repository or browser profile: real Jev decisions, a real filesystem environment with fixed
**synthetic** content, and an independent verifier.

```sh
# the variable is already in this process environment:
uv run python examples/live-files/run.py --max-steps 8 \
    --workspace "$(mktemp -d)/workspace"

# or load a private file explicitly (a real path; the value is never read by the library):
uv run --env-file .env python examples/live-files/run.py --max-steps 8 \
    --workspace "$(mktemp -d)/workspace"
```

What happens, in order:

1. Configuration and credential are validated first; the workspace is created only after that.
2. The workspace must be new or empty — a non-empty directory or a symlink is refused, and the
   example never overwrites, moves or deletes anything it did not create itself.
3. Each loop step is one real Jev decision request over the candidates offered in that frame;
   `--max-steps` (default 8) bounds the number of requests.
4. The environment performs real file operations (`inbox/alpha.txt` content, archiving
   `inbox/gamma.log` to `archive/gamma.log`) and the loop re-reads the directory after each step.
5. `LiveFilesVerifier` re-reads the filesystem itself and decides; the model's answer cannot make
   it pass. Evidence is written to `verification.json` in the workspace, which the run prints.
6. Nothing is deleted automatically: review the workspace and remove it yourself.

The runner exits `0` on success, `2` when the credential is missing or the service rejected it, `1` when
the loop itself failed or verification stayed unsatisfied, and `3` for a configuration, workspace or
command-line usage error. It is a reference example, not a production adapter and not a benchmark.
`examples/` is not part of the built wheel: run it from the source clone (as above), while
`jev-loop-doctor` is installed with the package and also works outside a checkout. The same adapter and
verifier also run as a managed bundle:

```sh
uv run jev-loop-host rpc <<'JSON'
{"action":"start","owner_id":"engineer","idempotency_key":"live-files-1",
 "project_root":"/path/to/jev-loop","bundle":"examples/live-files/bundle.json",
 "task":{"goal":"prepare the synthetic inbox"},"max_runtime_seconds":120}
JSON
```

When the key comes from an explicit file, prefix the same command with `uv run --env-file .env`.

Then read `inspect` / `events` and stop with `stop`. A run counts as done only when its status is
terminal, `resources_released` is true and `controller.verification.verdict` is `satisfied`. The
bundle always works in `run_dir/artifacts/workspace`, so two runs can never share a directory, and
its `bundle_config` accepts only `model`, `api_url`, `timeout` and `max_steps` (1..64, default 8).
`inspect` reports the effective `max_steps` and the `decision_requests` actually made.

### pi, OpenRouter and other providers

pi-jev does not read pi's credentials for Jev. The worker inherits the environment of the pi
process, so start pi from a process where the variable is already loaded — export it in the shell, or
let uv load a file explicitly:

```sh
uv run pi                     # the variable is already exported in this shell
uv run --env-file .env pi     # or load a private file explicitly
```

`/reload` does not re-import the key; start a new pi process.

Slow reasoning stays a pi concern configured in pi itself. If your pi session uses another
provider — OpenRouter, for example — configure it in pi, not here: pi supports
`/login openrouter` and the `OPENROUTER_API_KEY` variable (see `docs/providers.md` and
`docs/models.md` in the [pi repository](https://github.com/earendil-works/pi)). `TYPESAFE_API_KEY`
is only for Jev decision requests through `policies/jev.py`; jev-loop ships no other provider
client.

## What keeps it reliable

Fast judgment only helps if the cycle around it is trustworthy, so the loop, the state and the verdicts
are code:

- **Code owns the loop.** A policy (Jev or anything else) receives one immutable frame and may answer
  only with a candidate id from that frame. Observations, offered actions, pre-execution validation,
  stop conditions and budgets are all code, and the guards limit repeats of a step that had no effect.
- **Events are the state.** State is derived by a pure reducer over an append-only event log, so a run
  can be replayed and explained rather than reconstructed from chat history.
- **A result is not progress.** Every execution records a four-value receipt
  (`completed / rejected / pending / unknown`); `pending` and `unknown` operations may only be queried,
  never blindly resent, and `idempotency=NONE` is never retried.
- **Verification is independent.** `request_finish` only asks for verification; only a verifier
  returning `satisfied` counts as success, and `unknown` stays unknown — a tool returning is not the
  action working, and the action working is not the task being done.
- **Guards keep the loop alive or stop it honestly.** No-effect writes, repeats on one observation and
  revisited frames narrow the candidate set, then suspend, then end with a named reason. The full guard
  table and the invariants behind it are in [docs/design.md](docs/design.md).

## Limits and safety

- **Single environment, single writer.** The first kernel version has no multi-environment parallel
  writes, no automatic planning and no sub-loops. Each resource key has at most one active owner;
  overlapping claims conflict.
- **Adapters are yours.** The mock environment in this repository is a test double. A real adapter must
  prove its observations contain every field its preconditions and verifier rely on.
- **Bundle manifests are code.** A manifest loads project code with your privileges inside the worker;
  only run reviewed bundles inside a trusted project root. Discovery and validation are inert (no import,
  no execution, no dependency install, no network), but running a bundle is not a sandbox. Runtime
  cognition context is untrusted data, not user authorization, and a bundle's own text is never a
  permission.
- **A scaffold and the diagnostic probe are not results.** `scaffold: true` is refused by default and
  proves plumbing only; the built-in `diagnostic` bundle probes the bridge, nothing more.
- **Advisory metadata is advisory.** Declared authorizations, resource expectations, dependencies, stop
  intent and verification intent are recorded and printed as such; they grant nothing, claim nothing and
  guarantee nothing. `jev-loop bundle validate` marks enforced, advisory and not-checked separately.
- **Stopping is two-phase.** An accepted stop request is not proof that inputs were released; require a
  terminal status plus `resources_released=true`, and keep an out-of-process watchdog for real devices.
- **Liveness is not success.** A live process or a green diagnostic says nothing about the task; only
  the bundle's verifier evidence and a terminal run status do.
- **The core is synchronous; the worker is not.** `Loop.step()` executes one action at a time. The
  managed host keeps the environment moving in a worker while a cognition job is pending — it does not
  turn the synchronous kernel into an async one.
- **The bundle standard is host-neutral, not ubiquitous.** Any agent with a shell and the CLI can use it,
  and the skill teaches how; no third-party product is claimed to support the format natively.
- **Source-only distribution.** Not published to PyPI or npm; no version tags beyond the version fields
  in `pyproject.toml` and `package.json`.

## Documentation

| Document | English | 简体中文 |
|---|---|---|
| Jev Bundle Specification (manifest v2) — normative | [bundle-standard.md](spec/bundle-standard.md) | [bundle-standard.zh-CN.md](spec/bundle-standard.zh-CN.md) |
| Manifest v2 JSON Schema (machine-readable) | [bundle-manifest-v2.schema.json](spec/bundle-manifest-v2.schema.json) | — |
| Authoring a bundle (templates, offline tests, conformance) | [bundle-authoring.md](docs/bundle-authoring.md) | [bundle-authoring.zh-CN.md](docs/bundle-authoring.zh-CN.md) |
| External-engineer walkthrough (paths, safety, pitfalls) | [getting-started.html](docs/getting-started.html) | [getting-started.zh-CN.html](docs/getting-started.zh-CN.html) |
| pi adapter (optional client) | [pi-integration.md](docs/pi-integration.md) | [pi-integration.zh-CN.md](docs/pi-integration.zh-CN.md) |
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

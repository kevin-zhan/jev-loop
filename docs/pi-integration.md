# pi-jev: the optional pi client

**English** | [简体中文](pi-integration.zh-CN.md)

This document is about the **adapter**, not about the bundle format. The normative bundle contract is
[spec/bundle-standard.md](../spec/bundle-standard.md); discovery, validation and the request protocol are
identical here because the extension calls the same runtime as `jev-loop rpc` and the CLI. Read this page
for pi-specific installation, cognition delivery and lifecycle wiring.

`pi-jev` is the pi package shipped with this repository; it is not a pi fork. It runs project-local
behavior bundles as separate processes, lets pi manage their lifecycle, and handles runtime cognition
requests — the control loop does not wait for a single reply from the main agent. What pi adds over the
plain CLI is convenience, not authority: it supplies the session id as `owner_id`, heartbeats, and
delivers pending cognition jobs into the session. An agent outside pi does those three things itself
(see [the jev-loop skill's lifecycle reference](../skills/jev-loop/references/lifecycle.md)).

```text
pi session
  ├─ jev_loop tool / slash commands
  ├─ heartbeat + cognition delivery
  └─ bounded cognition result
                ↕ JSON over stdin for each management request
managed host (one worker process per run)
  ├─ append-only events.jsonl
  ├─ lease / max runtime / exclusive resource claims
  ├─ cognition broker
  └─ trusted project bundle → controller → Loop/environment/driver
```

## Install and try it

Python 3.12+ and pi are required (pi is not installed by this repository; install it using its official
installation instructions — see the [pi repository](https://github.com/earendil-works/pi)). The
extension calls `python3` by default; `JEV_LOOP_PYTHON` points it
at another interpreter. The Python source is passed to the child process through a controlled
`PYTHONPATH`, so the package does not have to be installed into a global environment.

```sh
git clone https://github.com/kevin-zhan/jev-loop.git   # private repository: access required
cd jev-loop
pi install .            # register this local package with pi (references the clone in place)
# already-open pi sessions: run /reload, or restart pi
```

Optional, unpinned remote install (the repository is private, so GitHub access is still required):

```sh
pi install git:github.com/kevin-zhan/jev-loop
pi -e https://github.com/kevin-zhan/jev-loop   # temporary try, without writing settings
```

Once installed, the package provides:

- the `jev_loop` tool: `start / list / inspect / events / update / respond / stop / release_resources`,
  plus `bundles` (list or validate a discovered bundle) and `validate_bundle`; `events` returns at most
  500 entries per page and includes `next_seq`;
- `/jev-runs`: the runs owned by the current session;
- `/jev-self-test`: an offline acceptance run of the real process (keeps progressing while a cognition
  job is pending, releases inputs when stopped);
- `/jev-stop-all`: stops all active runs of the current session after interactive confirmation;
- two skills: `jev-loop` (when to use a bundle, the request protocol, cognition and safe stop) and
  `jev-bundle-creator` (authoring a bundle from a real template).

The main skill was renamed from `pi-jev` to `jev-loop` when bundles became host-neutral, so an
already-open session keeps the old command list until it runs `/reload` (or restarts pi). `pi-jev` now
names only this optional package and extension; the `jev_loop` tool interface and `jev-loop-host rpc`
are unchanged.

## Credentials for real Jev runs

pi-jev does not read pi's credentials for Jev decisions. The extension passes the environment of
the pi process to the worker, so a bundle that reads `TYPESAFE_API_KEY` — the supported entry point
is `default_request_fn()` in `jev_loop.policies.jev` — sees it when pi was started from a process
that already has it:

```sh
uv run pi                     # the variable is already exported in this shell
uv run --env-file .env pi     # or load a private file explicitly
```

`/reload` does not re-import the key; start a new pi process. When the variable is already in the
environment, the flag is unnecessary.

Rules that apply to every bundle:

- The key belongs in the process environment, or in an explicit `request_fn` injection inside the
  bundle. Never in `task`, `inputsJson`, `bundleConfigJson`, events, snapshots or cognition
  messages: the host persists those and returns them to agents, and the worker log must not contain
  them either.
- A missing, blank or header-unsafe key fails before the first network or environment action.
  Nothing falls back to a mock, and no provider response body is copied into an error or a log.
- Check the local configuration before starting a run with the offline preflight:
  `uv run jev-loop-doctor` (prefix it with `--env-file .env` when the key lives in a private file;
  add `--bundle <manifest>` to check the manifest the run will load). Its report is a local
  statement: `authentication`, `connectivity` and `model_availability` stay `not_checked` because it
  makes no request.
- [`examples/live-files`](../examples/live-files) is the runnable reference bundle. Its workspace is
  always `run_dir/artifacts/workspace`, so two runs never share a directory, and the verifier
  writes `run_dir/artifacts/verification.json` and mirrors the verdict into the
  `controller.verification` snapshot that `inspect` returns.

## Bundle references and the trust root

The extension accepts the built-in `diagnostic` bundle, a bundle name discovered in
`<project>/.agents/jev-bundle/` (`project:<name>`, or a bare name), or a manifest path inside the
current trusted project directory. Names are resolved by the Python runtime — the extension only
pre-checks explicit paths so the error message is friendlier; the runtime is the authority. A manifest
executes Python code, so it is an explicit project-code trust boundary, not a remote prompt.

The manifest format, its fields and what is enforced versus advisory are in
[spec/bundle-standard.md](../spec/bundle-standard.md). The `entrypoint` factory signature is:

```python
def build(spec: RunSpec, services: RuntimeServices, config: dict) -> ManagedController:
    ...
```

The four controller methods:

```python
class ManagedController(Protocol):
    def run(self, stop_event: threading.Event) -> ControllerResult: ...
    def snapshot(self) -> Mapping[str, Json]: ...
    def update(self, task: Mapping[str, Json], patch: Mapping[str, Json]) -> None: ...
    def request_stop(self, reason: str) -> None: ...
```

- `run` executes the behavior loop on the engine thread; the returned `ControllerResult.resources_released`
  may be `true` only when the controller has confirmed that all external inputs are released. Uncertain
  paths such as force-killing a child process must return `false`.
- The other three methods may be called from the coordinator thread and must be thread-safe.
- `request_stop` must release held inputs synchronously and must not wait for a model or the network.
- External devices also need an independent watchdog, because no cleanup is guaranteed when the Python
  process is `SIGKILL`ed.
- `snapshot` carries only a bounded, JSON-serializable state summary; full evidence belongs in the run's
  `artifacts/`.

An existing `Loop` can be hosted directly with `LoopController`; see the discoverable reference
[`.agents/jev-bundle/offline-switchboard`](../.agents/jev-bundle/offline-switchboard) (manifest v2) and
the legacy-format [`examples/bundles/switchboard`](../examples/bundles/switchboard). The synchronous
`Loop.step()` still executes one action at a time: if the environment must keep moving during a slow
model call, the device driver/watchdog has to run on its own thread or process and the controller only
coordinates it.

## Runtime cognition

A bundle opens a job through the broker; the call returns immediately:

```python
job_id = services.cognition.request(
    "Existing candidates lean toward food and drink; which search directions should the next round add?",
    context={"candidate_summary": summary},
    output_schema={"type": "object", "required": ["search_terms"]},
    resource_keys=("research-plan",),
    deadline_seconds=90,
    dedupe_key="diversify-search",
)
```

The controller can keep running and check the state through `services.cognition.get(job_id)`. The pi
extension delivers pending jobs to the owner session as clearly marked custom messages; the main agent
submits a matching run/job/version result with `jev_loop respond`. Late, duplicate, version-mismatched,
schema-violating results, or results for an already stopped run are rejected.

The current dependency-free validator implements **limited semantics** for the following keywords only.
Bundles must not assume any other keyword is enforced, and must not treat this as full Draft 2020-12:

| Keyword | Actual behavior and limits |
|---|---|
| `type` | `null / boolean / integer / number / string / array / object`; neither `integer` nor `number` accepts `bool` |
| `enum` / `const` | compared with Python equality, so `1` and `true` are not distinguished; `const` is not checked when absent |
| `required` / `properties` | only applied when the value is an object; sub-schemas inside `properties` are validated recursively |
| `additionalProperties` | **only the literal `false` is recognized** (extra fields are rejected); the sub-schema object form is **not executed** |
| `items` / `minItems` / `maxItems` | only applied when the value is an array |
| `minLength` / `maxLength` | only applied when the value is a string |
| `minimum` / `maximum` | only applied to non-`bool` numbers; an empty schema `{}` passes everything |

Each run may have at most 8 pending jobs at a time, and question/context/schema/result/evidence each
have a hard size limit. Results only enter the broker; a bundle still has to validate its world
preconditions and resource versions before adopting them.

The built-in `diagnostic` bundle is a real probe of these semantics: while a cognition job is pending it
maintains a `forward` input while independently incrementing world ticks and decision counts, and
releases the input when a result arrives or any stop signal appears. It does not access web pages and
cannot serve as evidence that a user task was completed.

## Lifecycle and safety semantics

- **Idempotent start**: `owner session + project root + idempotency key` maps to exactly one run; a
  repeated call does not start another controller.
- **Exclusive resources**: `resourceKeys` (for example `browser:research-profile`) are declared
  atomically inside the host and cannot be held twice by active runs.
- **Session owner**: management commands and cognition replies must match the pi session ID that created
  the run. Switching sessions does not inherit control.
- **Lease**: non-detached runs default to 30 seconds; the extension heartbeats roughly every 3 seconds.
  If pi disappears or the session switches, the worker requests a stop and releases its inputs.
- **Detached**: can only be created with direct confirmation in the TUI, and is still bounded by
  `maxRuntimeSeconds`.
- **Two-phase stop**: `accepted` only means the worker received the stop intent; terminal status plus
  `resources_released=true` is what proves the host received the controller's synchronous release
  confirmation. `confirmed` only means it waited for terminal.
- **Timeouts**: a run has a hard maximum runtime and cognition jobs have their own deadlines; neither
  waits forever.
- **Single-writer ownership**: the agent must not operate the same `resourceKeys` directly while a run is
  active. pi-jev can only constrain runs registered through the host; it cannot stop an external program
  from bypassing it.
- **Crash isolation**: when a worker disappears without a confirmable release, the resource claim stays
  quarantined and is not taken over automatically by the next run. Only after the user has independently
  verified in the TUI that the device inputs are released may `release_resources` clear the claim; an
  out-of-process watchdog remains the first line of protection.
- **Session history is not the run's truth**: the event log is the source of host state, and the pi
  session only stores a delivery cursor. Compaction and reloads do not replay actions.

The default data directory is `~/.local/state/jev-loop/`, overridable with `JEV_LOOP_HOME`. Each run
contains:

```text
runs/<run-id>/
  spec.json          # 0600
  events.jsonl       # append-only source of truth
  status.json        # atomic projection/cache
  worker.log
  inbox/ acks/       # atomic command exchange
  artifacts/         # bundle-owned evidence
```

Task content travels through the management process's stdin, not through shell arguments. Directories
and state files are created with user-private permissions; a bundle must still avoid writing cookies,
tokens or raw private data into events and snapshots.

## Explicitly not included

- No Xiaohongshu, browser, phone or game-site adapters; those have to be separately reviewed and
  accepted project bundles.
- No authority over the bundle format: the extension does not define bundles, and a bundle never needs
  pi to be created, discovered, validated or run.
- The main agent is not turned into a button-press decision maker; button candidates are still chosen by
  the bundle's Jev policy.
- No MCP server; crossing hosts requires an adapter layered over the same host API.
- No guarantee that an arbitrary third-party driver can park safely; bundles must implement synchronous
  release and a device-side watchdog.
- No paid TypeSafe or other model calls in the offline tests.

## Verification

```sh
uv sync
uv run pytest
uv run ruff check .
uv run jev-loop-doctor --offline   # local configuration preflight; no network request
npm test
node --experimental-strip-types --check extensions/pi-jev.ts
```

Test coverage includes: progressing while a cognition job is pending, completing after a reply, releasing
inputs on stop, owner lease expiry, idempotent start, versioned updates, rejecting late replies, resource
exclusivity and crash quarantine, loading a project bundle, and loading the package with real pi RPC to
run `/jev-runs` and `/jev-self-test` (without calling a model).

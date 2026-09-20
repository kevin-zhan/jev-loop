# pi-jev: a managed host for Jev Loop

**English** | [简体中文](pi-integration.zh-CN.md)

`pi-jev` is the pi package shipped with this repository; it is not a pi fork. It runs project-local
behavior bundles as separate processes, lets pi manage their lifecycle, and handles runtime cognition
requests — the control loop does not wait for a single reply from the main agent.

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

- the `jev_loop` tool: `start / list / inspect / events / update / respond / stop / release_resources`;
  `events` returns at most 500 entries per page and includes `next_seq`;
- `/jev-runs`: the runs owned by the current session;
- `/jev-self-test`: an offline acceptance run of the real process (keeps progressing while a cognition
  job is pending, releases inputs when stopped);
- `/jev-stop-all`: stops all active runs of the current session after interactive confirmation;
- the `pi-jev` skill: teaches the agent when to use the extension, how to handle cognition and how to
  verify a run.

## Bundle manifest

The extension accepts only the built-in `diagnostic` bundle or a manifest inside the current trusted
project directory. A manifest executes Python code, so it is an explicit project-code trust boundary,
not a remote prompt.

```json
{
  "schema_version": 1,
  "entrypoint": "controller:build",
  "python_path": ".",
  "config": {"adapter": "project-specific settings"}
}
```

The `entrypoint` factory signature is:

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

An existing `Loop` can be hosted directly with `LoopController`; see
[`examples/bundles/switchboard`](../examples/bundles/switchboard). The synchronous `Loop.step()` still
executes one action at a time: if the environment must keep moving during a slow model call, the device
driver/watchdog has to run on its own thread or process and the controller only coordinates it.

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
npm test
node --experimental-strip-types --check extensions/pi-jev.ts
```

Test coverage includes: progressing while a cognition job is pending, completing after a reply, releasing
inputs on stop, owner lease expiry, idempotent start, versioned updates, rejecting late replies, resource
exclusivity and crash quarantine, loading a project bundle, and loading the package with real pi RPC to
run `/jev-runs` and `/jev-self-test` (without calling a model).

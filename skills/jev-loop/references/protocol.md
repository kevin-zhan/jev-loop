# The host request protocol

One request object on stdin, one response object on stdout:

```sh
printf '%s' '{"action":"inspect","owner_id":"my-session","run_id":"run_..."}' | jev-loop rpc
# identical: jev-loop-host rpc
```

A request is served by the host state directory (`$JEV_LOOP_HOME`, default
`~/.local/state/jev-loop`). Failures answer `{"ok":false,"error":"..."}`; the process exit
code is non-zero. Task content always travels through stdin, never through shell arguments.

## Actions

| Action | Required fields | Notes |
|---|---|---|
| `start` | `owner_id`, `idempotency_key`, `project_root`, `bundle`, `task.goal` | returns immediately with the run; identical `owner_id + project_root + idempotency_key` returns the same run |
| `list` | `owner_id` | the runs owned by that id |
| `inspect` | `owner_id`, `run_id` | current status, controller snapshot, pending cognition |
| `events` | `owner_id`, `run_id` | `after` (cursor) and `limit` (1..500); returns `next_seq` and `has_more` |
| `heartbeat` | `owner_id`, `run_id` | renews the owner lease of a non-detached run |
| `update` | `owner_id`, `run_id`, `expected_config_version`, `task_patch` | `task_patch` accepts `goal`, `inputs`, `constraints`, `success_criteria`, `authorization`; a stale version is rejected |
| `respond` | `owner_id`, `run_id`, `job_id`, `expected_job_version`, `result` | answers one pending cognition job; `evidence` is optional |
| `stop` | `owner_id`, `run_id` | `accepted` is not confirmation; read `confirmed` and then require terminal status plus `resources_released: true` |
| `release_resources` | `owner_id`, `run_id`, `confirmed: true` | only for a terminal run, and only after the user or operator explicitly confirmed the external inputs were independently verified as released |
| `bundle_list` | `project_root` | discovery; reads manifests, executes nothing |
| `bundle_show` | `project_root`, `bundle` | metadata, declarations, warnings |
| `bundle_validate` | `project_root`, `bundle` | static contract report; read `validation_ok` (see below) |

`start` fields:

| Field | Meaning |
|---|---|
| `project_root` | the trust root; a bundle must resolve inside it |
| `bundle` | `diagnostic`, `project:<name>`, a discovered name, or a manifest path inside the project root |
| `task.goal` | required, non-empty |
| `task.inputs` | bundle-specific inputs; validated against the manifest's `inputs_schema` when it declares one |
| `task.constraints`, `task.success_criteria`, `task.authorization` | recorded with the run and shown to the bundle |
| `resource_keys` | exclusive external inputs (for example `browser:profile`, `device:serial`); a key held by an active or quarantined run is refused |
| `bundle_config` | overrides for the manifest's `config`; validated against `config_schema` when declared |
| `detached` | `true` disables the owner lease and must be an explicit, interactive decision |
| `lease_seconds` | 10..300, default 30, non-detached only |
| `max_runtime_seconds` | 1..86400, default 300: a hard bound that stops the run |
| `stop_grace_seconds` | 0.1..30, default 3: how long the worker waits for the controller to exit after a stop |

### What `ok` means, and where the verdict lives

Three fields with three different meanings — do not collapse them:

| Field | Meaning |
|---|---|
| `ok` | the request itself was processed (transport/request layer) |
| `validation_ok` | `bundle_validate` only: the static contract passed |
| `validation.ok` | the verdict inside the full report, alongside `errors`, `warnings`, `advisory` and `not_checked` |

So a `bundle_validate` answer with `"ok": true, "validation_ok": false` is a *successfully served
request about a bundle that fails the contract*: read `validation.errors` for the reason. An
agent that only checks the outer `ok` will misread a broken bundle as fine. The same report is
also what `start` uses: a bundle that fails static validation is refused before any run,
journal, claim or worker exists, so `bundle_validate` is a faithful pre-check for `start`.

`confirmed: true` on `release_resources` is a caller **attestation**, not a server-side proof
that the external state is safe: the host records the claim. Never set it on your own
initiative, never use it as a retry, and require an explicit user/operator confirmation that a
human verified the device, browser or process was released. The pi adapter keeps its
interactive confirmation for the same reason.

`allow_scaffold` is a **test/plumbing switch only**. It is never set on a user's behalf; a run
started with it records `bundle_scaffold: true` in its journal and is never evidence that a
user's goal was implemented or verified.

Response fields worth reading: `run.status` (`starting`, `running`, `stopping`, `succeeded`,
`failed`, `cancelled`, `expired`), `run.resources_released`, `run.worker_alive`,
`run.lease_deadline`, `run.controller` (the bundle's bounded snapshot), `run.pending_cognition`,
`run.output`, `run.last_error`.

## Error semantics

- A rejected request is a normal answer (`ok:false`) with a named reason: stale config
  version, stale job version, wrong owner, already-terminal run, conflicting resource claim,
  declared-schema violation, scaffold start, unusable bundle entry.
- An unresolved or ambiguous bundle reference is refused before anything runs, listing the
  candidates or the exact reason.
- `pending`/`unknown` operation receipts inside a bundle mean query, never resend.

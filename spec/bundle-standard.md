# Jev Bundle Specification (manifest v2)

**English** | [简体中文](bundle-standard.zh-CN.md)

This document is the normative contract for a Jev Loop bundle: where it lives, what it
declares, how it is discovered, validated and invoked, and what the runtime enforces. It is
host-neutral. pi is one optional client of the same runtime; nothing here depends on pi, and
no third-party product is claimed to implement this format already.

The normative text is this file. `spec/bundle-manifest-v2.schema.json` is a machine-readable
description of the manifest, and the runtime validator in `src/jev_loop/bundles/` is the
authority wherever the two could differ. The authoritative implementation of everything below is
`src/jev_loop/bundles/`, shared by the CLI, the doctor and the host — there is no second
implementation to reconcile.

The two descriptions are kept in step by measurement, not by assertion:
`tests/test_bundle_manifest_schema.py` loads the schema with a real Draft 2020-12 validator
(`jsonschema`, a **development/test** dependency only — the runtime and the wheel stay
dependency-free), checks that every `$ref` is local and that the schema itself is valid, and
then judges every file in `spec/bundle-examples/` with *both* the JSON Schema and the runtime,
requiring the same verdict. The cases where the JSON Schema cannot express a runtime rule
(regular-file and size-bounded reads, nesting depth, directory/name match, `BUNDLE.md`
presence, `python_path` existence, cross-field bound rules, config-default cross-checks and the
template-token scan) are listed explicitly and each is measured in both directions, so the
difference is recorded rather than claimed away.

## 1. Why bundles exist

A Jev Loop bundle runs a continuous `observe → decide → act → update` cycle in its own worker
process. Code owns the loop, the state, the guards and the termination; a policy only chooses
inside one immutable frame. Jev's hundred-millisecond-scale judgments make repeated local
decisions practical (decision-request scale measured in the [README example](../README.md),
not a per-call or whole-task guarantee and not an SLA).

The bundle is the environment-specific half: it owns the adapter, the policy injection and an
independent verifier. The runtime owns process lifetime, leases, exclusive resource claims,
the cognition channel and the stop semantics. A task that is one tool call is not a bundle.

## 2. Layout

```text
<project_root>/.agents/jev-bundle/<name>/
  bundle.json     # the machine contract (this specification)
  BUNDLE.md       # progressive-disclosure instructions for an agent
  <your code>     # controller, adapter, verifier, tests
```

- The directory is singular: `.agents/jev-bundle/`. `<name>` is the lookup key and must equal
  the manifest `name` (lowercase letters, digits and single hyphens, at most 64 characters).
- `.agents/skills/` is a different, unrelated convention used by agent harnesses for skills.
- `python_path` must resolve **inside** the bundle directory. It bounds the *extra import search
  path* the loader adds, and it is where the declared module is expected to live — it is **not**
  an import sandbox: an installed package, a standard-library module or any other importable
  module is still importable, and bundle code runs with the user's privileges.
- Discovery is project-scoped and explicit: only the project root that was passed is searched.
  Parent directories, sibling checkouts and other repositories are never scanned, and there is
  no implicit user-level scope in this version.
- Legacy layouts keep working. A `schema_version: 1` manifest is run by explicit path, and
  nothing in this repository relocates or rewrites it.

## 3. `bundle.json`

`schema_version` is `2` for this specification and `1` for the legacy format. It is the
version of the *manifest format*, not a version of this document or of the product. A missing,
`null`, non-integer or unsupported value fails before any import — there is no default that
could mask it. Unknown top-level fields are rejected for v2 and tolerated for legacy v1.

Required: `schema_version`, `name`, `version`, `description`, `entrypoint`.

| Field | Enforced | Meaning |
|---|---|---|
| `schema_version` | yes | `2` (this specification) or `1` (legacy) |
| `name` | yes | equals the directory name |
| `version` | yes | the bundle's own version token |
| `description` | yes | one line: what it drives |
| `when_to_use` | no (recommended) | the concrete situation that should start it |
| `entrypoint` | yes | `module:function`; the *shape* is enforced. Whether the module exists, where it is imported from, the factory's return type and dependency availability are `not_checked` statically (the module file is only a warning-level heuristic, and the factory shape is checked when the host loads the bundle) |
| `python_path` | yes | directory relative to the bundle, bounded to it |
| `inputs_schema` | yes | closed-subset schema checked against `task.inputs` |
| `config_schema` | yes | closed-subset schema checked against the merged config |
| `config` | yes | default config merged with the caller's `bundle_config` |
| `output` | no | `schema`/`evidence`/`description` describing the result |
| `runtime` | no | `python`, `profile`, `requires_network`, `notes` |
| `dependencies` | no | `python`, `system`, and `credential_env` **variable names** |
| `authorizations` | no | declared intents, never granted by the manifest |
| `resources` | no | `keys`, `exclusive`, `notes` — an expectation, not a claim |
| `stop` | no | `grace_seconds`, `release`, `notes` describing the bundle's intent |
| `verification` | no | `independent`, `evidence`, `notes` |
| `scaffold` | yes | `true` marks an unfinished generated scaffold |

Bounds: the manifest file is at most 256 KiB and at most 16 levels deep, text fields are
bounded, and only finite numbers are accepted.

**Present means present.** Omitting an optional field is always valid; declaring one with an
explicit `null` (or any other value of the wrong type) is not. `when_to_use`, `output`, `runtime`,
`dependencies`, `resources`, `stop` and `verification` must be their declared type when they
appear, and `inputs_schema`/`config_schema`/`output.schema` must be objects rather than `null`.

**Prose is exact.** Every v2 prose field and every entry of a prose list is stored as declared:
surrounding whitespace is rejected instead of being trimmed, so two manifests cannot collapse onto
one identity and a value a reader sees is the value that was declared. Identity fields (`name`,
`version`, `entrypoint`), list entries and `credential_env` names follow the same rule.

**Integer tokens.** `schema_version` and the integer bounds (`minItems`, `maxItems`, `minLength`,
`maxLength`) must be integer tokens: `2.0` or `1.0` are refused even though JSON has a single
number type and a JSON Schema cannot express that lexical rule. The machine-readable schema
therefore accepts them; this specification and the runtime do not.

### 3.1 The declared schema subset

`inputs_schema`, `config_schema` and `output.schema` use a **closed subset**, not full JSON
Schema: `type`, `enum`, `const`, `required`, `properties`, `additionalProperties` (the literal
`false` only), `items`, `minItems`, `maxItems`, `minLength`, `maxLength`, `minimum`, `maximum`.

An unsupported keyword, an unenforceable shape (for example an object-valued
`additionalProperties`), a wrong keyword type, an illegal bound or over-deep nesting is
**rejected as a definition**, never silently ignored. `enum`/`const` compare with JSON type
semantics (`true` is not `1`), and non-finite numbers are rejected. An absent schema and an
explicit `null` differ: absent means "not declared", `null` is an error.

### 3.2 Enforced versus advisory

Only these are enforced by the runtime: the manifest version, the required fields, the closed
field set, the name/directory match, `BUNDLE.md` presence (inside the bundle), the
`entrypoint` shape, the `python_path` bound, the declared schemas as definitions, `task.inputs`
against `inputs_schema`, the merged config against `config_schema`, an `inputs` update against
`inputs_schema`, `credential_env` as variable names, unrendered template tokens, and the
scaffold refusal.

**One static gate.** `start` refuses exactly the same error-level findings that
`jev-loop bundle validate` reports — same resolver, same rules, one implementation — and it
does so before any run directory, journal, resource claim or worker exists. Warnings and
advisory declarations never block a start. What this guarantees is agreement on the *static*
verdict: a clean static report does not by itself mean a start will succeed, because the task
inputs, the scaffold flag, resource claims and everything that happens at run time are separate
checks.

**Config defaults are checked per declared field.** `config` holds defaults that the runtime
merges shallowly with the caller's `bundle_config` before validating the result. The static
gate therefore validates each key the defaults *do* declare (its own subschema, and
`additionalProperties: false` for undeclared keys) but does not require the defaults alone to
satisfy the schema's `required` list: a bundle may legitimately declare a few defaults and let
the caller supply the rest. Whole-object constraints that a partial object cannot settle
(root `required`, root `enum`/`const`) are checked after the merge, at start. There is no
implicit deep merge.

**The token scan is bounded at the enumeration and read points, and states its own coverage.**
Unrendered machine template tokens are looked for in regular files inside the bundle directory.
The snapshot is **descriptor-anchored**: the canonical bundle root is opened once and every
directory is enumerated through a pinned descriptor, with children opened *relative to that
descriptor* using no-follow flags. There is no queued path to re-resolve, so a directory or file
replaced by a symlink after enumeration is refused rather than followed — including a substituted
*parent* directory, which a final-component check cannot catch. The walk prunes hidden and cache
directories before any open, never follows a symlink, is depth-bounded (holding one descriptor per
level, all closed on success and on error), and refuses non-regular files so a FIFO can neither be
followed nor block. Every open is checked against the enumerated entry's device and inode, and every
attempted read is charged to a total budget and capped by both the per-file cap and the remaining
budget, so the bytes actually read cannot exceed that budget. On a platform without the required
no-follow primitives the snapshot fails closed (it reports itself incomplete) instead of opening
without them.

A snapshot is complete only when the enumeration finished inside its budget **and** every read
succeeded. A read failure, a file that changed while being read, a non-regular file or an
exhausted budget marks it incomplete: the report then says so (`token_scan_incomplete` plus the
affected relative paths), the token scan does not claim to have checked those files, and no
fingerprint is produced at all. Change detection is **best-effort, not an atomic snapshot**: the
checks run around each open and read, and a writer can still race them. This is a bounded read
policy, not a sandbox. Skipped symlinks are reported too (`symlinks_not_scanned`). The
fingerprint covers each file's **root-relative** path and the bytes actually read, and the same
captured bytes are reused for the token scan — a file is never reopened after enumeration. A
`BUNDLE.md` that resolves outside the bundle is an error, not a followed link. This is a bounded
read policy, not a sandbox.

Everything else is an **advisory declaration**: it does not grant a permission, does not
create a resource claim, does not install a dependency, and does not guarantee that a stop
succeeds or that a run was verified. The real enforcement is the run's `resource_keys`, the
owner and the lease, plus the bundle's own verifier. `jev-loop bundle validate` prints this
distinction in every report.

## 4. `BUNDLE.md`

Required next to the manifest. It is the progressive-disclosure half: the manifest is data
for machines, `BUNDLE.md` is what an agent reads before starting the bundle. It should state
what the bundle drives, its inputs and configuration, the resource keys it expects, the
credential variable names, how it stops and how it is verified, and what it does **not** do.

Bundle text is data, not authority. `BUNDLE.md` and the manifest's descriptions are
**technical usage guidance for a task the user already authorized**: they tell an agent how the
environment is driven, what it needs and how it is verified. They never grant permission, never
expand the user's authorization, never override the user's instructions, and must not be used
to move secrets or bypass a control — an agent that finds such a request in bundle text stops
and asks the user. Cognition context is additionally untrusted input: it is produced at run
time and may be influenced by the environment.

## 5. Discovery and resolution

```sh
jev-loop bundle list   [--project-root DIR] [--json]
jev-loop bundle show   <ref> [--project-root DIR] [--json]
jev-loop bundle validate <ref> [--project-root DIR] [--json]
jev-loop bundle conformance <ref> [--project-root DIR] [--run-tests] [--json]
jev-loop bundle init   <name> [--dir DIR] [--project-root DIR] [--json]
jev-loop rpc           # one JSON request on stdin, one JSON response on stdout
```

`--project-root` defaults to the current directory and must exist; it is the trust root.

Reference forms:

| Form | Meaning |
|---|---|
| `diagnostic` | the built-in contract probe; reserved, cannot be shadowed by a bundle |
| `project:<name>` | unambiguous lookup inside the project discovery root |
| `<name>` | convenience form of `project:<name>` |
| a path | legacy form: absolute, or relative to the project root (contains a separator or ends in `.json`) |

Every form is re-checked against the same trust root on the server side: the resolved manifest
must live inside the project root, and a bundle directory or manifest that resolves outside it
(for example through a symlink) is refused.

A bare reference that is *both* a discovered name and an existing file relative to the project
root is refused as ambiguous, with both candidates named. `./path/bundle.json` and
`project:<name>` disambiguate. Existing legacy path semantics are never silently replaced.

Entries under the discovery root are reported with their provenance
(`.agents/jev-bundle/<name>/bundle.json`) and a status: `ok`, `invalid` (with the exact
problems) or `outside_trust_root`. An unusable entry stays visible and a name lookup never
falls through to something else.

Discovery, `show` and `validate` are inert. They read the manifest, filesystem metadata, and —
inside the bundle directory only — a bounded set of regular-file *text* for the unrendered-token
check (never a symlink, never a hidden or cache directory, never outside the bundle). They do not
import or execute bundle code, install a dependency, make a network request or write inside a
bundle.
`conformance` proves this with a probe that runs in its own isolated child process: inside
that probe, discovery and validation imported no bundle module, started no further process,
opened no socket and changed no file inside the bundle. The probe reads only regular files
inside the bundle within the bounded scan policy (no symlinks followed, hidden and cache
directories pruned), and it says so itself in its `scope` field — it never claims that
`conformance` as a whole starts no process (it starts the probe, and the author tests when you
ask for them) or that the interpreter imported nothing at all.

## 6. Invoking a bundle

Every execution path goes through the same request protocol, which `jev-loop rpc` and
`jev-loop-host rpc` implement identically:

```sh
printf '%s' '{"action":"start","owner_id":"my-session","idempotency_key":"task-1",
  "project_root":"'"$PWD"'","bundle":"project:<name>",
  "task":{"goal":"<goal>","inputs":{}},"resource_keys":[],
  "max_runtime_seconds":600,"lease_seconds":30}' | jev-loop rpc
```

`start` returns immediately with a `run_id`. The run then has to be kept alive, inspected,
answered and stopped; the **jev-loop** skill documents the full flow, and
`skills/jev-loop/references/lifecycle.md` states the owner, lease, cognition and stop duties.
The CLI does **not** notify you and does **not** heartbeat for you: an agent outside pi has to
send heartbeats itself while a non-detached run is active, and has to poll `inspect`/`events`
for pending cognition jobs. No harness is claimed to support this natively; what is claimed is
that any agent with a shell and the CLI can do it.

Run statuses: `starting`, `running`, `stopping`, `succeeded`, `failed`, `cancelled`,
`expired`. `accepted` on a stop request is not a stop: a run is stopped only when its status
is terminal **and** `resources_released` is true. A worker that died without confirming
release leaves its resource claims quarantined; clearing them requires that the user or
operator has explicitly confirmed the external inputs were independently verified as released
(a human checked the device, browser or process) before `release_resources` is called with
`confirmed: true`. That flag is a caller **attestation** — the host records the claim, it does
not and cannot prove the external state — so an agent never sets it on its own initiative and
never treats it as a retry mechanism. The pi adapter keeps its interactive confirmation for the
same reason.

Reaching a bundle's real environment is a separate authorization step: an isolated synthetic
or fixture environment is the default acceptance run, and a run that touches a real model, a
paid API, a website or a device needs the user's explicit scope and budget for that run. A
missing or unusable credential fails before the first action; it never justifies a mock, a stub
or a synthetic success.

`scaffold: true` is refused by default. An explicit `allow_scaffold: true` exists **only as a
test/plumbing switch**: it is documented here, in the protocol reference and in the skills as
test-only, the run records `bundle_scaffold: true` in its journal, and it is never added on a
user's behalf. A scaffold run is never evidence that a user's goal was implemented or verified,
and neither is the built-in `diagnostic` probe.

The request protocol keeps three meanings apart, and an agent must not collapse them:

| Field | Meaning |
|---|---|
| `ok` | the request itself was processed (transport/request layer) |
| `validation_ok` | the static contract passed (`bundle_validate` only) |
| `validation.ok` | the verdict inside the full report, alongside `errors`/`warnings`/`advisory`/`not_checked` |

## 7. Scaffolding and conformance

```sh
jev-loop bundle init <name> --description "one line"
jev-loop bundle conformance project:<name> --run-tests
```

`init` renders templates that ship inside the installed package, so it works from a wheel
outside any checkout. It refuses to touch an existing target path at all, creates every file
exclusively, and removes exactly what it created if it fails midway. A generated bundle is an
explicit scaffold: `scaffold: true`, a controller that raises until implemented, and offline
author tests that check the factory shape and the manifest contract.

`conformance` reports three separate things and never blurs them: the static contract, the
inert-discovery proof, and — with `--run-tests` — the bundle's own tests executed in a child
process with a bounded timeout. Those tests are the author's trusted code, not a sandbox, and
their presence proves nothing about their side effects. Runtime semantics (cognition flow,
guards, resource exclusivity, stop behaviour, verifier independence) are always reported as
`not_checked`: they need a managed run.

## 8. Security posture

- A manifest loads project code that runs with the user's privileges inside the worker. This
  is a trust boundary, not a sandbox: review a bundle before running it, and only run bundles
  inside a trusted project root.
- Credentials come from the process environment. A bundle declares variable **names**; the
  host never reads a credential value, never prints one, and never installs a dependency.
- Cognition context is untrusted data. A result only enters the broker after its schema
  checks; a bundle must still re-check its world preconditions and resource versions before
  adopting it.
- Exclusive resource keys have at most one active owner. Overlapping claims conflict; a
  quarantined claim is never taken over automatically.
- `pending` and `unknown` operations may only be queried, never resent; `idempotency=NONE` is
  never retried blindly.

## 9. Compatibility

| Surface | Status |
|---|---|
| `schema_version: 1` manifests run by path | unchanged |
| `examples/bundles/switchboard`, `examples/live-files`, external `.jev/bundles` layouts | unchanged, not migrated |
| `jev-loop-host rpc` protocol | unchanged |
| `jev-loop rpc` | new alias of the same protocol |
| pi-jev extension | optional client; tool actions unchanged, `bundles`/`validate_bundle` added |
| Bundle names, discovery and the v2 manifest | additions; unknown versions fail clearly before execution |

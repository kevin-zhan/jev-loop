# The bundle contract (manifest v2)

A bundle lives in `<project_root>/.agents/jev-bundle/<name>/` and contains `bundle.json`
(the machine contract) and `BUNDLE.md` (instructions for an agent). `bundle.json` is data:
reading it imports nothing and executes nothing.

```json
{
  "schema_version": 2,
  "name": "<same as the directory name>",
  "version": "0.1.0",
  "description": "one line: what this bundle drives",
  "when_to_use": "the concrete situation that should start this bundle",
  "entrypoint": "bundle_controller:build",
  "python_path": ".",
  "inputs_schema": {"type": "object", "properties": {}, "additionalProperties": false},
  "config_schema": {"type": "object", "properties": {}},
  "config": {},
  "output": {"description": "what the caller should read to judge the run"},
  "runtime": {"python": ">=3.12", "profile": "single-process", "requires_network": false},
  "dependencies": {"python": [], "system": [], "credential_env": ["TYPESAFE_API_KEY"]},
  "authorizations": ["browser.navigate"],
  "resources": {"keys": ["browser:<profile>"], "exclusive": true},
  "stop": {"grace_seconds": 3.0, "release": "synchronous release of held inputs"},
  "verification": {"independent": true, "notes": "who verifies and how"},
  "scaffold": false
}
```

## Enforced by the runtime

| Field | Rule |
|---|---|
| `schema_version` | `1` (legacy) or `2` (this specification). Missing, null, a non-integer or an unknown value fails before any import |
| `name` | lowercase letters, digits and single hyphens, ≤64 chars, equal to the directory name |
| `version` | non-empty version token |
| `description` | non-empty, ≤1024 chars |
| `entrypoint` | `module:function` with dotted module names |
| `python_path` | must exist; for v2 it must resolve inside the bundle directory. It bounds the extra import search path — it is not an import sandbox, and installed modules stay importable |
| unknown fields | rejected for v2 (no silent typos); tolerated for legacy v1 |
| `inputs_schema` | validated as a schema *definition*; `task.inputs` is checked against it **before** a run is created, and an `update` that changes `inputs` is checked the same way |
| `config_schema` | validated as a definition; the merged `config` + `bundle_config` is checked before the run starts |
| `dependencies.credential_env` | environment **variable names** only |
| `BUNDLE.md` | required next to the manifest |
| `scaffold` | `true` makes the host refuse a start by name or path unless `allow_scaffold: true` is passed explicitly |
| template tokens | an unrendered machine token (`__BUNDLE_NAME__` and friends) is a validation error; prose TODOs are not |

The declared schema subset: `type`, `enum`, `const`, `required`, `properties`,
`additionalProperties: false`, `items`, `minItems`, `maxItems`, `minLength`, `maxLength`,
`minimum`, `maximum`. This is a deliberately closed subset, not full JSON Schema: an
unsupported keyword or an unenforceable shape (for example an object-valued
`additionalProperties`) is rejected instead of being silently ignored. `enum`/`const`
compare with JSON type semantics (`true` is not `1`), and non-finite numbers are rejected.

## Present means present, and prose is exact

Omitting an optional field is valid; declaring it as `null` is not. `when_to_use`, `output`,
`runtime`, `dependencies`, `resources`, `stop` and `verification` must be their declared type when
they appear, and the declared schemas must be objects rather than `null`.

Prose fields and list entries are stored exactly as written: surrounding whitespace is rejected
rather than trimmed (identity fields, list entries and `credential_env` names too). `schema_version`
and the integer bounds must be integer tokens — `2.0` and `minItems: 1.0` are refused even though a
JSON Schema cannot express that lexical rule.

## Config defaults and the static gate

`config` is a set of **defaults**, merged shallowly with the caller's `bundle_config` before the
merged object is validated. The static check therefore validates each key your defaults *do*
declare (its own subschema, plus `additionalProperties: false` for undeclared keys) and does not
require the defaults alone to satisfy `required` — declaring a few defaults and letting the
caller supply the rest is fine. Whole-object constraints (root `required`, root `enum`/`const`)
are settled after the merge, at start. There is no implicit deep merge: if you want nested
defaults, declare the whole nested object.

`start` refuses exactly the same error-level findings this report shows, before any run or
resource claim exists — one static gate, so a pre-check with `bundle validate` (or the
`bundle_validate` RPC) is faithful.

The unrendered-token scan is bounded at its enumeration and read points: it never follows
symlinks (so it cannot read outside your bundle), prunes hidden/cache directories, anchors the walk
to pinned directory descriptors (a substituted directory or file cannot be followed) and caps every
read. When it hits a budget, cannot read a file, or sees a file
change while reading it, the report says `token_scan_incomplete` (with the affected relative
paths) and produces **no** fingerprint instead of implying a complete check; skipped symlinks
appear as `symlinks_not_scanned`. Keep bundle files ordinary regular files (a FIFO or link is
skipped and reported) and do not make `BUNDLE.md` a link to something outside the bundle.

## Advisory declarations (recorded, enforced nowhere)

`authorizations`, `resources`, `dependencies`, `stop`, `verification` and `output` document
intent. They do **not** grant a permission, do **not** create a resource claim, do **not**
install a dependency, and do **not** guarantee that a stop succeeds or that a run was
verified. The real enforcement is the runtime's `resource_keys`, owner and lease, plus the
bundle's own verifier. `jev-loop bundle validate` prints this distinction explicitly.

## Not checked statically

The factory/controller shape (only checked when the host loads the bundle), runtime
semantics, dependency availability, credential presence, and any live network or device
interaction. Static validation never claims those.

## Legacy `schema_version: 1`

`{ "schema_version": 1, "entrypoint": "controller:build", "python_path": ".", "config": {} }`
keeps working by explicit path. It declares no metadata and no typed contract, so the host
cannot reject a malformed task before starting a worker. Legacy manifests are never
relocated or rewritten for you; adopting v2 is a deliberate authoring step.

"""The machine-readable manifest schema must agree with the runtime validator.

``spec/bundle-manifest-v2.schema.json`` is a real Draft 2020-12 document, and this module checks
it with a real validator (``jsonschema`` is a **development/test** dependency; the runtime and
the wheel stay dependency-free).  Every reference in the schema is local, and nothing here
fetches a remote schema.

The two descriptions are compared on the shipped corpus under ``spec/bundle-examples/``:
every file is judged by the JSON Schema *and* by the runtime, and the two verdicts must match.
That is the actual evidence — a field-set comparison alone would not have been.

They are deliberately **not** fully equivalent, and the difference is recorded here rather than
claimed away.  Constraints the runtime enforces that JSON Schema cannot express:

- the manifest file must be a regular file of at most ``MAX_MANIFEST_BYTES`` bytes, read with a
  bound, and must be valid UTF-8 JSON;
- the document may not nest deeper than ``MAX_MANIFEST_DEPTH`` levels;
- the manifest's directory name must equal ``name``, and ``BUNDLE.md`` must exist next to it
  inside the bundle;
- ``python_path`` must exist and resolve inside the bundle directory;
- declared contract schemas are validated as *definitions* (unsupported keywords, illegal
  bounds, over-deep nesting) and cross-checked against ``config`` defaults;
- identity fields may not carry surrounding whitespace, and ``credential_env`` entries are
  variable names that are never echoed;
- the unrendered-template-token scan of the bundle directory.

Conversely, JSON Schema is stricter about nothing that the runtime accepts in the corpus below:
where the two disagree on a corpus file, the test fails instead of the expectation being bent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from jev_loop.bundles.manifest import (
    V2_FIELDS,
    V2_REQUIRED_FIELDS,
    ManifestError,
    parse_manifest,
)
from jev_loop.schema import STRICT_KEYWORDS, SchemaValidationError, validate_definition

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "spec" / "bundle-manifest-v2.schema.json"
EXAMPLES = REPO_ROOT / "spec" / "bundle-examples"

SPEC_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SPEC_SCHEMA)

# The corpus and its expected verdict, stated explicitly rather than inferred from file names.
# ``runtime_reason`` is the message fragment the runtime must produce when it rejects a file;
# ``schema_valid`` is what the JSON Schema must say about the same file.
CORPUS: dict[str, tuple[bool, str | None]] = {
    "manifest-v2-valid.json": (True, None),
    "manifest-v2-minimal.json": (True, None),
    "manifest-v2-boundaries.json": (True, None),
    "manifest-v2-null-optional.json": (False, "python_path must be a non-empty string"),
    "manifest-v2-unknown-version.json": (False, "unsupported schema_version"),
    "manifest-v2-unknown-field.json": (False, "unknown fields"),
    "manifest-v2-unsupported-schema.json": (False, "not a supported schema"),
    "manifest-v2-missing-required.json": (False, "missing required field"),
    "manifest-v2-bad-name.json": (False, "must be lowercase"),
    "manifest-v2-bad-version.json": (False, "simple version token"),
    "manifest-v2-bad-entrypoint.json": (False, "entrypoint must be shaped"),
    "manifest-v2-bad-bound.json": (False, "not a supported schema"),
    "manifest-v2-name-newline.json": (False, "must not have surrounding whitespace"),
}


def schema_errors(payload: dict) -> list:
    return sorted(VALIDATOR.iter_errors(payload), key=lambda error: list(error.path))


def test_the_schema_itself_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(SPEC_SCHEMA)
    assert SPEC_SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_every_schema_reference_is_local() -> None:
    """No remote fetch: a ``$ref`` that is not a local pointer would make the test network-bound."""
    refs: list[str] = []

    def collect(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref":
                    refs.append(value)
                else:
                    collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(SPEC_SCHEMA)
    assert refs, "the schema is expected to use local $defs references"
    assert all(ref.startswith("#/") for ref in refs), refs


def test_the_schema_describes_exactly_the_runtime_field_set() -> None:
    assert set(SPEC_SCHEMA["properties"]) == set(V2_FIELDS)


def test_the_schema_required_list_matches_the_runtime() -> None:
    assert set(SPEC_SCHEMA["required"]) == set(V2_REQUIRED_FIELDS)


def test_the_schema_is_closed_and_pins_the_version() -> None:
    assert SPEC_SCHEMA["additionalProperties"] is False
    assert SPEC_SCHEMA["type"] == "object"
    assert SPEC_SCHEMA["properties"]["schema_version"] == {"const": 2}


def test_the_schema_only_uses_keywords_the_runtime_understands() -> None:
    contract = SPEC_SCHEMA["$defs"]["contractSchema"]["properties"]
    assert set(contract) == set(STRICT_KEYWORDS)
    assert SPEC_SCHEMA["$defs"]["contractSchema"]["additionalProperties"] is False


@pytest.mark.parametrize("example", sorted(CORPUS), ids=lambda name: name)
def test_the_two_descriptions_agree_on_every_corpus_file(example: str) -> None:
    payload = json.loads((EXAMPLES / example).read_text(encoding="utf-8"))
    runtime_ok, runtime_reason = CORPUS[example]
    schema_ok = not schema_errors(payload)

    assert schema_ok is runtime_ok, (
        f"{example}: JSON Schema says valid={schema_ok}, the runtime expects valid={runtime_ok}; "
        f"schema errors: {[error.message for error in schema_errors(payload)]}"
    )
    if runtime_ok:
        manifest = parse_manifest(payload)
        assert manifest.is_standard is True
        assert set(payload) <= set(SPEC_SCHEMA["properties"])
    else:
        with pytest.raises(ManifestError, match=runtime_reason):
            parse_manifest(payload)


def test_the_corpus_covers_the_interesting_shapes() -> None:
    """Guard the corpus itself: an emptied or renamed corpus must not silently pass."""
    on_disk = {path.name for path in EXAMPLES.glob("manifest-v2-*.json")}
    assert on_disk == set(CORPUS), (on_disk, set(CORPUS))
    assert sum(1 for valid, _ in CORPUS.values() if valid) >= 3
    assert sum(1 for valid, _ in CORPUS.values() if not valid) >= 8


def test_the_documented_non_equivalence_list_is_still_accurate() -> None:
    """The runtime enforces things the schema cannot; the schema file must say so."""
    description = SPEC_SCHEMA["description"]
    for phrase in (
        "regular file",
        "nesting depth",
        "directory name must equal",
        "BUNDLE.md",
        "python_path must exist",
        "cross-checked",
        "lexical integer-token rule",
        "explicit null",
    ):
        assert phrase in description, phrase


def _deep_schema(depth: int) -> dict:
    schema: dict = {"type": "object"}
    current = schema
    for _ in range(depth):
        current["properties"] = {"next": {"type": "object"}}
        current = current["properties"]["next"]
    return schema


# Constraints the JSON Schema cannot express, each measured in both directions below.  These are
# recorded, not claimed away: the two descriptions are not fully equivalent.
RUNTIME_ONLY: list[tuple[str, dict]] = [
    (
        "inverted bounds (minItems > maxItems) are a cross-field rule JSON Schema has no keyword for",
        {"schema_version": 2, "name": "example-notes", "version": "1.0.0", "description": "d",
         "entrypoint": "bundle_controller:build",
         "config_schema": {"type": "array", "minItems": 5, "maxItems": 2}},
    ),
    (
        "over-deep schema nesting is bounded by the runtime, not by the JSON Schema",
        {"schema_version": 2, "name": "example-notes", "version": "1.0.0", "description": "d",
         "entrypoint": "bundle_controller:build",
         "inputs_schema": _deep_schema(24)},
    ),
]


@pytest.mark.parametrize("label,payload", RUNTIME_ONLY, ids=[label for label, _ in RUNTIME_ONLY])
def test_the_recorded_non_equivalences_are_measured_in_both_directions(label: str, payload: dict) -> None:
    """The outer schema accepts these; the runtime must still refuse them."""
    assert not schema_errors(payload), label
    with pytest.raises(ManifestError):
        parse_manifest(payload)


def test_filesystem_and_layout_rules_are_runtime_only(tmp_path: Path) -> None:
    """python_path existence, the directory/name match and BUNDLE.md are outside JSON Schema."""
    from jev_loop.bundles.validate import validate_manifest_path

    base = {"schema_version": 2, "name": "layout-check", "version": "1.0.0", "description": "d",
            "entrypoint": "bundle_controller:build"}

    missing_python = {**base, "python_path": "does-not-exist"}
    assert not schema_errors(missing_python)
    directory = tmp_path / "layout-check"
    directory.mkdir()
    (directory / "bundle.json").write_text(json.dumps(missing_python), encoding="utf-8")
    (directory / "BUNDLE.md").write_text("# x\n", encoding="utf-8")
    assert validate_manifest_path(directory / "bundle.json")["ok"] is False

    # Directory/name mismatch, again accepted by the schema alone.
    mismatched = {**base, "name": "different-name"}
    assert not schema_errors(mismatched)
    (directory / "bundle.json").write_text(json.dumps(mismatched), encoding="utf-8")
    report = validate_manifest_path(directory / "bundle.json")
    assert "name_directory_mismatch" in {item["code"] for item in report["errors"]}

    # A missing BUNDLE.md: valid to the schema, an error to the runtime.
    (directory / "bundle.json").write_text(json.dumps(base), encoding="utf-8")
    (directory / "BUNDLE.md").unlink()
    assert not schema_errors(base)
    missing_instructions = validate_manifest_path(directory / "bundle.json")
    assert "missing_instructions" in {item["code"] for item in missing_instructions["errors"]}


def test_supported_contract_schemas_validate() -> None:
    for schema in (
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": ["string", "null"], "minLength": 1},
        {"type": "array", "items": {"enum": ["a", "b"]}, "minItems": 1, "maxItems": 2},
        {"const": None},
        {"type": "number", "minimum": 0, "maximum": 1},
        {},
    ):
        validate_definition(schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"oneOf": [{"type": "string"}]},
        {"type": "unknown-type"},
        {"type": "object", "additionalProperties": {"type": "string"}},
        {"type": "object", "properties": {"a": {"format": "email"}}},
        {"type": "string", "minLength": 5, "maxLength": 2},
        {"type": "array", "minItems": -1},
        {"type": "number", "minimum": "1"},
        {"enum": []},
        {"type": "array", "items": [{"type": "string"}]},
    ],
)
def test_unsupported_contract_schemas_are_rejected_not_ignored(schema: dict) -> None:
    with pytest.raises(SchemaValidationError):
        validate_definition(schema)


def test_nested_schema_definitions_are_depth_bounded() -> None:
    schema: dict = {"type": "object"}
    current = schema
    for _ in range(24):
        current["properties"] = {"next": {"type": "object"}}
        current = current["properties"]["next"]
    with pytest.raises(SchemaValidationError, match="nesting exceeds"):
        validate_definition(schema)


def test_the_schema_file_is_loadable_without_the_runtime() -> None:
    """The schema must stand on its own: a consumer may use it without importing jev_loop."""
    assert SCHEMA_PATH.is_file()
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["title"].startswith("Jev Bundle")
    with pytest.raises(SchemaError):
        Draft202012Validator.check_schema({"type": "object", "properties": {"a": {"type": 1}}})

# ---------------------------------------------------------------------------------------------
# Generated per-field matrix: every optional field is exercised separately for omission, explicit
# null, wrong types and a valid form; every prose/list category for empty, whitespace, newline and
# boundary values; plus the nested required/properties edge cases.  Both descriptions judge each
# case and must agree (except the documented lexical integer-token rule below).
# ---------------------------------------------------------------------------------------------

BASE: dict = {
    "schema_version": 2,
    "name": "example-notes",
    "version": "1.0.0",
    "description": "d",
    "entrypoint": "bundle_controller:build",
}


def _with(field: str, value: object) -> dict:
    return {**BASE, field: value}


OPTIONAL_OBJECTS: tuple[str, ...] = (
    "output",
    "runtime",
    "dependencies",
    "resources",
    "stop",
    "verification",
)
OPTIONAL_TEXT: tuple[str, ...] = ("when_to_use",)
OPTIONAL_LISTS: tuple[str, ...] = ("authorizations",)


def _field_matrix() -> list[tuple[str, dict, bool]]:
    cases: list[tuple[str, dict, bool]] = []
    for field in OPTIONAL_OBJECTS:
        cases.append((f"{field}: omitted", BASE, True))
        cases.append((f"{field}: null", _with(field, None), False))
        for wrong in ("text", 5, False, []):
            cases.append((f"{field}: {wrong!r}", _with(field, wrong), False))
        cases.append((f"{field}: empty object", _with(field, {}), True))
    for field in OPTIONAL_TEXT:
        cases.append((f"{field}: omitted", BASE, True))
        cases.append((f"{field}: null", _with(field, None), False))
        for wrong in (5, False, [], {}):
            cases.append((f"{field}: {wrong!r}", _with(field, wrong), False))
        cases.append((f"{field}: valid", _with(field, "somewhere"), True))
    for field in OPTIONAL_LISTS:
        cases.append((f"{field}: omitted", BASE, True))
        cases.append((f"{field}: null", _with(field, None), False))
        for wrong in ("text", 5, False, {}):
            cases.append((f"{field}: {wrong!r}", _with(field, wrong), False))
        cases.append((f"{field}: empty list", _with(field, []), True))
        cases.append((f"{field}: one entry", _with(field, ["a.b"]), True))

    prose_values: tuple[tuple[str, object], ...] = (
        ("empty", ""),
        ("space", " "),
        ("spaces", "   "),
        ("leading", " x"),
        ("trailing", "x "),
        ("newline-leading", "\nx"),
        ("newline-trailing", "x\n"),
        ("tab-trailing", "x\t"),
        ("boundary-1", "x"),
        ("boundary-1024", "x" * 1024),
        ("boundary-1025", "x" * 1025),
    )
    prose_slots: tuple[tuple[str, str], ...] = (
        ("description", "description"),
        ("when_to_use", "when_to_use"),
        ("output.evidence", "output.evidence"),
        ("output.description", "output.description"),
        ("runtime.python", "runtime.python"),
        ("runtime.profile", "runtime.profile"),
        ("runtime.notes", "runtime.notes"),
        ("resources.notes", "resources.notes"),
        ("stop.release", "stop.release"),
        ("stop.notes", "stop.notes"),
        ("verification.evidence", "verification.evidence"),
        ("verification.notes", "verification.notes"),
    )
    for slot_label, slot in prose_slots:
        for value_label, value in prose_values:
            payload = dict(BASE)
            if "." in slot:
                parent, child = slot.split(".")
                payload[parent] = {child: value}
            else:
                payload[slot] = value
            expect_valid = value_label in {"boundary-1", "boundary-1024"}
            if slot == "description" and value_label == "boundary-1025":
                expect_valid = False
            cases.append((f"{slot_label}={value_label}", payload, expect_valid))

    for list_slot in ("authorizations", "dependencies.python", "dependencies.system", "resources.keys"):
        for value_label, entry in (
            ("empty-entry", ""),
            ("space", " "),
            ("spaces", "   "),
            ("newline", "x\n"),
            ("boundary-1", "x"),
            ("boundary-1024", "x" * 1024),
            ("boundary-1025", "x" * 1025),
        ):
            payload = dict(BASE)
            if "." in list_slot:
                parent, child = list_slot.split(".")
                payload[parent] = {child: [entry]}
            else:
                payload[list_slot] = [entry]
            cases.append(
                (f"{list_slot}[{value_label}]", payload, value_label in {"boundary-1", "boundary-1024"})
            )

    for label, value in (
        ("empty", ""),
        ("lowercase", "key"),
        ("leading-digit", "1KEY"),
        ("value-with-equals", "KEY=value"),
        ("space", "KEY "),
        ("newline", "KEY\n"),
        ("underscore", "MY_API_KEY"),
        ("max-length", "A" + "B" * 127),
        ("too-long", "A" + "B" * 128),
    ):
        payload = _with("dependencies", {"credential_env": [value]})
        cases.append((f"credential_env[{label}]", payload, label in {"underscore", "max-length"}))

    nested: tuple[tuple[str, dict, bool], ...] = (
        ("contract required duplicate", {"inputs_schema": {"type": "object", "required": ["a", "a"]}}, False),
        ("contract required empty name", {"inputs_schema": {"type": "object", "required": [""]}}, False),
        ("contract required ok", {"inputs_schema": {"type": "object", "required": ["a"]}}, True),
        (
            "contract properties empty key",
            {"inputs_schema": {"type": "object", "properties": {"": {"type": "string"}}}},
            False,
        ),
        (
            "contract properties ok",
            {"inputs_schema": {"type": "object", "properties": {"a": {"type": "string"}}}},
            True,
        ),
        (
            "contract nested required ok",
            {
                "inputs_schema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "object", "required": ["b"], "properties": {"b": {"type": "string"}}}
                    },
                }
            },
            True,
        ),
        (
            "contract nested required duplicate",
            {"inputs_schema": {"type": "object", "properties": {"a": {"type": "object", "required": ["b", "b"]}}}},
            False,
        ),
        (
            "contract additionalProperties object",
            {"inputs_schema": {"type": "object", "additionalProperties": {"type": "string"}}},
            False,
        ),
        ("contract inverted bounds", {"inputs_schema": {"type": "array", "minItems": 5, "maxItems": 2}}, False),
        ("contract negative maxItems", {"inputs_schema": {"type": "array", "maxItems": -1}}, False),
        ("contract enum empty", {"inputs_schema": {"enum": []}}, False),
        ("contract unknown keyword", {"inputs_schema": {"oneOf": [{"type": "string"}]}}, False),
        ("contract unknown type", {"inputs_schema": {"type": "unknown-type"}}, False),
        ("output.schema null", {"output": {"schema": None}}, False),
        ("output.schema valid", {"output": {"schema": {"type": "object"}}}, True),
    )
    for label, patch, expect_valid in nested:
        cases.append((label, {**BASE, **patch}, expect_valid))
    return cases


MATRIX = _field_matrix()
# The JSON Schema cannot express these cross-field / filesystem rules: the runtime must still refuse
# them even though the schema accepts them.  They are asserted in the other direction below.
# uniqueItems and propertyNames are representable, so duplicates and empty property names now
# agree in both descriptions; only the cross-field bound rule cannot be expressed.
SCHEMA_ACCEPTS_RUNTIME_REFUSES = {
    "contract inverted bounds",
}


@pytest.mark.parametrize("label,payload,expect_valid", MATRIX, ids=[label for label, _, _ in MATRIX])
def test_the_two_descriptions_agree_on_the_field_matrix(label: str, payload: dict, expect_valid: bool) -> None:
    schema_ok = not schema_errors(payload)
    try:
        parse_manifest(payload)
        runtime_ok = True
    except ManifestError:
        runtime_ok = False

    assert runtime_ok is expect_valid, f"{label}: the runtime verdict is not what this case declares"
    if label in SCHEMA_ACCEPTS_RUNTIME_REFUSES:
        assert schema_ok, f"{label}: the schema is expected to be unable to express this rule"
        return
    assert schema_ok is runtime_ok, (
        f"{label}: JSON Schema says valid={schema_ok}, the runtime says valid={runtime_ok}; "
        f"schema errors: {[error.message for error in schema_errors(payload)]}"
    )


LEXICAL_INTEGER_CASES: tuple[tuple[str, dict], ...] = (
    ("schema_version as a float token", {**BASE, "schema_version": 2.0}),
    ("minItems as a float token", {**BASE, "inputs_schema": {"type": "array", "minItems": 1.0}}),
    ("maxItems as a float token", {**BASE, "inputs_schema": {"type": "array", "maxItems": 3.0}}),
)


@pytest.mark.parametrize("label,payload", LEXICAL_INTEGER_CASES, ids=[label for label, _ in LEXICAL_INTEGER_CASES])
def test_the_documented_lexical_integer_rule_is_a_known_non_equivalence(label: str, payload: dict) -> None:
    """JSON has one number type; the runtime additionally requires an integer *token*.

    This is a genuine, documented limitation of the machine-readable description rather than a
    claim of full agreement: the schema accepts ``2.0``/``1.0`` and the runtime refuses them.
    """
    assert not schema_errors(payload), f"{label}: the schema is expected to accept this"
    with pytest.raises(ManifestError):
        parse_manifest(payload)

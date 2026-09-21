"""Dependency-free JSON Schema support for bounded, declared data contracts.

The module implements a documented **subset** of JSON Schema; it is not a full Draft
2020-12 implementation and never claims to be one.  Two profiles are exposed:

``validate`` / ``legacy``
    The historical permissive behaviour, kept for cognition results and any bundle that
    already relies on it.  Unknown keywords are ignored, ``additionalProperties`` only
    understands the literal ``false``, ``enum``/``const`` compare with Python equality
    (so ``1`` and ``true`` are conflated), and non-finite numbers pass through.

``validate_strict`` / ``strict``
    The closed subset used for schemas *declared by a manifest v2 bundle* (its
    ``inputs_schema``, ``config_schema`` and ``output.schema``).  The schema definition
    itself is validated first: unsupported keywords, wrong keyword types, illegal bounds
    and non-finite bounds are rejected instead of being silently ignored.  Values are then
    checked with JSON type semantics (``true`` is not ``1``) and non-finite numbers are
    rejected.  A declared schema that cannot be enforced is an error, never a false green.

Neither profile executes code, reads files or touches the network: schemas and values are
plain JSON data.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

Json = Any

MAX_SCHEMA_DEPTH = 16
MAX_VALUE_DEPTH = 64

TYPE_CHECKS = {
    "null": lambda value: value is None,
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, Mapping),
}

STRICT_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
    }
)


class SchemaValidationError(ValueError):
    """A value, or a declared schema, does not satisfy the supported subset."""


# --------------------------------------------------------------------------------------
# Legacy permissive profile (unchanged behaviour)
# --------------------------------------------------------------------------------------


def validate(value: Json, schema: Mapping[str, Json], *, path: str = "$") -> None:
    if not schema:
        return
    allowed = schema.get("enum")
    if isinstance(allowed, Sequence) and not isinstance(allowed, (str, bytes)) and value not in allowed:
        raise SchemaValidationError(f"{path} must be one of {list(allowed)!r}")
    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path} must equal {schema['const']!r}")

    expected = schema.get("type")
    if isinstance(expected, str):
        expected_types = (expected,)
    elif isinstance(expected, Sequence) and not isinstance(expected, (str, bytes)):
        expected_types = tuple(str(item) for item in expected)
    else:
        expected_types = ()
    if expected_types and not any(_matches_type(value, item) for item in expected_types):
        raise SchemaValidationError(f"{path} must have type {' | '.join(expected_types)}, got {_type_name(value)}")

    if isinstance(value, Mapping):
        required = schema.get("required") or ()
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise SchemaValidationError(f"{path} schema.required must be an array")
        missing = [str(key) for key in required if key not in value]
        if missing:
            raise SchemaValidationError(f"{path} is missing required properties {missing}")
        properties = schema.get("properties") or {}
        if not isinstance(properties, Mapping):
            raise SchemaValidationError(f"{path} schema.properties must be an object")
        for key, child_schema in properties.items():
            if key in value:
                if not isinstance(child_schema, Mapping):
                    raise SchemaValidationError(f"{path}.{key} schema must be an object")
                validate(value[key], child_schema, path=f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extras = sorted(str(key) for key in value if key not in properties)
            if extras:
                raise SchemaValidationError(f"{path} has unexpected properties {extras}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < int(minimum):
            raise SchemaValidationError(f"{path} must contain at least {minimum} items")
        if maximum is not None and len(value) > int(maximum):
            raise SchemaValidationError(f"{path} must contain at most {maximum} items")
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, Mapping):
                raise SchemaValidationError(f"{path} schema.items must be an object")
            for index, item in enumerate(value):
                validate(item, items, path=f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < int(minimum):
            raise SchemaValidationError(f"{path} must contain at least {minimum} characters")
        if maximum is not None and len(value) > int(maximum):
            raise SchemaValidationError(f"{path} must contain at most {maximum} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < float(minimum):
            raise SchemaValidationError(f"{path} must be >= {minimum}")
        if maximum is not None and value > float(maximum):
            raise SchemaValidationError(f"{path} must be <= {maximum}")


def _matches_type(value: Json, expected: str) -> bool:
    try:
        return TYPE_CHECKS[expected](value)
    except KeyError:
        raise SchemaValidationError(f"unsupported JSON schema type {expected!r}") from None


def _type_name(value: Json) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


# --------------------------------------------------------------------------------------
# Strict closed profile (manifest v2 declared schemas)
# --------------------------------------------------------------------------------------


_MISSING = object()


def validate_definition(schema: Json, *, path: str = "$", _depth: int = 0) -> None:
    """Check that a declared schema is inside the strict closed subset.

    Unknown keywords, unsupported keyword types, illegal bounds, non-finite bounds, an
    object-valued ``additionalProperties`` and over-deep nesting are rejected.  A rejected
    definition means the contract cannot be enforced, so it must never be treated as a
    passing declared schema.
    """
    if _depth > MAX_SCHEMA_DEPTH:
        raise SchemaValidationError(f"{path} schema nesting exceeds {MAX_SCHEMA_DEPTH} levels")
    if not isinstance(schema, Mapping):
        raise SchemaValidationError(f"{path} schema must be an object, got {_type_name(schema)}")
    unknown = sorted(str(key) for key in schema if key not in STRICT_KEYWORDS)
    if unknown:
        raise SchemaValidationError(
            f"{path} uses unsupported schema keywords {unknown}; "
            f"the supported subset is {sorted(STRICT_KEYWORDS)}"
        )
    _reject_non_finite(schema, path=path)

    expected = schema.get("type", _MISSING)
    if expected is not _MISSING:
        names = [expected] if isinstance(expected, str) else expected
        if not isinstance(names, Sequence) or isinstance(names, (str, bytes)) or not names:
            raise SchemaValidationError(f"{path}.type must be a type name or a non-empty array of type names")
        for name in names:
            if not isinstance(name, str) or name not in TYPE_CHECKS:
                raise SchemaValidationError(f"{path}.type has unsupported type {name!r}")

    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list) or not allowed:
            raise SchemaValidationError(f"{path}.enum must be a non-empty array")
    if "const" in schema and not _is_json_value(schema["const"]):
        raise SchemaValidationError(f"{path}.const must be a JSON value")

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
            raise SchemaValidationError(f"{path}.required must be an array of non-empty strings")
        if len(set(required)) != len(required):
            raise SchemaValidationError(f"{path}.required must not repeat a property name")

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping):
            raise SchemaValidationError(f"{path}.properties must be an object")
        for key, child in properties.items():
            if not isinstance(key, str) or not key:
                raise SchemaValidationError(f"{path}.properties keys must be non-empty strings")
            validate_definition(child, path=f"{path}.{key}", _depth=_depth + 1)

    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise SchemaValidationError(
            f"{path}.additionalProperties only supports the literal false in the strict subset"
        )

    if "items" in schema:
        validate_definition(schema["items"], path=f"{path}.items", _depth=_depth + 1)

    _check_bounds(schema, path=path)


def validate_strict(value: Json, schema: Mapping[str, Json], *, path: str = "$") -> None:
    """Validate a value against a declared schema after checking the definition itself."""
    validate_definition(schema)
    validate_value(value, schema, path=path)


def validate_value(value: Json, schema: Mapping[str, Json], *, path: str = "$") -> None:
    """Strict value check with JSON type semantics; the definition must already be valid."""
    _reject_non_finite(value, path=path)
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        raise SchemaValidationError(f"{path} must be one of {schema['enum']!r}")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise SchemaValidationError(f"{path} must equal {schema['const']!r}")

    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(TYPE_CHECKS[name](value) for name in names):
            raise SchemaValidationError(f"{path} must have type {' | '.join(names)}, got {_type_name(value)}")

    if isinstance(value, Mapping):
        required = schema.get("required") or ()
        missing = [key for key in required if key not in value]
        if missing:
            raise SchemaValidationError(f"{path} is missing required properties {missing}")
        properties = schema.get("properties") or {}
        for key, child in properties.items():
            if key in value:
                validate_value(value[key], child, path=f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extras = sorted(str(key) for key in value if key not in properties)
            if extras:
                raise SchemaValidationError(f"{path} has unexpected properties {extras}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise SchemaValidationError(f"{path} must contain at least {minimum} items")
        if maximum is not None and len(value) > maximum:
            raise SchemaValidationError(f"{path} must contain at most {maximum} items")
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(value):
                validate_value(item, items, path=f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            raise SchemaValidationError(f"{path} must contain at least {minimum} characters")
        if maximum is not None and len(value) > maximum:
            raise SchemaValidationError(f"{path} must contain at most {maximum} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise SchemaValidationError(f"{path} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise SchemaValidationError(f"{path} must be <= {maximum}")


def validate_declared_defaults(value: Json, schema: Mapping[str, Json], *, path: str = "$") -> None:
    """Validate the fields a manifest *does* declare, not the fields a caller will add.

    ``config`` holds defaults, and the runtime merges them shallowly with the caller's
    ``bundle_config`` before validating the result.  A static check must therefore not demand
    that the defaults already satisfy ``required``: a bundle may legitimately declare a few
    defaults and let the caller supply the rest.  What is checked here:

    - the value is an object when the schema requires an object,
    - every declared key that the schema describes matches its own subschema,
    - a declared key the schema does not describe is refused under ``additionalProperties: false``,
    - a declared key whose own subschema declares ``required`` still has to satisfy it.

    Whole-object constraints (``required`` at the root, root ``enum``/``const``) are deliberately
    left to the post-merge validation, because a caller's overrides can still satisfy them.  This
    does not weaken definition validation or per-value type checks: the definition is validated
    first, and each declared field is validated with the same strict rules.
    """
    validate_definition(schema)
    _reject_non_finite(value, path=path)

    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(TYPE_CHECKS[name](value) for name in names):
            raise SchemaValidationError(f"{path} must have type {' | '.join(names)}, got {_type_name(value)}")

    if not TYPE_CHECKS["object"](value):
        if "properties" in schema or schema.get("additionalProperties") is False:
            raise SchemaValidationError(f"{path} must have type object, got {_type_name(value)}")
        return
    properties = schema.get("properties") or {}
    for key, item in value.items():
        if key in properties:
            validate_value(item, properties[key], path=f"{path}.{key}")
        elif schema.get("additionalProperties") is False:
            raise SchemaValidationError(f"{path} has unexpected properties [{key!r}]")


def _check_bounds(schema: Mapping[str, Json], *, path: str) -> None:
    """Validate every declared bound, distinguishing "absent" from an explicit null.

    All four length/count bounds must be non-boolean non-negative integers (a negative or
    fractional ``maxItems``/``maxLength`` is a broken contract, not a permissive one), and
    ``minimum``/``maximum`` must be finite numbers.  ``None`` is *present* here, so an explicit
    ``null`` bound is an error rather than a silently ignored default.
    """
    for group in (("minItems", "maxItems"), ("minLength", "maxLength")):
        for key in group:
            if key not in schema:
                continue
            bound = schema[key]
            if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
                raise SchemaValidationError(f"{path}.{key} must be a non-negative integer")
        if group[0] in schema and group[1] in schema and schema[group[0]] > schema[group[1]]:
            raise SchemaValidationError(f"{path}.{group[0]} must not exceed {group[1]}")

    for key in ("minimum", "maximum"):
        if key not in schema:
            continue
        bound = schema[key]
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            raise SchemaValidationError(f"{path}.{key} must be a finite number")
        if isinstance(bound, float) and not math.isfinite(bound):
            raise SchemaValidationError(f"{path}.{key} must be a finite number")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise SchemaValidationError(f"{path}.minimum must not exceed maximum")


def _json_equal(left: Json, right: Json) -> bool:
    """JSON equality: booleans are not numbers, and containers compare by structure.

    Numbers compare **exactly**: the operands are never coerced to ``float``, so ``2**53`` and
    ``2**53 + 1`` stay distinct (a float round-trip would conflate them) and an arbitrarily
    large integer cannot raise ``OverflowError``.  ``1`` and ``1.0`` still compare equal, which
    is what JSON means by the same number.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    return type(left) is type(right) and left == right


def _is_json_value(value: Json, *, _depth: int = 0) -> bool:
    if _depth > MAX_SCHEMA_DEPTH:
        return False
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item, _depth=_depth + 1) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_value(item, _depth=_depth + 1) for key, item in value.items())
    return False


def _reject_non_finite(value: Json, *, path: str = "$", _depth: int = 0) -> None:
    """Reject non-finite numbers, and refuse to walk an unbounded value.

    Hitting the depth limit is an explicit, bounded rejection: returning quietly would let a
    non-finite number hide below the limit and reach a consumer as if it had been checked.
    """
    if _depth > MAX_VALUE_DEPTH:
        raise SchemaValidationError(f"{path} nests deeper than {MAX_VALUE_DEPTH} levels")
    if isinstance(value, float) and not math.isfinite(value):
        raise SchemaValidationError(f"{path} must be a finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite(item, path=f"{path}.{key}", _depth=_depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_non_finite(item, path=f"{path}[{index}]", _depth=_depth + 1)

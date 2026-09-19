"""Small dependency-free validator for cognition result schemas.

This intentionally implements a documented JSON Schema subset rather than pretending to
be a full Draft 2020-12 implementation.  It covers the shapes useful for bounded cognition
jobs and rejects a result before it enters runtime state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

Json = Any


class SchemaValidationError(ValueError):
    pass


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
    checks = {
        "null": value is None,
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "object": isinstance(value, Mapping),
    }
    if expected not in checks:
        raise SchemaValidationError(f"unsupported JSON schema type {expected!r}")
    return checks[expected]


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

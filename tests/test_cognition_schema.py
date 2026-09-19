from __future__ import annotations

import pytest

from jev_loop.host.schema import SchemaValidationError, validate

SCHEMA = {
    "type": "object",
    "required": ["search_terms"],
    "additionalProperties": False,
    "properties": {
        "search_terms": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def test_cognition_schema_accepts_the_documented_structured_subset():
    validate({"search_terms": ["湾区周末市集"], "confidence": 0.8}, SCHEMA)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"confidence": 0.8}, "missing required"),
        ({"search_terms": []}, "at least 1"),
        ({"search_terms": [""]}, "at least 1 characters"),
        ({"search_terms": ["x"], "extra": True}, "unexpected properties"),
        ({"search_terms": ["x"], "confidence": True}, "must have type number"),
    ],
)
def test_cognition_schema_rejects_malformed_results(value, message):
    with pytest.raises(SchemaValidationError, match=message):
        validate(value, SCHEMA)

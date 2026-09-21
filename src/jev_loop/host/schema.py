"""Compatibility re-export of :mod:`jev_loop.schema`.

The dependency-free validator moved to the top-level ``jev_loop.schema`` module so the
bundle contract layer can use it without importing the host.  ``jev_loop.host.schema``
keeps working for existing callers: cognition results keep the historical permissive
``validate`` behaviour, while manifest-declared schemas use the strict profile.
"""

from __future__ import annotations

from ..schema import (
    MAX_SCHEMA_DEPTH,
    STRICT_KEYWORDS,
    SchemaValidationError,
    validate,
    validate_definition,
    validate_strict,
    validate_value,
)

__all__ = [
    "MAX_SCHEMA_DEPTH",
    "STRICT_KEYWORDS",
    "SchemaValidationError",
    "validate",
    "validate_definition",
    "validate_strict",
    "validate_value",
]

"""Non-secret Jev settings validation, shared by the live policy factory and the doctor.

Nothing in this module performs I/O.  It validates exactly the values that are safe to
show, so the same rules apply when a live request function is built and when
``jev-loop-doctor`` reports whether a live run is configured.

Credential rules:

* The process environment is the only supported source.  No file, keychain or other
  project's configuration is searched.
* A missing, blank or header-unsafe credential raises :class:`CredentialError` before any
  network or environment action, and never falls back to a mock.
* Error messages never contain the credential value.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

DEFAULT_API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 300.0

# HTTP header values and API keys are printable ASCII without spaces; anything else
# (CR/LF in particular) could forge header content, so it is refused up front.  ``fullmatch``
# is deliberate: ``match`` with a trailing ``$`` would accept a value ending in a newline.
_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]+")


class ConfigError(ValueError):
    """A non-secret setting is missing or invalid.

    Raised while configuration is checked, before any network request or environment
    action, so a typo fails fast instead of surfacing later as a confusing run failure.
    """


class CredentialError(RuntimeError):
    """The API credential is missing, blank or unusable as an HTTP header value.

    The message names the variable and the problem only; it never contains the value.
    """


def validate_api_url(value: str) -> str:
    """Validate the Jev endpoint URL, refusing every shape that could leak the credential.

    The value is checked as a whole before any URL parsing: whitespace, control characters and
    non-ASCII text are refused rather than silently normalized away.  Parsing itself is wrapped
    so a malformed URL becomes a fixed :class:`ConfigError` (never a raw ``ValueError`` and never
    an echo of the supplied value).  The port must be a number in 1..65535, so a typo like
    ``https://host:v1/path`` fails the preflight instead of passing it and dying later.
    """
    if not isinstance(value, str) or not value:
        raise ConfigError("api url must be a non-empty string")
    if not _PRINTABLE_ASCII.fullmatch(value):
        raise ConfigError("api url must be printable ASCII without whitespace or control characters")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ConfigError("api url is malformed") from None
    if parts.scheme not in {"http", "https"}:
        raise ConfigError("api url must use https (http is accepted only for a loopback test endpoint)")
    if not parts.hostname:
        raise ConfigError("api url has no host")
    try:
        port = parts.port
    except ValueError:
        raise ConfigError("api url has an invalid port; it must be a number between 1 and 65535") from None
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("api url has an invalid port; it must be a number between 1 and 65535")
    if parts.username or parts.password:
        raise ConfigError("api url must not contain userinfo; the credential belongs in the Authorization header")
    if parts.query:
        raise ConfigError("api url must not contain a query string; never put a credential in the URL")
    if parts.fragment:
        raise ConfigError("api url must not contain a fragment")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ConfigError("api url may use http only for a loopback host; use https otherwise")
    return value


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_timeout(value: float) -> float:
    """Validate a finite, positive timeout in seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("timeout must be a number of seconds")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("timeout must be a positive, finite number of seconds")
    if timeout > MAX_TIMEOUT:
        raise ConfigError(f"timeout must be at most {MAX_TIMEOUT:g} seconds")
    return timeout


def validate_model(value: str) -> str:
    """Validate a non-empty model name without embedded whitespace."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("model must be a non-empty string")
    model = value.strip()
    if any(character.isspace() for character in model):
        raise ConfigError("model must not contain whitespace")
    return model


def validate_api_key_text(key: str, *, label: str = "api_key") -> str:
    """Validate that ``key`` can be sent as an HTTP header value without forging headers.

    The whole string must be printable ASCII without spaces (``fullmatch``).  This function does
    not trim: explicit ``http_request_fn(api_key=...)`` injection is exact, while
    :func:`api_key_from_env` strips surrounding whitespace before calling it, because an exported
    value like ``KEY=$(cat file)`` commonly carries a trailing newline.
    """
    if not isinstance(key, str) or not key:
        raise CredentialError(f"{label} is blank; the live Jev policy will not fall back to a mock")
    if not _PRINTABLE_ASCII.fullmatch(key):
        raise CredentialError(
            f"{label} contains characters that cannot be sent in an HTTP Authorization header; "
            "the value was not used and was not printed"
        )
    return key


def api_key_from_env(
    env_name: str = DEFAULT_API_KEY_ENV,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Read the credential from the process environment, or raise :class:`CredentialError`.

    Surrounding whitespace is stripped (a trailing newline from ``export KEY=$(cat file)``
    is common); anything else must be printable ASCII.  ``environ`` exists for tests and
    for callers that already hold an explicit environment mapping.
    """
    if not isinstance(env_name, str) or not env_name.strip():
        raise CredentialError("the credential environment variable name is empty")
    name = env_name.strip()
    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None:
        raise CredentialError(
            f"{name} is not configured; jev-loop reads the credential from the process environment only "
            "and will not fall back to a mock"
        )
    key = raw.strip()
    if not key:
        raise CredentialError(f"{name} is blank; the live Jev policy will not fall back to a mock")
    return validate_api_key_text(key, label=name)


def require_api_key(
    env_name: str = DEFAULT_API_KEY_ENV,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Explicit alias for :func:`api_key_from_env`."""
    return api_key_from_env(env_name, environ=environ)

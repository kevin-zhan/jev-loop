"""Credential and non-secret configuration rules for the live Jev policy.

Everything here is offline: the point is that a missing, blank or header-unsafe credential
fails with a named error before any network or environment action, and that the same
validation backs the doctor and the live example.
"""

from __future__ import annotations

import traceback

import pytest

from jev_loop.policies.config import (
    ConfigError,
    CredentialError,
    api_key_from_env,
    require_api_key,
    validate_api_key_text,
    validate_api_url,
    validate_model,
    validate_timeout,
)
from jev_loop.policies.jev import JevPolicy, default_request_fn, http_request_fn

SENTINEL = "sentinel-credential-value-never-logged"


def test_missing_key_is_named_and_never_reaches_the_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no network may happen before the credential is known")

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr("socket.socket.connect", explode)

    with pytest.raises(CredentialError) as caught:
        default_request_fn(environ={})

    assert "TYPESAFE_API_KEY" in str(caught.value)
    assert "mock" in str(caught.value)


@pytest.mark.parametrize("value", ["", "   ", "\n", "\t \t"])
def test_blank_key_is_rejected(value):
    with pytest.raises(CredentialError) as caught:
        api_key_from_env(environ={"TYPESAFE_API_KEY": value})
    message = str(caught.value)
    assert "TYPESAFE_API_KEY" in message
    assert "blank" in message


@pytest.mark.parametrize("value", ["key\r\nX-Injected: 1", "key\nInjected", "key with spaces", "key\x00", "clé"])
def test_header_unsafe_key_is_rejected_without_echoing_it(value):
    with pytest.raises(CredentialError) as caught:
        api_key_from_env(environ={"TYPESAFE_API_KEY": value})
    message = str(caught.value)
    assert "header" in message
    assert value not in message
    assert SENTINEL not in message


def test_explicit_injection_is_validated_at_construction_time():
    with pytest.raises(CredentialError):
        http_request_fn(api_key="")
    with pytest.raises(CredentialError):
        http_request_fn(api_key="bad\nkey")


def test_environment_is_the_only_source_and_surrounding_whitespace_is_stripped(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    request_fn = default_request_fn(environ={"TYPESAFE_API_KEY": f"  {SENTINEL}\n"})
    assert callable(request_fn)

    with pytest.raises(CredentialError):
        default_request_fn(environ={})


def test_custom_environment_variable_name_is_supported():
    assert api_key_from_env("MY_JEV_KEY", environ={"MY_JEV_KEY": SENTINEL}) == SENTINEL
    assert require_api_key("MY_JEV_KEY", environ={"MY_JEV_KEY": SENTINEL}) == SENTINEL
    with pytest.raises(CredentialError) as caught:
        require_api_key("MY_JEV_KEY", environ={})
    assert "MY_JEV_KEY" in str(caught.value)


def test_validate_api_key_text_rejects_empty_and_unsafe_values():
    assert validate_api_key_text(SENTINEL) == SENTINEL
    with pytest.raises(CredentialError):
        validate_api_key_text("")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",  # http outside loopback
        "https://user:pass@api.example.com/v1",  # userinfo
        "https://api.example.com/v1?key=abc",  # credential in the URL
        "https://api.example.com/v1#frag",
        "ftp://api.example.com/v1",
        "https://",
        "",
    ],
)
def test_invalid_endpoints_are_refused(url):
    with pytest.raises(ConfigError):
        validate_api_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/v1 with space",
        "https://api.example.com/v1\nX-Injected: 1",
        "https://api.example.com/v1\t",
        "https://api.example.com/v1\x00",
        "https://api.example.com/v1é",
        "https://[::1",
        "https://api.example.com:abc/v1",
        "https://api.example.com:0/v1",
        "https://api.example.com:65536/v1",
        "https://api.example.com:99999999999999999999/v1",
        "https://api.typesafe.ai:v1/systemone",
    ],
)
def test_whitespace_control_and_bad_ports_are_refused_without_echoing_the_value(url):
    with pytest.raises(ConfigError) as caught:
        validate_api_url(url)
    message = str(caught.value)
    assert url not in message
    assert url not in "".join(traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.typesafe.ai/v1/systemone",
        "http://127.0.0.1:8080/x",
        "http://localhost/x",
        "http://[::1]/x",
    ],
)
def test_valid_endpoints_pass(url):
    assert validate_api_url(url) == url


@pytest.mark.parametrize("key", [f"{SENTINEL}\n", f"{SENTINEL}\r\n", f"{SENTINEL}\r", f"\n{SENTINEL}", f"{SENTINEL} "])
def test_explicit_key_with_edge_whitespace_is_rejected_before_urllib(monkeypatch, key):
    def explode(*args, **kwargs):
        raise AssertionError("no network may happen for an invalid explicit key")

    monkeypatch.setattr("socket.socket.connect", explode)
    with pytest.raises(CredentialError) as caught:
        http_request_fn(api_key=key)

    message = str(caught.value)
    assert SENTINEL not in message
    rendered = "".join(traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__))
    assert SENTINEL not in rendered


def test_environment_strips_but_explicit_injection_is_exact():
    # An exported value commonly carries a trailing newline; explicit injection must be exact,
    # otherwise a caller could smuggle header-splitting characters past urllib's own validation.
    assert api_key_from_env(environ={"TYPESAFE_API_KEY": f"{SENTINEL}\n"}) == SENTINEL
    assert validate_api_key_text(f"  {SENTINEL}  ".strip()) == SENTINEL
    with pytest.raises(CredentialError):
        validate_api_key_text(f"{SENTINEL}\n")


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "20", 10_000])
def test_invalid_timeouts_are_refused(value):
    with pytest.raises(ConfigError):
        validate_timeout(value)


def test_timeout_accepts_a_positive_finite_number():
    assert validate_timeout(2.5) == 2.5
    assert validate_timeout(20) == 20.0


@pytest.mark.parametrize("value", ["", "   ", "two words", 7, None])
def test_invalid_models_are_refused(value):
    with pytest.raises(ConfigError):
        validate_model(value)


def test_policy_validates_its_own_model():
    with pytest.raises(ConfigError):
        JevPolicy(request_fn=lambda payload: {}, model="  ")


def test_http_request_fn_validates_non_secret_settings():
    with pytest.raises(ConfigError):
        http_request_fn(api_key=SENTINEL, url="http://api.example.com/v1")
    with pytest.raises(ConfigError):
        http_request_fn(api_key=SENTINEL, timeout=0)

"""Wire-level tests for the only networked code in the project.

A real loopback HTTP fixture (``fake_jev``) is used, so the Authorization header, the
request payload, the success path and every failure mapping are exercised for real without
calling a paid API.  No provider response body may reach an exception, a traceback or a log.
"""

from __future__ import annotations

import math
import time
import traceback

import pytest

from jev_loop.policies import jev as jev_module
from jev_loop.policies.jev import CredentialError, JevRequestError, ResponseShapeError, http_request_fn

SENTINEL = "sentinel-credential-value-never-logged"
BODY_MARKER = "fixture-body-marker"


def minimal_payload() -> dict:
    return {
        "model": "jev-1.13.0",
        "state": {"goal": "transport test"},
        "questions": {"next_action": {"type": "choice", "instructions": "pick", "criteria": {}}},
    }


def rendered(error: BaseException) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def test_round_trip_sends_bearer_json_and_parses_the_answer(fake_jev):
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)
    payload = minimal_payload()
    answer = request_fn(payload)

    assert answer["answers"]["next_action"]["choice"] == "request_finish"
    assert len(fake_jev.requests) == 1
    sent = fake_jev.requests[0]
    assert sent["authorization"] == f"Bearer {SENTINEL}"
    assert sent["content_type"] == "application/json"
    assert sent["payload"] == payload
    assert sent["path"] == "/"


@pytest.mark.parametrize("behavior", ["auth_error", "forbidden"])
def test_auth_failures_are_credential_errors_without_any_body(fake_jev, behavior):
    fake_jev.behavior = behavior
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)

    with pytest.raises(CredentialError) as caught:
        request_fn(minimal_payload())

    message = str(caught.value)
    assert "HTTP 40" in message
    assert BODY_MARKER not in message
    assert SENTINEL not in message
    assert BODY_MARKER not in rendered(caught.value)
    assert len(fake_jev.requests) == 1  # nothing is retried


@pytest.mark.parametrize("behavior,code", [("rate_limited", 429), ("server_error", 500)])
def test_http_failures_use_fixed_messages_and_never_read_the_body(fake_jev, behavior, code):
    fake_jev.behavior = behavior
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)

    with pytest.raises(JevRequestError) as caught:
        request_fn(minimal_payload())

    message = str(caught.value)
    assert str(code) in message
    assert BODY_MARKER not in message
    assert BODY_MARKER not in rendered(caught.value)
    assert not isinstance(caught.value, ResponseShapeError)
    assert len(fake_jev.requests) == 1


def test_invalid_json_is_a_fixed_transport_error(fake_jev):
    fake_jev.behavior = "invalid_json"
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)

    with pytest.raises(JevRequestError) as caught:
        request_fn(minimal_payload())

    assert "not valid JSON" in str(caught.value)
    assert BODY_MARKER not in str(caught.value)


@pytest.mark.parametrize("behavior", ["truncated_chunked", "reset"])
def test_read_and_protocol_failures_are_fixed_errors_without_retry(fake_jev, behavior):
    fake_jev.behavior = behavior
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)

    with pytest.raises(JevRequestError) as caught:
        request_fn(minimal_payload())

    message = str(caught.value)
    assert "retried" in message
    assert "IncompleteRead" not in message  # no raw http.client exception text
    assert "RemoteDisconnected" not in message
    assert BODY_MARKER not in message
    assert SENTINEL not in message
    assert not isinstance(caught.value, ResponseShapeError)
    assert len(fake_jev.requests) == 1  # exactly one attempt, no implicit retry


def test_timeout_is_a_fixed_transport_error(fake_jev):
    fake_jev.behavior = "slow"
    fake_jev.delay = 0.75
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=0.2)

    started = time.monotonic()
    with pytest.raises(JevRequestError) as caught:
        request_fn(minimal_payload())
    elapsed = time.monotonic() - started

    assert "timed out" in str(caught.value)
    assert elapsed < 0.7


def test_cross_origin_redirect_is_refused_and_never_reaches_the_trap(fake_jev):
    fake_jev.behavior = "redirect"
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)

    with pytest.raises(JevRequestError) as caught:
        request_fn(minimal_payload())

    assert "redirect" in str(caught.value)
    assert fake_jev.trap_requests == []  # the Authorization header was never resent
    assert len(fake_jev.requests) == 1


def test_same_origin_redirect_is_refused_too(fake_jev):
    fake_jev.behavior = "redirect_self"
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)

    with pytest.raises(JevRequestError):
        request_fn(minimal_payload())

    assert len(fake_jev.requests) == 1


def test_oversized_response_is_refused(fake_jev, monkeypatch):
    monkeypatch.setattr(jev_module, "MAX_RESPONSE_BYTES", 10)
    request_fn = http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=2.0)

    with pytest.raises(JevRequestError) as caught:
        request_fn(minimal_payload())

    assert "exceeded" in str(caught.value)


def test_timeout_value_is_validated_before_any_request(fake_jev):
    with pytest.raises(Exception) as caught:
        http_request_fn(api_key=SENTINEL, url=fake_jev.url, timeout=math.nan)
    assert not isinstance(caught.value, JevRequestError)  # a configuration error, not a transport one
    assert fake_jev.requests == []

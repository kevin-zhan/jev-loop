"""Shared offline test helpers: a loopback HTTP fixture for the Jev wire shape.

The fixture speaks the real request/answer shape over real HTTP on 127.0.0.1, so the
transport, header handling, error mapping and the example's happy path can be exercised
without a paid API.  It also runs a trap server: a redirect that would resend the
Authorization header must never reach it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def pick_alpha_then_gamma(payload):
    """Default deterministic policy: satisfy alpha, then archive gamma, then finish."""
    criteria = payload["questions"]["next_action"]["criteria"]
    for option, spec in criteria.items():
        args = spec.get("args") or {}
        if spec.get("effect") != "control" and args.get("path") == "inbox/alpha.txt":
            return option
    for option, spec in criteria.items():
        args = spec.get("args") or {}
        if spec.get("effect") != "control" and args.get("target") == "archive/gamma.log":
            return option
    return "request_finish"


class FakeJev:
    """A loopback server speaking the Jev request/answer shape.

    ``behavior`` selects the response: ``ok`` (default), ``slow``, ``auth_error``,
    ``forbidden``, ``rate_limited``, ``server_error``, ``invalid_json``, ``redirect``
    (cross-origin toward the trap), ``redirect_self`` (same-origin redirect).
    ``answer_picker`` turns a received payload into an option key.
    """

    def __init__(self) -> None:
        self.behavior = "ok"
        self.delay = 0.0
        self.answer_picker = pick_alpha_then_gamma
        self.requests: list[dict] = []
        self.trap_requests: list[dict] = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802  (http.server API)
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = raw.decode(errors="replace")
                state.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "payload": payload,
                    }
                )
                if state.behavior == "ok":
                    model = payload.get("model") if isinstance(payload, dict) else None
                    body = json.dumps(
                        {
                            "model": model,
                            "answers": {
                                "next_action": {
                                    "type": "choice",
                                    "choice": state.answer_picker(payload),
                                    "confidence": 0.9,
                                }
                            },
                            "usage": {"input_tokens": 120, "output_tokens": 4},
                        }
                    ).encode()
                    state.respond(self, 200, body)
                elif state.behavior == "slow":
                    time.sleep(state.delay)
                    state.respond(self, 200, b"{}")
                elif state.behavior == "auth_error":
                    state.respond(self, 401, b'{"error":"invalid key fixture-body-marker"}')
                elif state.behavior == "forbidden":
                    state.respond(self, 403, b'{"error":"permission fixture-body-marker"}')
                elif state.behavior == "rate_limited":
                    state.respond(self, 429, b'{"error":"fixture-body-marker"}')
                elif state.behavior == "server_error":
                    state.respond(self, 500, b'{"error":"fixture-body-marker"}')
                elif state.behavior == "invalid_json":
                    state.respond(self, 200, b"not json fixture-body-marker")
                elif state.behavior == "malformed":
                    # HTTP 200 with an answer that is not in the frame; the choice value is a marker
                    # that must never be echoed into an error, a traceback or a log.
                    body = json.dumps(
                        {
                            "model": "jev-1.13.0",
                            "answers": {
                                "next_action": {"type": "choice", "choice": "fixture-body-marker-not-an-option"}
                            },
                            "usage": {"input_tokens": 1, "output_tokens": 0},
                        }
                    ).encode()
                    state.respond(self, 200, body)
                elif state.behavior == "truncated_chunked":
                    # Declares a 31-byte chunk and then closes without the trailing bytes/terminator.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'1f\r\n{"model":"jev-1.13.0","answers":')
                    self.close_connection = True
                elif state.behavior == "reset":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                elif state.behavior == "redirect":
                    state.respond(self, 302, b"", location=state.trap_url)
                elif state.behavior == "redirect_self":
                    state.respond(self, 302, b"", location=f"{state.url}/same-origin")
                else:
                    state.respond(self, 500, b"unknown behavior")

            def log_message(self, *args):
                pass

        class TrapHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802  (http.server API)
                state.trap_requests.append(
                    {"path": self.path, "authorization": self.headers.get("Authorization")}
                )
                state.respond(self, 200, b"{}")

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._trap = ThreadingHTTPServer(("127.0.0.1", 0), TrapHandler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self.trap_url = f"http://127.0.0.1:{self._trap.server_port}"
        self._threads = [
            threading.Thread(target=self._server.serve_forever, daemon=True),
            threading.Thread(target=self._trap.serve_forever, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    @staticmethod
    def respond(handler: BaseHTTPRequestHandler, code: int, body: bytes, location: str | None = None) -> None:
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if location is not None:
            handler.send_header("Location", location)
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client timed out or refused the redirect; the test already saw what it needed

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._trap.shutdown()
        self._trap.server_close()
        for thread in self._threads:
            thread.join(timeout=2)


@pytest.fixture
def fake_jev():
    server = FakeJev()
    try:
        yield server
    finally:
        server.close()

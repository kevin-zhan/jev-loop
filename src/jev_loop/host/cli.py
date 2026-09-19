"""CLI entry point used by the pi extension and by operators.

The ``rpc`` command consumes one JSON object on stdin and prints one JSON object on stdout.
Task content never appears in process arguments or shell history.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .service import MAX_RPC_BYTES, dispatch, run_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-loop-host")
    subparsers = parser.add_subparsers(dest="command", required=True)
    rpc = subparsers.add_parser("rpc", help="read one host request as JSON from stdin")
    rpc.add_argument("--home", type=Path)
    worker = subparsers.add_parser("worker", help="internal per-run worker (normally spawned by start)")
    worker.add_argument("--run-dir", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "worker":
        raise SystemExit(run_worker(args.run_dir))

    raw = sys.stdin.buffer.read(MAX_RPC_BYTES + 1)
    if len(raw) > MAX_RPC_BYTES:
        _reply({"ok": False, "error": f"request exceeds {MAX_RPC_BYTES} bytes"}, exit_code=2)
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        response = dispatch(request, home=args.home)
    except Exception as error:
        _reply({"ok": False, "error": str(error)}, exit_code=1)
    _reply(response)


def _reply(payload, *, exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    raise SystemExit(exit_code)

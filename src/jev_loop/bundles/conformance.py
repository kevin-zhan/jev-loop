"""Reusable conformance checks any bundle author can run.

``conformance_report`` answers three separate questions and never blurs them:

1. **Static contract** — the same checks as ``jev-loop bundle validate`` (enforced versus
   advisory versus not checked).
2. **Side effects while discovering and inspecting** — a subprocess probe proves that no
   module from the bundle directory was imported, no subprocess was spawned, no socket was
   opened and no file inside the bundle changed.  Discovery and validation are inert.
3. **Author tests** (``--run-tests``) — the bundle's own offline tests, executed with a
   bounded timeout in a child process.  They are the *author's trusted code*: running them
   is not a sandbox, and their presence does not prove that a bundle has no side effects.

Runtime semantics — cognition flow, guards, resource exclusivity, stop behaviour and
verifier independence — are always reported as ``not_checked`` here: they need a real
managed run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .resolve import BundleRefError, resolve_bundle_ref
from .validate import fingerprint, scan_bundle_files, validate_ref

Json = Any

PROBE_TIMEOUT_SECONDS = 60.0
TESTS_TIMEOUT_SECONDS = 300.0
FORBIDDEN_AUDIT_EVENTS = (
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.bind",
    "urllib.Request",
    "shutil.copyfile",
    "shutil.rmtree",
)

_PROBE_SOURCE = r'''
import json, os, sys

project_root, ref = sys.argv[1], sys.argv[2]
bundle_dir = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None

from jev_loop.bundles.validate import fingerprint, scan_bundle_files

def snapshot(root):
    """The same bounded, symlink-free snapshot the static checks use.

    Hidden and cache directories are never entered, symlinks are never followed, and an incomplete
    or failed scan yields no fingerprint instead of a partial one.  The shape is identical whether
    or not there is a bundle directory to scan: the built-in bundle has none, and that is reported
    as ``not_applicable`` rather than as "no file changed".
    """
    shape = {
        "fingerprint": None,
        "coverage": "",
        "files": 0,
        "incomplete": False,
        "complete": True,
        "not_applicable": True,
        "symlinks_skipped": 0,
        "read_failures": [],
    }
    if root is None:
        shape["coverage"] = "not applicable: this reference has no bundle directory to scan"
        return shape
    from pathlib import Path as _Path

    scan = scan_bundle_files(_Path(root))
    return {
        "fingerprint": fingerprint(scan),
        "coverage": scan.coverage,
        "files": len(scan.files),
        "incomplete": scan.incomplete,
        "complete": scan.complete,
        "not_applicable": False,
        "symlinks_skipped": scan.symlinks_skipped,
        "read_failures": list(scan.read_failures),
    }

before_modules = set(sys.modules)
before = snapshot(bundle_dir)
events = []

def hook(event, args):
    if event.startswith(("subprocess.", "os.exec", "os.spawn", "socket.", "urllib.")) or event in {
        "os.system", "shutil.copyfile", "shutil.rmtree", "os.posix_spawn",
    }:
        events.append(event)

sys.addaudithook(hook)

from jev_loop.bundles import discover_bundles, validate_ref, find_bundle

entries = [entry.to_json() for entry in discover_bundles(project_root)]
name = bundle_dir.split(os.sep)[-1] if bundle_dir else None
entry = find_bundle(project_root, name) if name else None
report = validate_ref(ref, project_root)

imported_from_bundle = []
for module_name, module in list(sys.modules.items()):
    if module_name in before_modules:
        continue
    origin = getattr(module, "__file__", None)
    if origin and bundle_dir and os.path.realpath(origin).startswith(os.path.realpath(bundle_dir) + os.sep):
        imported_from_bundle.append(module_name)

after = snapshot(bundle_dir)
if before["not_applicable"]:
    changed = []  # nothing to compare; the scope says so instead of implying "unchanged"
elif not before["complete"] or not after["complete"]:
    # A partial or failed read can never support a "nothing changed" claim.
    changed = ["unknown: the bounded snapshot was incomplete or a read failed"]
elif before["fingerprint"] != after["fingerprint"]:
    changed = ["content changed"]
else:
    changed = []
blocked = sorted(event for event in events if event.startswith(("socket.", "urllib.")))
print(json.dumps({
    "imported_from_bundle": sorted(imported_from_bundle),
    "audit_events": sorted(set(events)),
    "network_events": blocked,
    "files_changed": changed,
    "scan_coverage": before["coverage"],
    "scan_incomplete": bool(not before["complete"] or not after["complete"]),
    "scan_not_applicable": bool(before["not_applicable"]),
    "fingerprint": before["fingerprint"],
    "read_failures": before["read_failures"] + after["read_failures"],
    "symlinks_skipped": before["symlinks_skipped"] + after["symlinks_skipped"],
    "bundle_module_on_path": bool(bundle_dir and os.path.realpath(bundle_dir) in
                                   [os.path.realpath(item) for item in sys.path if item]),
    "discovered": [item["name"] for item in entries],
    "validated": bool(report["ok"]),
    "validation_errors": [item["code"] for item in report["errors"]],
}))
'''


def run_probe(ref: str, project_root: Path | str, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Json]:
    """Prove discovery/validation are inert, in a child process, without touching the bundle."""
    project = Path(project_root).expanduser().resolve()
    bundle_dir: Path | None = None
    try:
        resolved = resolve_bundle_ref(ref, project)
    except BundleRefError:
        resolved = None
    if resolved is not None and resolved.manifest_path is not None:
        bundle_dir = resolved.manifest_path.parent
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(item for item in sys.path if item)
    command = [sys.executable, "-c", _PROBE_SOURCE, str(project), ref, str(bundle_dir) if bundle_dir else "-"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
            cwd=str(project),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "detail": f"probe exceeded {timeout:g}s"}
    if completed.returncode != 0:
        # Never copy raw child output into the report: it can contain paths or values from the
        # bundle's own files.  Report a fixed reason plus sizes only.
        return {
            "ok": False,
            "status": "probe_failed",
            "detail": "the isolated probe process did not complete; raw output is withheld",
            "diagnostics": {
                "exit_code": completed.returncode,
                "stderr_bytes": len(completed.stderr or ""),
                "stdout_bytes": len(completed.stdout or ""),
            },
        }
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "status": "unreadable", "detail": "probe produced no JSON report"}
    problems: list[str] = []
    if payload["imported_from_bundle"]:
        problems.append(f"bundle modules were imported: {payload['imported_from_bundle']}")
    if payload["audit_events"]:
        problems.append(f"unexpected process/network activity: {payload['audit_events']}")
    if payload["files_changed"]:
        problems.append(f"files inside the bundle changed: {payload['files_changed']}")
    if payload["bundle_module_on_path"]:
        problems.append("the bundle directory was placed on sys.path during discovery")
    if payload.get("scan_incomplete"):
        problems.append(
            "the bounded file snapshot was incomplete or a read failed, so 'no file changed' cannot be claimed"
        )
    return {
        "ok": not problems,
        "status": "ok" if not problems else "side_effects_detected",
        "detail": _probe_detail(problems),
        "observed": payload,
        "scope": (
            "conformance runs one isolated probe subprocess (and, with --run-tests, the author tests); "
            "inside that probe, discovery and validation started no further process, imported no bundle "
            "module and opened no socket. It does not claim that the interpreter imported nothing at all, "
            "and it reads only regular files inside the bundle within the bounded scan policy"
        ),
    }


def run_tests(bundle_directory: Path, *, timeout: float = TESTS_TIMEOUT_SECONDS) -> dict[str, Json]:
    """Run the bundle author's own tests.  Trusted code, bounded by a timeout, no sandbox."""
    tests_dir = Path(bundle_directory) / "tests"
    if not tests_dir.is_dir():
        return {
            "status": "no_tests",
            "detail": "no tests/ directory; add the offline tests the author checklist asks for",
            "exit_code": None,
        }
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(bundle_directory),
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "detail": f"author tests exceeded {timeout:g}s",
            "command": " ".join(command),
            "exit_code": None,
        }
    duration = round(time.monotonic() - started, 3)
    tail = "\n".join((completed.stdout or completed.stderr or "").strip().splitlines()[-25:])
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "detail": "author tests are trusted code run in a child process, not a sandbox",
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "output_tail": tail,
    }


def conformance_report(
    ref: str,
    project_root: Path | str,
    *,
    run_tests_flag: bool = False,
    tests_timeout: float = TESTS_TIMEOUT_SECONDS,
) -> dict[str, Json]:
    """Static contract + inert discovery proof, plus optional author tests."""
    project = Path(project_root).expanduser().resolve()
    static = validate_ref(ref, project)
    probe = run_probe(ref, project)
    resolved = static.get("resolved") or {}
    manifest_path = resolved.get("manifest_path")
    bundle_directory = Path(manifest_path).parent if manifest_path else None
    tests: dict[str, Json] = {
        "status": "not_run",
        "detail": "pass --run-tests to execute the bundle's own tests (trusted author code, bounded by a timeout)",
    }
    if not static["ok"]:
        tests = {
            "status": "skipped",
            "detail": "the static contract failed, so the author tests were not executed",
        }
    elif run_tests_flag and bundle_directory is not None:
        tests = run_tests(bundle_directory, timeout=tests_timeout)
    ok = bool(static["ok"]) and bool(probe.get("ok")) and tests.get("status") in {"not_run", "passed", "no_tests"}
    scan = scan_bundle_files(bundle_directory) if bundle_directory is not None else None
    return {
        "schema_version": 1,
        "mode": "conformance",
        "ref": ref,
        "project_root": str(project),
        "ok": ok,
        "static": static,
        "side_effects": probe,
        "author_tests": tests,
        "runtime_semantics": {
            "status": "not_checked",
            "detail": "cognition flow, guards, resource exclusivity, stop behaviour and verifier independence "
            "need a managed run; this report never claims them",
        },
        "bundle_digest": fingerprint(scan) if scan is not None else None,
        "bundle_digest_scope": scan.coverage if scan is not None else "not applicable: no bundle directory to scan",
    }





def _probe_detail(problems: list[str]) -> str:
    if not problems:
        return "discovery and validation imported no bundle code and spawned nothing"
    return "; ".join(problems)

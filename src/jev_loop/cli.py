"""The host-neutral ``jev-loop`` command line.

Any agent that can run a command and read JSON can create, discover, inspect, validate and
invoke a bundle with this entry point; pi is only one optional client of the same runtime.

Exit codes (stable, also printed in ``--help``):

==== ==========================================================================
0    the command succeeded
1    unexpected internal error (details withheld)
3    the request was invalid, the bundle did not resolve, or validation failed
==== ==========================================================================

``bundle init`` never overwrites an existing path.  ``--json`` always prints one object with
``ok`` and ``exit_code``, including every failure path; a valid ``--help`` still exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from .bundles import (
    BundleRefError,
    ManifestError,
    ScaffoldError,
    conformance_report,
    create_bundle,
    discover_bundles,
    discovery_root,
    resolve_bundle_ref,
    validate_ref,
)
from .bundles.manifest import load_manifest
from .bundles.validate import manifest_summary

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_INVALID = 3

INTERNAL_ERROR_DETAIL = "unexpected internal error; details were withheld"
USAGE_ERROR_DETAIL = "invalid command-line usage; run jev-loop --help"

INIT_MARKER = (
    "This is a scaffold: it proves the plumbing only, never that a user's goal was implemented or verified."
)


class _UsageError(Exception):
    """argparse rejected the command line; the raw argument text is never echoed."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        raise _UsageError(message)


def _add_project_root(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Accept --project-root at every advertised position (default: the current directory).

    ``default=argparse.SUPPRESS`` on every parser is what makes this work: a subparser must not
    write its own default back over a value the root parser already parsed, and the call site
    reads the attribute with ``getattr``.
    """
    parser.add_argument(
        "--project-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="project root (default: the current directory)",
    )
    return parser


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="jev-loop",
        description=(
            "Jev Loop runtime and bundle tooling. Bundles live in "
            "<project_root>/.agents/jev-bundle/<name>/ with bundle.json and BUNDLE.md."
        ),
        epilog=(
            "exit codes: 0 success, 1 unexpected error, 3 invalid request, unresolved bundle or failed validation"
        ),
    )
    _add_project_root(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bundle = _add_project_root(subparsers.add_parser("bundle", help="create, list, inspect and validate bundles"))
    bundle_sub = bundle.add_subparsers(dest="bundle_command", required=True)

    listing = _add_project_root(bundle_sub.add_parser("list", help="list the bundles discovered in the project"))
    listing.add_argument("--json", action="store_true", help="print one JSON object")

    show = _add_project_root(
        bundle_sub.add_parser("show", help="show one bundle's metadata and declarations")
    )
    show.add_argument("ref", help="'diagnostic', project:<name>, a discovered name, or a manifest path")
    show.add_argument("--json", action="store_true", help="print one JSON object")

    validate = _add_project_root(bundle_sub.add_parser("validate", help="statically validate one bundle"))
    validate.add_argument("ref", help="'diagnostic', project:<name>, a discovered name, or a manifest path")
    validate.add_argument("--json", action="store_true", help="print one JSON object")

    conformance = _add_project_root(
        bundle_sub.add_parser(
            "conformance", help="static contract + inert-discovery probe (+ optional author tests)"
        )
    )
    conformance.add_argument("ref", help="'diagnostic', project:<name>, a discovered name, or a manifest path")
    conformance.add_argument(
        "--run-tests",
        action="store_true",
        help="also run the bundle's own tests (trusted author code, child process, bounded timeout)",
    )
    conformance.add_argument("--timeout", type=float, default=None, help="author-test timeout in seconds")
    conformance.add_argument("--json", action="store_true", help="print one JSON object")

    init = _add_project_root(bundle_sub.add_parser("init", help="create a new bundle scaffold (never overwrites)"))
    init.add_argument("name", help="bundle name: lowercase letters, digits and single hyphens")
    init.add_argument("--dir", type=Path, default=None, help="target directory (default: the discovery root)")
    init.add_argument("--template", default="basic", help="packaged template to render (default: basic)")
    init.add_argument("--version", default="0.1.0", help="initial bundle version (default: 0.1.0)")
    init.add_argument("--description", default=None, help="one-line description for the manifest and BUNDLE.md")
    init.add_argument("--json", action="store_true", help="print one JSON object")

    rpc = subparsers.add_parser(
        "rpc", help="run one host request from stdin as JSON (alias of 'jev-loop-host rpc')"
    )
    rpc.add_argument("--home", type=Path, help="host state directory (default: $JEV_LOOP_HOME)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    try:
        args = _parser().parse_args(raw)
    except _UsageError:
        return _emit(
            action="usage",
            ok=False,
            exit_code=EXIT_INVALID,
            json_mode=json_mode,
            error=USAGE_ERROR_DETAIL,
        )
    if args.command == "rpc":
        # One implementation: the alias reuses the long-standing host entry point verbatim.
        from .host.cli import main as host_main

        forwarded = ["rpc"] + (["--home", str(args.home)] if args.home else [])
        previous = sys.argv
        sys.argv = ["jev-loop-host", *forwarded]
        try:
            return host_main()
        finally:
            sys.argv = previous

    action = f"bundle.{args.bundle_command}"
    try:
        project_root = _project_root(getattr(args, "project_root", None))
        if args.bundle_command == "list":
            return _emit(action=action, ok=True, exit_code=EXIT_OK, json_mode=args.json, result=_list(project_root))
        if args.bundle_command == "show":
            return _emit(
                action=action,
                ok=True,
                exit_code=EXIT_OK,
                json_mode=args.json,
                result=_show(args.ref, project_root),
            )
        if args.bundle_command == "validate":
            result = validate_ref(args.ref, project_root)
            return _emit(
                action=action,
                ok=bool(result["ok"]),
                exit_code=EXIT_OK if result["ok"] else EXIT_INVALID,
                json_mode=args.json,
                result=result,
                error=None if result["ok"] else _first_error(result),
            )
        if args.bundle_command == "conformance":
            result = conformance_report(
                args.ref,
                project_root,
                run_tests_flag=bool(args.run_tests),
                tests_timeout=args.timeout if args.timeout is not None else 300.0,
            )
            return _emit(
                action=action,
                ok=bool(result["ok"]),
                exit_code=EXIT_OK if result["ok"] else EXIT_INVALID,
                json_mode=args.json,
                result=result,
                error=None if result["ok"] else _conformance_error(result),
            )
        if args.bundle_command == "init":
            target = args.dir if args.dir is not None else discovery_root(project_root) / args.name
            created = create_bundle(
                target,
                args.name,
                template=args.template,
                version=args.version,
                description=args.description,
                project_root=project_root,
            )
            return _emit(
                action=action,
                ok=True,
                exit_code=EXIT_OK,
                json_mode=args.json,
                result={"created": created.to_json(), "note": INIT_MARKER},
            )
        raise _UsageError(action)  # pragma: no cover - argparse enforces the command set
    except (BundleRefError, ManifestError, ScaffoldError, ValueError, OSError) as error:
        return _emit(
            action=action,
            ok=False,
            exit_code=EXIT_INVALID,
            json_mode=args.json,
            error=str(error),
        )
    except Exception:  # noqa: BLE001 - a broken CLI must still exit with a stable, safe envelope
        return _emit(
            action=action,
            ok=False,
            exit_code=EXIT_UNEXPECTED,
            json_mode=json_mode,
            error=INTERNAL_ERROR_DETAIL,
        )


def _project_root(configured: Path | None) -> Path:
    root = (configured if configured is not None else Path.cwd()).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    return root


def _list(project_root: Path) -> dict[str, Any]:
    entries = discover_bundles(project_root)
    return {
        "project_root": str(project_root),
        "discovery_root": str(discovery_root(project_root)),
        "bundles": [entry.to_json() for entry in entries],
        "valid": sorted(entry.name for entry in entries if entry.valid),
        "unusable": sorted(entry.name for entry in entries if not entry.valid),
    }


def _show(ref: str, project_root: Path) -> dict[str, Any]:
    resolved = resolve_bundle_ref(ref, project_root)
    if resolved.is_diagnostic:
        return {
            "kind": "diagnostic",
            "provenance": "diagnostic",
            "manifest_path": None,
            "manifest": None,
            "note": "built-in contract probe: it verifies bridge semantics, never that a user task was done",
        }
    manifest = load_manifest(resolved.manifest_path) if resolved.manifest_path else None
    report = validate_ref(ref, project_root)
    return {
        "kind": resolved.kind,
        "provenance": resolved.provenance,
        "manifest_path": str(resolved.manifest_path) if resolved.manifest_path else None,
        "manifest": manifest_summary(manifest) if manifest is not None else None,
        "warnings": report["warnings"],
        "advisory": report["advisory"],
        "not_checked": report["not_checked"],
    }


def _first_error(report: Mapping[str, Any]) -> str:
    errors = list(report.get("errors") or ())
    if errors:
        return f"{errors[0]['code']}: {errors[0]['detail']}"
    return "bundle validation failed"


def _conformance_error(report: Mapping[str, Any]) -> str:
    static = report.get("static") or {}
    if not static.get("ok", False):
        return _first_error(static)
    side_effects = report.get("side_effects") or {}
    if not side_effects.get("ok", False):
        return f"side_effects: {side_effects.get('detail', 'probe failed')}"
    tests = report.get("author_tests") or {}
    if tests.get("status") == "failed":
        return f"author tests failed with exit code {tests.get('exit_code')}"
    return "conformance check failed"


def _emit(
    *,
    action: str,
    ok: bool,
    exit_code: int,
    json_mode: bool,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> int:
    if json_mode:
        payload = {
            "schema_version": 1,
            "action": action,
            "ok": ok,
            "exit_code": exit_code,
            "error": error,
            "result": result,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        if error:
            print(f"{action}: {error}", file=sys.stderr)
        if result is not None:
            print(_human(action, result))
    return exit_code


def _human(action: str, result: Mapping[str, Any]) -> str:
    if action == "bundle.list":
        lines = [f"project root: {result['project_root']}", f"discovery root: {result['discovery_root']}"]
        if not result["bundles"]:
            lines.append("no bundles found")
        for entry in result["bundles"]:
            manifest = entry.get("manifest") or {}
            label = manifest.get("name") or entry["name"]
            status = entry["status"]
            detail = f" [{'; '.join(entry['problems'])}]" if entry["problems"] else ""
            lines.append(f"  {label} {manifest.get('version') or ''} ({status}) {entry['provenance']}{detail}")
        return "\n".join(lines)
    if action == "bundle.show":
        manifest = result.get("manifest") or {}
        lines = [
            f"name: {manifest.get('name')}",
            f"version: {manifest.get('version')}",
            f"schema_version: {manifest.get('schema_version')}",
            f"entrypoint: {manifest.get('entrypoint')}",
            f"bundle root: {manifest.get('bundle_root')}",
            f"instructions: {manifest.get('instructions')}",
            f"scaffold: {manifest.get('scaffold')}",
        ]
        for warning in result.get("warnings") or ():
            lines.append(f"warning: {warning['code']}: {warning['detail']}")
        return "\n".join(lines)
    if action == "bundle.validate":
        lines = [f"valid: {'yes' if result['ok'] else 'no'}"]
        for key, label in (("errors", "error"), ("warnings", "warning"), ("advisory", "advisory")):
            for finding in result.get(key) or ():
                lines.append(f"{label}: {finding['code']}: {finding['detail']}")
        for entry in result.get("not_checked") or ():
            lines.append(f"not checked: {entry['item']}: {entry['detail']}")
        return "\n".join(lines)
    if action == "bundle.conformance":
        static = result["static"]
        return "\n".join(
            [
                f"static contract: {'ok' if static['ok'] else 'failed'}",
                f"inert discovery: {result['side_effects'].get('status')} — {result['side_effects'].get('detail')}",
                f"author tests: {result['author_tests'].get('status')}",
                f"runtime semantics: {result['runtime_semantics']['status']}",
            ]
        )
    if action == "bundle.init":
        created = result["created"]
        lines = [f"created scaffold: {created['directory']}"]
        lines.extend(f"  {path}" for path in created["files"])
        lines.append(result["note"])
        lines.extend(f"next: {command}" for command in created["next_commands"])
        lines.extend(f"note: {note}" for note in created.get("notes", ()))
        return "\n".join(lines)
    return json.dumps(dict(result), ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())

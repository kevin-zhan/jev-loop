"""Offline preflight for a live Jev run: ``jev-loop-doctor``.

The doctor inspects local configuration only.  It makes no network request, never prints a
credential value or anything derived from one, and never claims that authentication,
connectivity or model availability has been verified — those stay explicitly
``not_checked`` entries, not green lights.  A green report means the local preflight
passed; it is not a live-acceptance result.

Every exit path prints the same machine-readable envelope with ``--json``, including
command-line usage errors (exit 3) and unexpected internal errors (exit 1); those paths print
fixed messages and never echo exception text or supplied argument values.

Exit codes (stable; also documented in ``--help`` and in the docs):

==== ==========================================================================
0    the checked local configuration is valid (credential required unless --offline)
1    unexpected internal error
2    the credential is missing, blank or unusable
3    a non-secret setting is invalid, a command-line usage error, or an unsupported platform
==== ==========================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

from .policies.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    ConfigError,
    CredentialError,
    api_key_from_env,
    validate_api_url,
    validate_model,
    validate_timeout,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CREDENTIAL = 2
EXIT_CONFIG = 3

OFFLINE_DEMO_COMMAND = "uv run jev-loop-demo --scenario clean"
LIVE_RUN_COMMAND = "uv run python examples/live-files/run.py --max-steps 8"
NOT_CHECKED = ("authentication", "connectivity", "model_availability")
BUNDLE_CONFIG_KEYS = ("model", "api_url", "timeout")
INTERNAL_ERROR_DETAIL = "unexpected internal error; details were withheld"
USAGE_ERROR_DETAIL = "invalid command-line usage; run jev-loop-doctor --help"

# Distinguishes "the manifest/config did not mention this key" from "the key is present and null".
# ``dict.get(key, default)`` cannot tell those apart, and an explicit null must not silently become
# the default: it is a configuration error.
_MISSING = object()


class _UsageError(Exception):
    """argparse rejected the command line; reported as a configuration failure (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        # argparse messages can contain the raw argument, so the message is dropped and a fixed
        # text is reported by the caller instead.
        raise _UsageError(message)


def _check(name: str, status: str, detail: str = "") -> dict[str, str]:
    entry = {"name": name, "status": status}
    if detail:
        entry["detail"] = detail
    return entry


def _read_bundle(path: Path) -> tuple[dict[str, Any], str, bool]:
    """Read a bundle manifest for its non-secret config only; never import or execute it."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {}, f"cannot read bundle manifest ({error.__class__.__name__})", False
    if not isinstance(manifest, dict):
        return {}, "bundle manifest must be a JSON object", False
    if manifest.get("schema_version") != 1:
        return {}, "bundle schema_version must be 1", False
    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, str) or ":" not in entrypoint:
        return {}, "bundle entrypoint must be shaped like 'module:function'", False
    python_path = (path.resolve().parent / str(manifest.get("python_path", "."))).resolve()
    if not python_path.is_dir():
        return {}, "bundle python_path is not a directory", False
    config = manifest.get("config")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return {}, "bundle config must be a JSON object", False
    selected = {key: config[key] for key in BUNDLE_CONFIG_KEYS if key in config}
    return selected, f"manifest {path} is well-formed", True


def _resolve_text(flag: str | None, manifest: object, default: str, *, label: str) -> str:
    """Resolve one text setting, distinguishing 'not given' from 'given but invalid'.

    A flag that is present but empty is an error (no silent fallback to the default); a manifest
    value that is absent (``_MISSING``) falls back to the default, while an explicit ``null`` or a
    value of the wrong type is rejected instead of being coerced with ``str()``.
    """
    if flag is not None:
        if not flag:
            raise ConfigError(f"--{label} was provided but is empty")
        return flag
    if manifest is _MISSING:
        return default
    if not isinstance(manifest, str) or not manifest.strip():
        raise ConfigError(f"bundle config {label} must be a non-empty string")
    return manifest


def _resolve_timeout(flag: float | None, manifest: object, default: float) -> float:
    if flag is not None:
        return flag
    if manifest is _MISSING:
        return default
    if isinstance(manifest, bool) or not isinstance(manifest, (int, float)):
        raise ConfigError("bundle config timeout must be a number of seconds")
    return float(manifest)


def _environment_checks() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if sys.version_info >= (3, 12):  # noqa: UP036  (also reachable from a source checkout with an older interpreter)
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        checks.append(_check("python", "ok", version))
    else:
        checks.append(_check("python", "fail", "jev-loop requires Python 3.12 or newer"))
    try:
        import fcntl  # noqa: F401  (presence check only)

        checks.append(_check("platform", "ok", f"{sys.platform} (fcntl available)"))
    except ImportError:
        checks.append(_check("platform", "fail", "the managed host needs Unix fcntl; on Windows use WSL"))
    return checks


def _endpoint_checks(api_url: str, model: str, timeout: object) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    try:
        checks.append(_check("endpoint", "ok", validate_api_url(api_url)))
    except ConfigError as error:
        checks.append(_check("endpoint", "fail", str(error)))
    try:
        checks.append(_check("model", "ok", validate_model(model)))
    except ConfigError as error:
        checks.append(_check("model", "fail", str(error)))
    try:
        seconds = validate_timeout(timeout)  # type: ignore[arg-type]
        checks.append(_check("timeout", "ok", f"{seconds:g}s"))
    except ConfigError as error:
        checks.append(_check("timeout", "fail", str(error)))
    return checks


def _credential_check(
    env_name: str, *, offline: bool, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    if offline:
        return _check("credential", "skipped", f"offline mode: {env_name} is not required")
    try:
        api_key_from_env(env_name, environ=environ)
    except CredentialError as error:
        return _check("credential", "fail", str(error))
    return _check("credential", "ok", f"{env_name} is present (the value is never shown)")


def run_checks(
    *,
    api_url: str,
    model: str,
    timeout: object,
    env_name: str,
    offline: bool,
    environ: Mapping[str, str] | None = None,
    settings_error: str | None = None,
) -> list[dict[str, str]]:
    """Run every offline check and return the entries in report order."""
    checks = _environment_checks()
    if settings_error is None:
        checks.extend(_endpoint_checks(api_url, model, timeout))
    else:
        checks.append(_check("settings", "fail", settings_error))
    checks.append(_credential_check(env_name, offline=offline, environ=environ))
    for name in NOT_CHECKED:
        checks.append(_check(name, "not_checked", "requires a live request; the doctor makes none"))
    return checks


def _verdict(checks: Sequence[Mapping[str, str]]) -> tuple[bool, int]:
    failed = [check for check in checks if check["status"] == "fail"]
    if not failed:
        return True, EXIT_OK
    if all(check["name"] == "credential" for check in failed):
        return False, EXIT_CREDENTIAL
    return False, EXIT_CONFIG


def _next_steps(checks: Sequence[Mapping[str, str]], *, offline: bool) -> tuple[list[str], list[str]]:
    """Recommend the next command for the state that was actually observed.

    A paid live command is only recommended when the live preflight passed, and then it is
    explicitly marked as requiring authorization rather than as a verified capability.
    """
    ready = not any(check["status"] == "fail" for check in checks)
    if offline:
        return (
            [OFFLINE_DEMO_COMMAND],
            [f"offline mode: {DEFAULT_API_KEY_ENV} is not required and no live command is recommended"],
        )
    if ready:
        return (
            [LIVE_RUN_COMMAND],
            [
                "requires explicit authorization: this command makes real, paid Jev decision requests",
                "authentication, connectivity and model availability are not checked by this preflight",
            ],
        )
    return (
        [OFFLINE_DEMO_COMMAND],
        ["fix the reported checks, then re-run this preflight before running any live command"],
    )


def _envelope(checks: Sequence[Mapping[str, str]], *, offline: bool) -> dict[str, Any]:
    next_commands, next_notes = _next_steps(checks, offline=offline)
    failed = any(check["status"] == "fail" for check in checks)
    return {
        "schema_version": 1,
        "mode": "preflight",
        "credential_required": not offline,
        "network_requests": 0,
        "ok": not failed,
        "configuration_ready": not failed,
        "checks": list(checks),
        "next_commands": next_commands,
        "next_notes": next_notes,
    }


def _human(report: Mapping[str, Any]) -> str:
    lines = ["jev-loop doctor - local configuration preflight (no network request)"]
    for check in report["checks"]:
        detail = f"  {check.get('detail', '')}" if check.get("detail") else ""
        lines.append(f"  {check['status']:<12}{check['name']:<18}{detail}")
    lines.append("")
    lines.append(f"configuration-ready: {'yes' if report['configuration_ready'] else 'no'}")
    lines.append("authentication, connectivity and model availability were not checked (no request was made).")
    lines.append("next:")
    for command in report["next_commands"]:
        lines.append(f"  {command}")
    for note in report.get("next_notes", ()):
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def build_report(
    *,
    api_url: str | None,
    model: str | None,
    timeout: float | None,
    env_name: str,
    offline: bool,
    bundle: Path | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    config: dict[str, Any] = {}
    bundle_check: dict[str, str] | None = None
    if bundle is not None:
        config, problem, valid = _read_bundle(bundle)
        bundle_check = _check("bundle", "ok" if valid else "fail", problem)

    settings_error: str | None = None
    try:
        effective_url = _resolve_text(
            api_url, config.get("api_url", _MISSING), DEFAULT_API_URL, label="api-url"
        )
        effective_model = _resolve_text(
            model, config.get("model", _MISSING), DEFAULT_MODEL, label="model"
        )
        effective_timeout = _resolve_timeout(timeout, config.get("timeout", _MISSING), DEFAULT_TIMEOUT)
    except ConfigError as error:
        settings_error = str(error)
        effective_url, effective_model, effective_timeout = DEFAULT_API_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT

    checks = run_checks(
        api_url=effective_url,
        model=effective_model,
        timeout=effective_timeout,
        env_name=env_name,
        offline=offline,
        environ=environ,
        settings_error=settings_error,
    )
    if bundle_check is not None:
        if bundle_check["status"] == "fail":
            checks.insert(0, bundle_check)
        else:
            checks.insert(len(checks) - len(NOT_CHECKED), bundle_check)

    _, exit_code = _verdict(checks)
    return _envelope(checks, offline=offline), exit_code


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="jev-loop-doctor",
        description="Check local Jev configuration without contacting the network or printing a credential.",
        epilog=(
            "exit codes: 0 local configuration ok, 1 unexpected error, 2 credential missing/blank/unusable, "
            "3 invalid non-secret configuration, invalid command-line usage, or unsupported platform"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of the human report")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="check the installation and runtime only; no credential is required",
    )
    parser.add_argument(
        "--env-name",
        default=DEFAULT_API_KEY_ENV,
        help=f"credential variable name (default {DEFAULT_API_KEY_ENV})",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        help="read a bundle manifest's non-secret config (model/api_url/timeout); never imports it",
    )
    parser.add_argument("--api-url", default=None, help="override the endpoint used for the check")
    parser.add_argument("--model", default=None, help="override the model used for the check")
    parser.add_argument("--timeout", type=float, default=None, help="override the timeout used for the check")
    return parser


def _emit(report: Mapping[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(_human(report))


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    offline_mode = "--offline" in raw
    try:
        args = _parser().parse_args(raw)
    except _UsageError:
        report = _envelope([_check("usage", "fail", USAGE_ERROR_DETAIL)], offline=offline_mode)
        _emit(report, json_mode=json_mode)
        return EXIT_CONFIG

    try:
        report, exit_code = build_report(
            api_url=args.api_url,
            model=args.model,
            timeout=args.timeout,
            env_name=args.env_name,
            offline=args.offline,
            bundle=args.bundle,
        )
    except Exception:  # noqa: BLE001  (a broken doctor must still exit with a stable, secret-free report)
        report = _envelope([_check("internal", "fail", INTERNAL_ERROR_DETAIL)], offline=args.offline)
        _emit(report, json_mode=args.json)
        return EXIT_UNEXPECTED

    _emit(report, json_mode=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

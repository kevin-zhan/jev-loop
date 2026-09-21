"""Real-filesystem reference environment for the live Jev example.

This is a reference example with synthetic data: not a production adapter and not a speed
benchmark.  It owns one private workspace directory that it creates itself, only touches
files it wrote itself with fixed synthetic content, and never deletes anything.

The three roles the kernel expects are all visible here:

* :class:`LiveFilesEnvironment` observes the real directory, offers the file operations that
  are valid right now, and executes them for real (re-checking its preconditions first).
* Decisions are not made here; the ``JevPolicy`` in ``run.py``/``controller.py`` asks the
  model to choose among the offered options.
* :class:`LiveFilesVerifier` independently re-reads the directory and checks the success
  criteria itself; it never consults the observation or the model's answer.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jev_loop import (
    ActionCandidate,
    Effect,
    ExecutionReceipt,
    Idempotency,
    Observation,
    ReceiptStatus,
    RuntimeState,
    VerifierResult,
    VerifyVerdict,
)
from jev_loop.ports import ExecutionRejected, ObservationError, default_validate

ALPHA = "inbox/alpha.txt"
BETA = "inbox/beta.txt"
GAMMA = "inbox/gamma.log"
ARCHIVED_GAMMA = "archive/gamma.log"
SEED: dict[str, str] = {ALPHA: "draft", BETA: "ready", GAMMA: "noise"}
TARGET_ALPHA = "ready"
TASK_FILES = (ALPHA, BETA, GAMMA, ARCHIVED_GAMMA)
MAX_CONTENT_BYTES = 64

GOAL = (
    f"Make this synthetic workspace satisfy every criterion: {ALPHA} contains exactly "
    f"{TARGET_ALPHA!r}; {GAMMA} no longer exists and {ARCHIVED_GAMMA} contains exactly "
    f"{SEED[GAMMA]!r}; {BETA} is untouched and still contains exactly {SEED[BETA]!r}."
)
SUCCESS_CRITERIA = (
    f"{ALPHA} contains exactly {TARGET_ALPHA!r}",
    f"{GAMMA} no longer exists and {ARCHIVED_GAMMA} contains exactly {SEED[GAMMA]!r}",
    f"{BETA} is untouched and still contains exactly {SEED[BETA]!r}",
)


class WorkspaceError(RuntimeError):
    """The workspace is not a new, empty, private directory."""


def prepare_workspace(path: Path) -> Path:
    """Create a new workspace, or accept an existing empty directory, and nothing else.

    Existing content is never touched: a non-empty directory is refused before any file is
    written, so the example cannot overwrite or delete someone else's data.
    """
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise WorkspaceError(f"workspace must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise WorkspaceError(f"workspace is not a directory: {resolved}")
        entries = sorted(entry.name for entry in resolved.iterdir())
        if entries:
            shown = ", ".join(entries[:5])
            raise WorkspaceError(
                f"workspace must be new or empty; refusing to touch existing content in {resolved} "
                f"({len(entries)} entries: {shown})"
            )
    else:
        resolved.mkdir(parents=True, mode=0o700)
    return resolved


def seed_workspace(workspace: Path) -> None:
    """Write the fixed synthetic seed; every file is created by this example."""
    (workspace / "inbox").mkdir(mode=0o700)
    for relative, content in SEED.items():
        _write_text(workspace, relative, content)


def _safe_path(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, refusing escapes and symlink components."""
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise WorkspaceError(f"refusing a path outside the workspace: {relative}")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceError(f"refusing to follow a symlink inside the workspace: {relative}")
    target = current.resolve()
    if not target.is_relative_to(root):
        raise WorkspaceError(f"refusing a path outside the workspace: {relative}")
    return target


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")[:MAX_CONTENT_BYTES]


def _write_text(root: Path, relative: str, content: str) -> None:
    target = _safe_path(root, relative)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, target)


class LiveFilesEnvironment:
    """Observe and operate one private directory; the model never touches the filesystem."""

    name = "live-files"

    def __init__(self, workspace: Path) -> None:
        self.root = Path(workspace).resolve()
        self.calls: list[dict[str, Any]] = []

    def observe(self) -> Observation:
        files: dict[str, Any] = {}
        try:
            for relative in TASK_FILES:
                path = _safe_path(self.root, relative)
                if path.is_file():
                    files[relative] = {
                        "content": path.read_text(encoding="utf-8")[:MAX_CONTENT_BYTES],
                        "bytes": path.stat().st_size,
                    }
            entries = sorted(entry.name for entry in self.root.iterdir())
        except OSError as error:
            raise ObservationError(f"the workspace could not be read ({error.__class__.__name__})") from None
        except WorkspaceError as error:
            raise ObservationError(f"the workspace could not be read safely ({error.__class__.__name__})") from None
        revision = "rev-" + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:10]
        return Observation(
            revision=revision,
            payload={"task_files": files, "workspace_entries": entries},
        )

    def offer(self, state: RuntimeState, observation: Observation) -> Sequence[ActionCandidate]:
        files = observation.payload["task_files"]
        candidates: list[ActionCandidate] = []

        alpha = files.get(ALPHA)
        if alpha is None or alpha["content"] != TARGET_ALPHA:
            current = "missing" if alpha is None else repr(alpha["content"])
            candidates.append(
                ActionCandidate(
                    key="write:alpha-ready",
                    capability="filesystem",
                    description=f"Write {TARGET_ALPHA!r} into {ALPHA} (currently {current}).",
                    args={"path": ALPHA, "content": TARGET_ALPHA},
                    effect=Effect.LOCAL_WRITE,
                    idempotency=Idempotency.SAFE,
                    observation=observation.revision,
                )
            )

        if GAMMA in files and ARCHIVED_GAMMA not in files:
            candidates.append(
                ActionCandidate(
                    key="move:gamma-archive",
                    capability="filesystem",
                    description=f"Move {GAMMA} into {ARCHIVED_GAMMA}.",
                    args={"source": GAMMA, "target": ARCHIVED_GAMMA},
                    effect=Effect.LOCAL_WRITE,
                    idempotency=Idempotency.SAFE,
                    observation=observation.revision,
                )
            )
        return candidates

    def execute(self, candidate: ActionCandidate, intent) -> ExecutionReceipt:
        self.calls.append({"key": candidate.key, "operation_id": intent.operation_id})

        if candidate.key == "write:alpha-ready":
            path = _safe_path(self.root, ALPHA)
            if _read_text(path) == TARGET_ALPHA:
                raise ExecutionRejected(f"{ALPHA} already contains the target content")
            _write_text(self.root, ALPHA, TARGET_ALPHA)
            return ExecutionReceipt(intent.operation_id, ReceiptStatus.COMPLETED, detail=f"wrote {ALPHA}")

        if candidate.key == "move:gamma-archive":
            source = _safe_path(self.root, GAMMA)
            target = _safe_path(self.root, ARCHIVED_GAMMA)
            if not source.is_file():
                raise ExecutionRejected(f"{GAMMA} is not there any more")
            if target.exists():
                raise ExecutionRejected(f"{ARCHIVED_GAMMA} already exists")
            target.parent.mkdir(mode=0o700, exist_ok=True)
            source.rename(target)
            return ExecutionReceipt(
                intent.operation_id, ReceiptStatus.COMPLETED, detail=f"moved {GAMMA} to {ARCHIVED_GAMMA}"
            )

        raise ExecutionRejected(f"unknown candidate {candidate.key!r}")

    def query(self, operation):
        # Every operation here completes synchronously; nothing is ever left pending.
        return None

    def validate(self, candidate: ActionCandidate, state: RuntimeState) -> str | None:
        return default_validate(candidate, state)


@dataclass
class LiveFilesVerifier:
    """Independent evidence: re-read the workspace instead of trusting observation or model."""

    workspace: Path

    def verify(self, state: RuntimeState) -> VerifierResult:
        root = Path(self.workspace).resolve()
        reads: dict[str, str | None] = {}
        try:
            for relative in TASK_FILES:
                reads[relative] = _read_text(_safe_path(root, relative))
        except OSError:
            return VerifierResult(
                VerifyVerdict.UNKNOWN,
                detail="a workspace path exists but cannot be read as a file",
                evidence={"workspace": str(root)},
            )
        except WorkspaceError:
            return VerifierResult(
                VerifyVerdict.UNKNOWN,
                detail="the workspace contains a symlink or an out-of-bounds path; refusing to read it",
                evidence={"workspace": str(root)},
            )

        expected = {
            "alpha_content": TARGET_ALPHA,
            "beta_content": SEED[BETA],
            "archived_gamma_content": SEED[GAMMA],
        }
        checked = {
            "alpha_content": reads[ALPHA],
            "beta_content": reads[BETA],
            "gamma_still_in_inbox": reads[GAMMA] is not None,
            "archived_gamma_content": reads[ARCHIVED_GAMMA],
        }
        evidence = {"workspace": str(root), "checked": checked, "expected": expected}
        satisfied = (
            reads[ALPHA] == TARGET_ALPHA
            and reads[BETA] == SEED[BETA]
            and reads[GAMMA] is None
            and reads[ARCHIVED_GAMMA] == SEED[GAMMA]
        )
        if not satisfied:
            return VerifierResult(VerifyVerdict.UNSATISFIED, detail=f"not satisfied: {checked}", evidence=evidence)
        return VerifierResult(
            VerifyVerdict.SATISFIED,
            detail="the workspace matches every success criterion",
            evidence=evidence,
        )

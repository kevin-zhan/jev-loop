"""Bundle scaffolding from templates that ship inside the installed package.

The templates live next to this module (``templates/<template>/``) and are read through
``importlib.resources``, so they work from a wheel installed anywhere — no path into the
author's checkout, no hidden private project.  ``create_bundle`` refuses to touch an
existing target directory at all: it never writes into a directory the user already owns.
Every file is created with ``O_EXCL``; if rendering fails midway the call removes exactly
the files and directories it created in that call and leaves everything else alone.

A generated bundle is an explicit scaffold: its manifest says ``scaffold: true``, its
controller raises until the author implements it, and the host refuses to start it by
default.  A scaffold proves the plumbing, never that a user's goal was implemented or
verified.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .discovery import discovery_root
from .manifest import BUNDLE_NAME_PATTERN

TEMPLATE_ROOT_PACKAGE = "jev_loop.bundles.templates"
# An explicit whitelist, not a directory listing: a stray directory in the package (a cache, a
# leftover experiment) must never be offered or accepted as a template.
TEMPLATE_NAMES = ("basic",)
REQUIRED_TEMPLATE_FILES = ("bundle.json", "BUNDLE.md", "bundle_controller.py")
DEFAULT_TEMPLATE = "basic"
DEFAULT_VERSION = "0.1.0"
MAX_NAME_CHARS = 64

TOKEN_PATTERN = re.compile(r"__[A-Z][A-Z0-9_]{2,}__")
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip")


class ScaffoldError(ValueError):
    """The requested scaffold cannot be created safely."""


@dataclass
class ScaffoldResult:
    name: str
    template: str
    directory: Path
    files: list[Path] = field(default_factory=list)
    created_directories: list[Path] = field(default_factory=list)
    project_root: Path | None = None

    def next_commands(self) -> list[str]:
        """Follow-up commands that actually work from where the bundle was created.

        The first command is the conformance gate, which runs the author tests with the same
        interpreter that provides the CLI, so it also works in a source checkout.  The second is
        the plain unittest loop for fast local iteration; it needs an environment where
        ``jev_loop`` is importable (an installed package, or a checkout with ``src`` on
        ``PYTHONPATH``), which the authoring guide states.

        A bundle created outside the project's discovery root cannot be addressed by name, and a
        bundle created outside the selected project root cannot be validated against it at all
        (the trust root would refuse it).  In that case only the author-test command is offered,
        plus a note asking for an explicit project choice — never a command that is bound to fail.
        """
        local = f"cd {shlex.quote(str(self.directory))} && python3 -m unittest discover -s tests -v"
        if self.project_root is None:
            return [local]
        root = self.project_root
        manifest = self.directory / "bundle.json"
        quoted_root = shlex.quote(str(root))
        if _inside(self.directory, discovery_root(root)):
            return [
                f"jev-loop bundle conformance project:{self.name} --project-root {quoted_root} --run-tests",
                local,
            ]
        if _inside(self.directory, root):
            return [
                f"jev-loop bundle conformance {shlex.quote(str(manifest))} --project-root {quoted_root} --run-tests",
                local,
            ]
        return [local]

    def notes(self) -> list[str]:
        if self.project_root is None:
            return []
        if _inside(self.directory, discovery_root(self.project_root)):
            return []
        if _inside(self.directory, self.project_root):
            return [
                f"this bundle is inside {self.project_root} but not in its discovery root "
                f"({discovery_root(self.project_root)}), so it is validated by explicit path and will not be "
                "listed by 'jev-loop bundle list'"
            ]
        return [
            f"this bundle was created outside {self.project_root}, so the standard CLI cannot validate or start "
            "it against that project (a manifest must live inside the project root). Create it inside a project "
            "instead (omit --dir so it lands in <project_root>/.agents/jev-bundle/<name>), or pass a "
            "--project-root that contains it."
        ]

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "template": self.template,
            "directory": str(self.directory),
            "files": [str(path) for path in self.files],
            "next_commands": self.next_commands(),
            "notes": self.notes(),
            "scaffold": True,
        }


def template_directory(template: str = DEFAULT_TEMPLATE):
    """Return the traversable directory for one packaged template.

    Only names in :data:`TEMPLATE_NAMES` are accepted, and the directory must actually carry the
    files a bundle needs; anything else is reported as an unknown template.
    """
    if template not in TEMPLATE_NAMES:
        raise ScaffoldError(
            f"unknown template {template!r}; available templates are {list(TEMPLATE_NAMES)}"
        )
    candidate = resources.files(TEMPLATE_ROOT_PACKAGE) / template
    if not candidate.is_dir():
        raise ScaffoldError(
            f"template {template!r} is missing from this installation; available templates are "
            f"{list(TEMPLATE_NAMES)}"
        )
    missing = [name for name in REQUIRED_TEMPLATE_FILES if not (candidate / name).is_file()]
    if missing:
        raise ScaffoldError(
            f"template {template!r} is incomplete in this installation (missing {missing})"
        )
    return candidate


def template_tokens(template: str = DEFAULT_TEMPLATE) -> frozenset[str]:
    """Machine tokens the packaged template still contains (used to detect unrendered files)."""
    tokens: set[str] = set()
    for item in _walk(template_directory(template)):
        if not _is_text(item.name):
            continue
        try:
            tokens.update(TOKEN_PATTERN.findall(item.read_text(encoding="utf-8")))
        except OSError:
            continue
    return frozenset(tokens)


def create_bundle(
    target_directory: Path,
    name: str,
    *,
    template: str = DEFAULT_TEMPLATE,
    version: str = DEFAULT_VERSION,
    description: str | None = None,
    project_root: Path | None = None,
) -> ScaffoldResult:
    """Create a new bundle directory.  Refuses to touch an existing target path."""
    if not isinstance(name, str) or not name.strip():
        raise ScaffoldError("bundle name is required")
    bundle_name = name.strip()
    if len(bundle_name) > MAX_NAME_CHARS or not BUNDLE_NAME_PATTERN.match(bundle_name) or "--" in bundle_name:
        raise ScaffoldError(
            f"{bundle_name!r} is not a valid bundle name: use lowercase letters, digits and single hyphens"
        )
    source = template_directory(template)
    target = Path(target_directory)
    if target.exists():
        raise ScaffoldError(
            f"refusing to create a bundle in {target}: the path already exists. "
            "Choose a new directory; this command never overwrites or merges user files."
        )
    target = target.resolve()

    directories_to_create = _missing_parents(target)
    created_directories: list[Path] = []
    created_files: list[Path] = []
    try:
        for directory in directories_to_create:
            _ensure_directory(directory, created_directories)
        for item in _walk(source):
            relative = _relative_name(item, source)
            destination = target / relative
            if relative.name == "__init__.py":  # package markers of the template store, not bundle content
                continue
            _ensure_directory(destination.parent, created_directories)
            text = item.read_text(encoding="utf-8")
            rendered = _render(text, name=bundle_name, version=version, description=description)
            _exclusive_write(destination, rendered)
            created_files.append(destination)
    except BaseException as error:
        _cleanup(created_files, created_directories)
        raise ScaffoldError(f"scaffold creation failed, nothing was left behind: {error}") from error
    return ScaffoldResult(
        name=bundle_name,
        template=template,
        directory=target,
        files=created_files,
        created_directories=created_directories,
        project_root=Path(project_root).resolve() if project_root is not None else None,
    )


def _render(text: str, *, name: str, version: str, description: str | None) -> str:
    rendered = text.replace("__BUNDLE_NAME__", name).replace("__BUNDLE_VERSION__", version)
    if "__BUNDLE_DESCRIPTION__" in rendered:
        rendered = rendered.replace("__BUNDLE_DESCRIPTION__", description or f"{name} bundle")
    leftovers = sorted(set(TOKEN_PATTERN.findall(rendered)))
    if leftovers:
        raise ScaffoldError(f"template left unrendered tokens {leftovers}")
    return rendered


def _exclusive_write(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        raise ScaffoldError(f"refusing to overwrite existing file {path}")
    with path.open("x", encoding="utf-8") as handle:  # O_EXCL
        handle.write(text)
    os.chmod(path, 0o644)


def _ensure_directory(path: Path, created: list[Path]) -> None:
    if path.exists():
        if not path.is_dir():
            raise ScaffoldError(f"refusing to use {path} as a directory: it exists and is not a directory")
        return
    _ensure_directory(path.parent, created)
    path.mkdir(mode=0o755)
    created.append(path)


def _missing_parents(target: Path) -> list[Path]:
    missing: list[Path] = []
    current = target
    while not current.exists():
        missing.append(current)
        current = current.parent
        if current == current.parent:
            break
    return list(reversed(missing))


def _cleanup(files: list[Path], directories: list[Path]) -> None:
    """Remove exactly what this call created; a directory that gained other content stays."""
    for path in reversed(files):
        try:
            path.unlink()
        except OSError:
            pass
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            pass


def _walk(directory):
    for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if item.name == "__pycache__" or item.name.startswith("."):
            continue
        if item.is_dir():
            yield from _walk(item)
        else:
            yield item


def _relative_name(item, source) -> Path:
    parts: list[str] = []
    current = item
    while str(current) != str(source):
        parts.append(current.name)
        current = current.parent
    return Path(*reversed(parts))


def _is_text(filename: str) -> bool:
    return not filename.lower().endswith(BINARY_SUFFIXES)


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        root_resolved = Path(root).resolve()
    except OSError:
        return False
    return resolved == root_resolved or resolved.is_relative_to(root_resolved)

"""Boundedness regressions for the bundle file scan.

These tests instrument the *actual* enumeration, open and read points rather than asserting that a
big fixture eventually reports a budget: a directory materialised in full, a path re-resolved after
enumeration, a file read without a cap, or bytes read without being charged must fail here even
though the final report could look the same.  Everything uses synthetic sentinels in temporary
directories; no real credential, private file or environment value is read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jev_loop.bundles import validate as validate_module
from jev_loop.bundles.conformance import conformance_report, run_probe
from jev_loop.bundles.scaffold import create_bundle
from jev_loop.bundles.validate import fingerprint, scan_bundle_files

SENTINEL = b"SCAN-SENTINEL-DO-NOT-READ-0123456789"


def make_project(tmp_path: Path, name: str = "scan-bundle") -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    create_bundle(project / ".agents" / "jev-bundle" / name, name)
    return project


def bundle_dir(project: Path, name: str = "scan-bundle") -> Path:
    return project / ".agents" / "jev-bundle" / name


def open_descriptor_count() -> int:
    """Count this process's open descriptors (POSIX /dev/fd)."""
    return len(os.listdir("/dev/fd"))


# ---------------------------------------------------------------------------------------------
# enumeration is descriptor-anchored: there is no queued path to substitute
# ---------------------------------------------------------------------------------------------


def test_the_walk_enumerates_only_through_pinned_descriptors(tmp_path: Path) -> None:
    """Every scandir call must take a descriptor, so no path can be re-resolved later."""
    directory = tmp_path / "bundle"
    (directory / "nested" / "deeper").mkdir(parents=True)
    (directory / "nested" / "deeper" / "f.txt").write_text("x\n", encoding="utf-8")

    arguments: list[object] = []
    real_scandir = validate_module._scandir

    def recording_scandir(argument):
        arguments.append(argument)
        return real_scandir(argument)

    original = validate_module._scandir
    validate_module._scandir = recording_scandir
    try:
        scan = scan_bundle_files(directory)
    finally:
        validate_module._scandir = original

    assert scan.complete is True, scan.read_failures
    assert arguments and all(isinstance(argument, int) for argument in arguments), arguments


def test_a_queued_directory_swapped_for_an_outside_symlink_is_never_followed(tmp_path: Path) -> None:
    """The parent-directory substitution case: swap the directory, then let the walk continue."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(SENTINEL)
    (bundle / "sub").mkdir()
    (bundle / "sub" / "inner.txt").write_text("inner\n", encoding="utf-8")

    real_scandir = validate_module._scandir
    swapped = {"done": False}

    def swapping_scandir(descriptor):
        for entry in real_scandir(descriptor):
            if entry.name == "sub" and not swapped["done"]:
                swapped["done"] = True
                target = bundle / "sub"
                for child in target.iterdir():
                    child.unlink()
                target.rmdir()
                os.symlink(outside, target)  # the enumerated directory becomes an outside link
            yield entry

    original = validate_module._scandir
    validate_module._scandir = swapping_scandir
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._scandir = original

    assert swapped["done"] is True, "the fixture did not perform the swap"
    payloads = b"".join(item.payload for item in scan.files)
    assert SENTINEL not in payloads, "a substituted directory must never be followed"
    assert scan.symlinks_skipped >= 1
    assert scan.incomplete is True
    assert fingerprint(scan) is None


def test_a_file_swapped_for_a_symlink_is_not_followed(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "victim.txt").write_text("harmless\n", encoding="utf-8")
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(SENTINEL)

    real_scandir = validate_module._scandir

    def swapping_scandir(descriptor):
        for entry in real_scandir(descriptor):
            if entry.name == "victim.txt":
                (bundle / "victim.txt").unlink()
                os.symlink(secret, bundle / "victim.txt")
            yield entry

    original = validate_module._scandir
    validate_module._scandir = swapping_scandir
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._scandir = original

    payloads = b"".join(item.payload for item in scan.files)
    assert SENTINEL not in payloads
    assert scan.incomplete is True


def test_a_file_replaced_by_a_same_size_other_inode_is_reported(tmp_path: Path) -> None:
    """A same-size replacement by a different inode must be detected, not read as if unchanged.

    The swap is injected between the entry's stat and the open, which is the race the identity
    check exists for.  (A change that happens *before* the scan observes the file is simply the
    file's current content; no snapshot can see that.)
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "victim.txt").write_text("AAAA\n", encoding="utf-8")
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("BBBB\n", encoding="utf-8")  # same length, different inode and content

    real_open = validate_module._open_file_at

    def swapping_open(directory_descriptor, name):
        if name == "victim.txt":
            os.replace(replacement, bundle / "victim.txt")
        return real_open(directory_descriptor, name)

    original = validate_module._open_file_at
    validate_module._open_file_at = swapping_open
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._open_file_at = original

    payloads = b"".join(item.payload for item in scan.files)
    assert b"BBBB" not in payloads, "the replacement must not be accepted as the enumerated file"
    assert scan.incomplete is True
    assert any("victim.txt" in failure for failure in scan.read_failures)
    assert fingerprint(scan) is None


def test_a_same_length_write_during_the_read_is_detected(tmp_path: Path) -> None:
    """fstat after the read is what catches a same-length modification (best effort, not atomic)."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    target = bundle / "mutated.txt"
    target.write_text("original\n", encoding="utf-8")

    real_read = validate_module._read_bounded

    def mutating_read(descriptor, limit, *, chunk=validate_module.READ_CHUNK_BYTES, charge=None):
        payload = real_read(descriptor, limit, chunk=chunk, charge=charge)
        target.write_text("mutated!\n", encoding="utf-8")  # same length, different content
        return payload

    original = validate_module._read_bounded
    validate_module._read_bounded = mutating_read
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._read_bounded = original

    assert scan.incomplete is True
    assert any("changed while it was being read" in failure for failure in scan.read_failures)
    assert fingerprint(scan) is None


def test_a_file_replaced_by_a_fifo_neither_blocks_nor_is_read(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    os.mkfifo(bundle / "pipe.txt")

    scan = scan_bundle_files(bundle)  # must return, not hang
    assert all(item.relative != "pipe.txt" for item in scan.files)
    assert scan.incomplete is True


# ---------------------------------------------------------------------------------------------
# the total read budget charges every attempted read
# ---------------------------------------------------------------------------------------------


def test_repeated_failed_reads_cannot_exceed_the_declared_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every attempted read is charged, even when the payload is discarded as changed.

    The old accounting charged only *retained* files, so repeated discarded reads could read far
    past the declared budget while reporting ``bytes_read == 0``.  Here each read really consumes
    bytes, then fails the post-read check (the file is mutated), and the walk must still stop at
    the budget.  The assertion is on the bytes actually returned by ``os.read``.
    """
    budget = 200_000
    monkeypatch.setattr(validate_module, "MAX_SCAN_TOTAL_BYTES", budget)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for index in range(64):
        (bundle / f"g-{index:02d}.txt").write_text("x" * 4096, encoding="utf-8")

    real_read = validate_module._read_bounded
    real_os_read = os.read
    counters = {"bytes": 0, "attempts": 0}

    def counting_os_read(descriptor, size):
        piece = real_os_read(descriptor, size)
        counters["bytes"] += len(piece)
        return piece

    def mutating_read(descriptor, limit, *, chunk=validate_module.READ_CHUNK_BYTES, charge=None):
        counters["attempts"] += 1
        payload = real_read(descriptor, limit, chunk=chunk, charge=charge)
        # The bytes were really read; the file then changes, so the payload is discarded.
        for path in bundle.iterdir():
            path.write_text("y" * 4096, encoding="utf-8")
        return payload

    original_read = validate_module._read_bounded
    validate_module._read_bounded = mutating_read
    os.read = counting_os_read
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._read_bounded = original_read
        os.read = real_os_read

    assert counters["bytes"] <= budget, counters
    assert scan.bytes_read <= budget
    assert scan.bytes_read > 0, "the discarded reads must still be charged"
    assert scan.incomplete is True
    assert scan.files == ()
    assert fingerprint(scan) is None
    assert counters["attempts"] < 64, counters  # the walk stopped at the budget


def test_the_budget_caps_each_attempt_not_only_the_retained_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-attempt limit is min(per-file cap, remaining budget), and it is really enforced."""
    monkeypatch.setattr(validate_module, "MAX_SCAN_TOTAL_BYTES", 1500)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "a.txt").write_text("a" * 1000, encoding="utf-8")
    (bundle / "b.txt").write_text("b" * 1000, encoding="utf-8")

    limits: list[int] = []
    real_read = validate_module._read_bounded

    def recording_read(descriptor, limit, *, chunk=validate_module.READ_CHUNK_BYTES, charge=None):
        limits.append(limit)
        return real_read(descriptor, limit, chunk=chunk, charge=charge)

    original = validate_module._read_bounded
    validate_module._read_bounded = recording_read
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._read_bounded = original

    # 1500 bytes of budget, 1000-byte files: the second attempt is capped by the remainder.
    assert limits == [1500, 500]
    assert scan.bytes_read == 1500
    assert len(scan.files) == 1, "only the file that fitted in the budget is retained"
    assert scan.files[0].relative in {"a.txt", "b.txt"}
    assert scan.incomplete is True, "the second file could not be read within the budget"
    assert fingerprint(scan) is None


def test_a_large_directory_is_not_materialised_before_the_budget(tmp_path: Path) -> None:
    """`sorted(iterdir())` would consume the whole directory; the walk must stop at the budget."""
    directory = tmp_path / "huge"
    directory.mkdir()
    total = validate_module.MAX_SCAN_ENTRIES * 3
    for index in range(total):
        (directory / f"f-{index:05d}.txt").write_text("x\n", encoding="utf-8")

    consumed = 0
    real_scandir = validate_module._scandir

    def counting_scandir(descriptor):
        nonlocal consumed
        for entry in real_scandir(descriptor):
            consumed += 1
            yield entry

    original = validate_module._scandir
    validate_module._scandir = counting_scandir
    try:
        scan = scan_bundle_files(directory)
    finally:
        validate_module._scandir = original

    assert scan.incomplete is True
    assert consumed <= validate_module.MAX_SCAN_ENTRIES + 1, consumed
    assert consumed < total


def test_the_depth_limit_is_reported_and_bounded(tmp_path: Path) -> None:
    directory = tmp_path / "deep"
    directory.mkdir()
    current = directory
    for index in range(validate_module.MAX_SCAN_DEPTH + 5):
        current = current / f"d{index}"
        current.mkdir()
    (current / "deep.txt").write_text("x\n", encoding="utf-8")

    scan = scan_bundle_files(directory)
    assert scan.incomplete is True
    assert "depth limit" in scan.reason
    assert all(item.relative != "deep.txt" for item in scan.files)


# ---------------------------------------------------------------------------------------------
# errors, cleanup and coverage honesty
# ---------------------------------------------------------------------------------------------


def test_an_error_during_iterator_consumption_is_reported_not_raised(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "first.txt").write_text("one\n", encoding="utf-8")

    real_scandir = validate_module._scandir

    def failing_scandir(descriptor):
        iterator = real_scandir(descriptor)
        try:
            yield from iterator
            raise OSError(5, "simulated mid-iteration failure")
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    original = validate_module._scandir
    validate_module._scandir = failing_scandir
    try:
        scan = scan_bundle_files(bundle)  # must not raise
    finally:
        validate_module._scandir = original

    assert scan.incomplete is True
    assert "read to completion" in scan.reason
    assert fingerprint(scan) is None


def test_an_unreadable_file_makes_the_scan_incomplete_and_the_fingerprint_none(tmp_path: Path) -> None:
    directory = tmp_path / "unreadable"
    directory.mkdir()
    (directory / "blocked.txt").write_text("content\n", encoding="utf-8")

    real_open = validate_module._open_file_at

    def failing_open(directory_descriptor, name):
        if name == "blocked.txt":
            raise PermissionError(13, "denied")
        return real_open(directory_descriptor, name)

    original = validate_module._open_file_at
    validate_module._open_file_at = failing_open
    try:
        scan = scan_bundle_files(directory)
    finally:
        validate_module._open_file_at = original

    assert scan.incomplete is True
    assert scan.read_failures and "blocked.txt" in scan.read_failures[0]
    assert fingerprint(scan) is None, "a failed read must never yield a fingerprint"
    assert scan.complete is False


def test_descriptors_and_iterators_are_closed(tmp_path: Path) -> None:
    """A deep and wide scan must not leak descriptors, on success or on an injected failure."""
    directory = tmp_path / "many"
    for index in range(6):
        branch = directory / f"b{index}" / "nested"
        branch.mkdir(parents=True)
        for leaf in range(5):
            (branch / f"l{leaf}.txt").write_text("x\n", encoding="utf-8")

    before = open_descriptor_count()
    assert scan_bundle_files(directory).complete is True
    after_success = open_descriptor_count()
    assert after_success <= before + 1, (before, after_success)

    real_open = validate_module._open_directory_at
    calls = {"count": 0}

    def failing_open(directory_descriptor, name):
        calls["count"] += 1
        if calls["count"] == 3:
            raise OSError(5, "simulated directory open failure")
        return real_open(directory_descriptor, name)

    original = validate_module._open_directory_at
    validate_module._open_directory_at = failing_open
    try:
        scan = scan_bundle_files(directory)
    finally:
        validate_module._open_directory_at = original
    assert scan.incomplete is True
    after_failure = open_descriptor_count()
    assert after_failure <= before + 1, (before, after_failure)


def test_the_probe_reports_incompleteness_instead_of_a_false_ok(tmp_path: Path) -> None:
    """A bundle file that cannot be scanned must not produce a green 'nothing changed' probe."""
    project = make_project(tmp_path)
    os.mkfifo(bundle_dir(project) / "blocked.txt")

    probe = run_probe("project:scan-bundle", project)

    assert probe["ok"] is False, probe
    assert probe["observed"]["scan_incomplete"] is True
    assert probe["observed"]["fingerprint"] is None
    assert probe["observed"]["files_changed"], "an unscannable file must not look unchanged"


def test_a_complete_scan_reports_its_exact_coverage(tmp_path: Path) -> None:
    directory = tmp_path / "clean"
    directory.mkdir()
    (directory / "one.txt").write_text("one\n", encoding="utf-8")
    scan = scan_bundle_files(directory)
    assert scan.complete is True and scan.incomplete is False
    assert scan.read_failures == ()
    assert fingerprint(scan) is not None
    assert "nothing outside the bundle read" in scan.coverage


def test_a_fingerprint_depends_on_root_relative_paths(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory, name in ((first, "a.txt"), (second, "b.txt")):
        directory.mkdir()
        (directory / name).write_text("same content\n", encoding="utf-8")
    assert fingerprint(scan_bundle_files(first)) != fingerprint(scan_bundle_files(second))


def test_hidden_files_and_external_links_are_never_read(tmp_path: Path) -> None:
    directory = tmp_path / "sealed"
    directory.mkdir()
    (directory / "visible.txt").write_text("visible\n", encoding="utf-8")
    hidden = directory / ".hidden"
    hidden.mkdir()
    (hidden / "credentials.txt").write_bytes(SENTINEL)
    external = tmp_path / "outside.txt"
    external.write_bytes(SENTINEL)
    os.symlink(external, directory / "link.txt")
    os.symlink(tmp_path, directory / "link-dir")

    scan = scan_bundle_files(directory)
    payloads = b"".join(item.payload for item in scan.files)
    assert SENTINEL not in payloads
    assert sorted(item.relative for item in scan.files) == ["visible.txt"]
    assert scan.symlinks_skipped >= 2


def test_a_bundle_whose_file_cannot_be_scanned_is_reported_by_conformance(tmp_path: Path) -> None:
    """The report itself must carry the incompleteness, not just the probe payload."""
    project = make_project(tmp_path, "unreadable-conformance")
    os.mkfifo(bundle_dir(project, "unreadable-conformance") / "blocked.txt")

    report = conformance_report("project:unreadable-conformance", project)

    assert report["ok"] is False
    assert report["bundle_digest"] is None
    assert "partial" in report["bundle_digest_scope"]
    assert report["side_effects"]["ok"] is False


def test_the_platform_support_check_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without the no-follow primitives the snapshot reports itself incomplete, never weaker."""
    directory = tmp_path / "plain"
    directory.mkdir()
    (directory / "f.txt").write_text("x\n", encoding="utf-8")

    monkeypatch.delattr(validate_module.os, "O_NOFOLLOW", raising=False)
    try:
        scan = scan_bundle_files(directory)
    finally:
        monkeypatch.undo()
    assert scan.incomplete is True
    assert "no-follow primitives" in scan.reason
    assert scan.files == ()
    assert fingerprint(scan) is None


# ---------------------------------------------------------------------------------------------
# R1(a): every successful os.read is charged immediately, including before a later EIO
# ---------------------------------------------------------------------------------------------


def test_a_partial_read_that_then_fails_still_charges_the_bytes_it_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunks read before an EIO must be charged, so repeated failures cannot exceed the budget."""
    budget = 100_000
    monkeypatch.setattr(validate_module, "MAX_SCAN_TOTAL_BYTES", budget)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for index in range(40):
        (bundle / f"p-{index:02d}.txt").write_text("x" * 8192, encoding="utf-8")

    real_os_read = os.read
    counters = {"bytes": 0, "attempts": 0}

    def counting_os_read(descriptor, size):
        piece = real_os_read(descriptor, size)
        counters["bytes"] += len(piece)
        return piece

    def partial_then_eio(descriptor, limit, *, chunk=validate_module.READ_CHUNK_BYTES, charge=None):
        counters["attempts"] += 1
        first = counting_os_read(descriptor, min(4096, limit))  # the counted read
        if charge is not None:
            charge(len(first))
        raise OSError(5, "simulated EIO after a partial read")

    monkeypatch.setattr(validate_module, "_read_bounded", partial_then_eio)
    try:
        scan = scan_bundle_files(bundle)
    finally:
        pass

    assert counters["bytes"] <= budget, counters
    assert scan.bytes_read == counters["bytes"], (scan.bytes_read, counters)
    assert scan.bytes_read > 0, "the partial reads must be charged, not hidden"
    assert scan.files == ()
    assert scan.incomplete is True
    assert fingerprint(scan) is None
    # 4096 bytes charged per failed attempt: the walk must stop well before 40 files.
    assert counters["attempts"] <= budget // 4096 + 1, counters


def test_the_charge_callback_is_invoked_per_chunk_not_per_file(tmp_path: Path) -> None:
    """A single large-ish file must be charged in chunks as they arrive."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "big.txt").write_bytes(b"x" * (validate_module.READ_CHUNK_BYTES * 3))

    charges: list[int] = []
    real_read = validate_module._read_bounded

    def recording_read(descriptor, limit, *, chunk=validate_module.READ_CHUNK_BYTES, charge=None):
        def record(count: int) -> None:
            charges.append(count)
            if charge is not None:
                charge(count)

        return real_read(descriptor, limit, chunk=chunk, charge=record)

    original = validate_module._read_bounded
    validate_module._read_bounded = recording_read
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._read_bounded = original

    assert len(charges) >= 3, charges
    assert sum(charges) == scan.bytes_read
    assert scan.complete is True


# ---------------------------------------------------------------------------------------------
# R1(b): the canonical root is pinned component by component, never by resolve() + one open
# ---------------------------------------------------------------------------------------------


def test_an_ancestor_replaced_by_an_outside_symlink_is_never_opened(tmp_path: Path) -> None:
    """The root is pinned with dir_fd + O_NOFOLLOW per component, so an ancestor swap fails closed."""
    anchor = tmp_path / "anchor"
    bundle = anchor / "project" / ".agents" / "jev-bundle" / "pinned"
    bundle.mkdir(parents=True)
    (bundle / "bundle.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(SENTINEL)

    real_open = validate_module._open_component_at
    swapped = {"done": False}

    def swapping_open(directory_descriptor, name):
        # Replace the ancestor the walk is about to enter with a link to the outside directory.
        if name == "anchor" and not swapped["done"]:
            swapped["done"] = True
            for child in sorted(anchor.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            anchor.rmdir()
            os.symlink(outside, anchor)
        return real_open(directory_descriptor, name)

    original = validate_module._open_component_at
    validate_module._open_component_at = swapping_open
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._open_component_at = original

    assert swapped["done"] is True, "the fixture did not perform the swap"
    payloads = b"".join(item.payload for item in scan.files)
    assert SENTINEL not in payloads, "an ancestor substitution must never be followed"
    assert scan.files == ()
    assert scan.incomplete is True
    assert fingerprint(scan) is None


def test_the_bundle_root_replaced_by_an_outside_symlink_is_never_opened(tmp_path: Path) -> None:
    """The same guarantee for the root component itself."""
    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "root-swap"
    bundle.mkdir(parents=True)
    (bundle / "bundle.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(SENTINEL)

    real_open = validate_module._open_component_at
    swapped = {"done": False}

    def swapping_open(directory_descriptor, name):
        if name == "root-swap" and not swapped["done"]:
            swapped["done"] = True
            (bundle / "bundle.json").unlink()
            bundle.rmdir()
            os.symlink(outside, bundle)
        return real_open(directory_descriptor, name)

    original = validate_module._open_component_at
    validate_module._open_component_at = swapping_open
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._open_component_at = original

    assert swapped["done"] is True
    payloads = b"".join(item.payload for item in scan.files)
    assert SENTINEL not in payloads
    assert scan.files == ()
    assert scan.incomplete is True


def test_the_root_is_pinned_with_a_component_walk_not_one_open(tmp_path: Path) -> None:
    """Every component of the canonical root is opened relative to its pinned parent."""
    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "walked"
    bundle.mkdir(parents=True)
    (bundle / "f.txt").write_text("x\n", encoding="utf-8")

    opened: list[str] = []
    real_open = validate_module._open_component_at

    def recording_open(directory_descriptor, name):
        opened.append(name)
        return real_open(directory_descriptor, name)

    original = validate_module._open_component_at
    validate_module._open_component_at = recording_open
    try:
        scan = scan_bundle_files(bundle)
    finally:
        validate_module._open_component_at = original

    assert scan.complete is True
    expected = [part for part in bundle.resolve().parts if part not in {os.sep, ""}]
    assert opened[: len(expected)] == expected, (opened, expected)


def test_a_cached_canonical_root_replaced_before_the_scan_is_never_opened(tmp_path: Path) -> None:
    """The scan must not re-resolve the path it is handed.

    A caller caches the canonical bundle path (as the resolver does), the root or an ancestor is
    replaced by an outside symlink, and only then is the scan called.  Re-resolving inside the scan
    would bless the new target and read the outside file; pinning the given path component by
    component fails closed instead.
    """
    import shutil

    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "cached"
    bundle.mkdir(parents=True)
    (bundle / "inner.txt").write_text("inner\n", encoding="utf-8")
    cached = bundle.resolve()  # the canonical path a caller would hold
    assert str(cached).startswith(str(tmp_path.resolve()))

    # The outside tree mirrors the same relative layout, so a re-resolved path would find it.
    outside = tmp_path / "outside"
    (outside / ".agents" / "jev-bundle" / "cached").mkdir(parents=True)
    (outside / ".agents" / "jev-bundle" / "cached" / "secret.txt").write_bytes(SENTINEL)

    shutil.rmtree(project)
    os.symlink(outside, project)  # the ancestor is now an outside link, before the call

    scan = scan_bundle_files(cached)

    payloads = b"".join(item.payload for item in scan.files)
    assert SENTINEL not in payloads, "a pre-call substitution must never be blessed by re-resolving"
    assert b"inner" not in payloads
    assert scan.files == ()
    assert scan.incomplete is True
    assert fingerprint(scan) is None


def test_a_cached_canonical_root_replaced_by_an_outside_symlink_itself(tmp_path: Path) -> None:
    """The same for the bundle root component itself, swapped before the call."""
    import shutil

    project = tmp_path / "project"
    bundle = project / ".agents" / "jev-bundle" / "root-swap"
    bundle.mkdir(parents=True)
    (bundle / "inner.txt").write_text("inner\n", encoding="utf-8")
    cached = bundle.resolve()

    outside = tmp_path / "outside"
    (outside / ".agents" / "jev-bundle" / "root-swap").mkdir(parents=True)
    (outside / ".agents" / "jev-bundle" / "root-swap" / "secret.txt").write_bytes(SENTINEL)

    shutil.rmtree(bundle)
    os.symlink(outside / ".agents" / "jev-bundle" / "root-swap", bundle)

    scan = scan_bundle_files(cached)

    payloads = b"".join(item.payload for item in scan.files)
    assert SENTINEL not in payloads
    assert scan.files == ()
    assert scan.incomplete is True


def test_a_normal_canonical_scratch_path_still_scans(tmp_path: Path) -> None:
    """Canonical paths (including macOS /private/var scratch paths) keep working unchanged."""
    directory = tmp_path / "plain"
    directory.mkdir()
    (directory / "f.txt").write_text("hello\n", encoding="utf-8")
    assert str(directory.resolve()) == str(directory), "the fixture path is expected to be canonical"

    scan = scan_bundle_files(directory)
    assert scan.complete is True, scan.read_failures
    assert [item.relative for item in scan.files] == ["f.txt"]
    assert fingerprint(scan) is not None

"""Startup sweep removes leftover .partial archives and staging dirs
that a SIGKILL during ``archive.build`` would otherwise leave on the
spool PVC indefinitely.
"""
from __future__ import annotations

from pathlib import Path

from artifactory_airlift import archive


def test_sweep_removes_partials_and_staging(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "1700000000-aaaaaaaa.tar.zst.partial").write_bytes(b"half-written")
    (spool / "1700000001-bbbbbbbb.tar.zst.partial").write_bytes(b"also half-written")
    staging = spool / ".staging" / "1700000000-aaaaaaaa"
    staging.mkdir(parents=True)
    (staging / "leftover").write_bytes(b"x")

    partials, staging_dirs = archive.sweep_orphan_partials(spool)

    assert partials == 2
    assert staging_dirs == 1
    assert list(spool.glob("*.partial")) == []
    assert list((spool / ".staging").iterdir()) == []


def test_sweep_preserves_finalised_archives(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    finalised = spool / "1700000000-aaaaaaaa.tar.zst"
    finalised.write_bytes(b"complete")
    (spool / "1700000001-bbbbbbbb.tar.zst.partial").write_bytes(b"half")

    archive.sweep_orphan_partials(spool)

    assert finalised.exists()
    assert not (spool / "1700000001-bbbbbbbb.tar.zst.partial").exists()


def test_sweep_on_clean_tree_is_noop(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "1700000000-aaaaaaaa.tar.zst").write_bytes(b"complete")

    partials, staging_dirs = archive.sweep_orphan_partials(spool)

    assert partials == 0
    assert staging_dirs == 0


def test_sweep_missing_spool_dir_is_noop(tmp_path: Path) -> None:
    # Sender/receiver create spool_dir before calling sweep, but the
    # helper should still be safe to call against a path that does not
    # exist yet (e.g. during unit tests that bypass run()).
    partials, staging_dirs = archive.sweep_orphan_partials(tmp_path / "absent")
    assert partials == 0
    assert staging_dirs == 0

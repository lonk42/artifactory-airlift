"""Blobs missing from the store are deferred, not lost.

An object-storage chain with an `eventual` provider uploads asynchronously, so
an artifact can appear in the export metadata before its bytes reach the
bucket. Previously a missing blob was logged and the cursor advanced anyway,
which meant the artifact was never shipped and never retried.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from artifactory_airlift import archive, sender
from artifactory_airlift.config import Settings
from artifactory_airlift.export_unpacker import ArtifactEntry

from ._store import fs_store

PRESENT = "a" * 40
ABSENT = "b" * 40


def _settings(tmp_path: Path) -> Settings:
    state_dir = tmp_path / "state"
    spool_dir = tmp_path / "spool"
    filestore_root = tmp_path / "fs"
    for p in (state_dir, spool_dir, filestore_root):
        p.mkdir(parents=True, exist_ok=True)
    return Settings(
        mode="sender",
        state_dir=state_dir,
        spool_dir=spool_dir,
        filestore_root=filestore_root,
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
    )


def _make_export(tmp_path: Path) -> Path:
    root = tmp_path / "export"
    (root / "repositories").mkdir(parents=True)
    return root


def _entries() -> list[ArtifactEntry]:
    return [
        ArtifactEntry(repo_key="r1", repo_path="here.bin", sha1=PRESENT, size=11),
        ArtifactEntry(repo_key="r1", repo_path="pending.bin", sha1=ABSENT, size=22),
    ]


def _write_snapshot(path: Path, entries: list[ArtifactEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(e.to_json() + "\n" for e in sorted(entries, key=lambda e: e.sha1)))


def test_missing_blob_is_reported_and_left_out_of_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    # Only one of the two blobs is actually in the store.
    blob = settings.filestore_root / PRESENT[:2] / PRESENT
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"hello world")

    archive_path, deferred = archive.build(
        spool_dir=settings.spool_dir,
        cycle_id="c1",
        prev_cycle_id=None,
        source_instance="art-a",
        export_root=_make_export(tmp_path),
        entries=_entries(),
        store=fs_store(settings.filestore_root),
    )

    assert deferred == {ABSENT}
    manifest = archive.read_manifest(archive_path)
    # The shipped entry survives; the one with no bytes behind it does not,
    # so the receiver is never asked to import an artifact it cannot build.
    assert [e["sha1"] for e in manifest.entries] == [PRESENT]
    assert manifest.blob_count == 1


def test_deferred_entries_are_stripped_from_the_baseline(tmp_path: Path) -> None:
    """The rewritten snapshot is what makes the next cycle retry."""
    snapshot = tmp_path / "snap.jsonl"
    _write_snapshot(snapshot, _entries())

    removed = sender._defer_entries(snapshot, {ABSENT})

    assert removed == 1
    remaining = [json.loads(line)["sha1"] for line in snapshot.read_text().splitlines()]
    assert remaining == [PRESENT]


def test_defer_entries_is_a_noop_without_deferrals(tmp_path: Path) -> None:
    snapshot = tmp_path / "snap.jsonl"
    _write_snapshot(snapshot, _entries())
    before = snapshot.read_text()

    assert sender._defer_entries(snapshot, set()) == 0
    assert snapshot.read_text() == before


def test_deferred_entry_reappears_as_added_next_cycle(tmp_path: Path) -> None:
    """End to end: strip from the baseline, then diff a fresh snapshot against it."""
    from artifactory_airlift import diff

    baseline = tmp_path / "prev.jsonl"
    _write_snapshot(baseline, _entries())
    sender._defer_entries(baseline, {ABSENT})

    # Next cycle sees the source unchanged: both artifacts are still there.
    current = tmp_path / "cur.jsonl"
    _write_snapshot(current, _entries())

    added = sorted(e.sha1 for e in diff.added(baseline, current))
    assert added == [ABSENT], "the deferred blob must be retried next cycle"

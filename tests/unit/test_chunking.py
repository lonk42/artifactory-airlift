"""Sender chunking + spool backpressure tests.

Covers the size-based split of a single cycle's diff into N archives
linked by parent_cycle_id, plus the disk-usage guard that aborts a
cycle when free spool space is below the configured high-water mark.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ._store import fs_store
from artifactory_airlift import archive, sender
from artifactory_airlift.config import Settings
from artifactory_airlift.export_unpacker import ArtifactEntry


# Headroom budgeted on top of raw blob bytes when projecting a chunk's
# on-disk footprint; mirrored from sender for clarity in test assertions.
_OVERHEAD = sender._CHUNK_OVERHEAD_BYTES


def _entry(repo: str, name: str, sha1: str, size: int) -> ArtifactEntry:
    return ArtifactEntry(repo_key=repo, repo_path=name, sha1=sha1, size=size)


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        mode="sender",
        instance_name="art-a",
        state_dir=tmp_path / "state",
        spool_dir=tmp_path / "spool",
        filestore_root=tmp_path / "filestore",
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
        snapshot_retention_days=3,
    )
    base.update(overrides)
    for p in (base["state_dir"], base["spool_dir"], base["filestore_root"]):
        p.mkdir(parents=True, exist_ok=True)
    return Settings(**base)


def _make_export(tmp_path: Path) -> Path:
    """Minimal export tree with one repositories/ subdir; build() needs it."""
    root = tmp_path / "export"
    (root / "repositories" / "r1").mkdir(parents=True)
    return root


def _make_blob(filestore_root: Path, sha1: str, size: int) -> None:
    blob = filestore_root / sha1[:2] / sha1
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"x" * size)


# -------- _group_into_chunks --------

def test_group_into_chunks_unlimited_returns_single_group() -> None:
    es = [_entry("r1", f"a{i}", f"{i:040x}", 100) for i in range(5)]
    groups = sender._group_into_chunks(es, max_archive_bytes=0)
    assert len(groups) == 1
    assert groups[0] == es


def test_group_into_chunks_splits_by_cumulative_blob_bytes() -> None:
    es = [
        _entry("r1", "a", "a" * 40, 600),
        _entry("r1", "b", "b" * 40, 500),  # first chunk: a+b=1100 > 1000? no, a=600 then b 600+500=1100 > 1000
        _entry("r1", "c", "c" * 40, 300),
        _entry("r1", "d", "d" * 40, 200),
    ]
    groups = sender._group_into_chunks(es, max_archive_bytes=1000)
    # Greedy: a(600) fits, +b(500)=1100 > 1000 -> split.
    # New chunk: b(500), +c(300)=800, +d(200)=1000 fits exactly.
    assert [[e.repo_path for e in g] for g in groups] == [["a"], ["b", "c", "d"]]


def test_group_into_chunks_oversize_entry_isolated() -> None:
    # A single blob larger than max gets its own chunk rather than being
    # split (we never fragment a blob). Subsequent smaller entries start
    # a fresh chunk.
    es = [
        _entry("r1", "tiny", "1" * 40, 50),
        _entry("r1", "huge", "2" * 40, 5000),
        _entry("r1", "next", "3" * 40, 100),
    ]
    groups = sender._group_into_chunks(es, max_archive_bytes=1000)
    assert [[e.repo_path for e in g] for g in groups] == [["tiny"], ["huge"], ["next"]]


def test_group_into_chunks_dedups_sha1_within_logical_cycle() -> None:
    # Two manifest entries with the same sha1 (one blob, two repos) cost
    # the budget once. Both land in the same chunk because the second
    # contributes no new bytes.
    sha = "a" * 40
    es = [
        _entry("r1", "x", sha, 900),
        _entry("r2", "x", sha, 900),  # same sha; no new bytes
        _entry("r1", "y", "b" * 40, 150),
    ]
    groups = sender._group_into_chunks(es, max_archive_bytes=1000)
    # 900 (sha-a) + 0 (sha-a again) + 150 = 1050 > 1000 only on the third entry.
    assert [len(g) for g in groups] == [2, 1]


# -------- manifest v2 backcompat --------

def test_manifest_v2_loads_as_single_chunk_view() -> None:
    v2 = {
        "schema": 2,
        "cycle_id": "old-cycle",
        "prev_cycle_id": None,
        "created_at": 1700000000,
        "source_instance": "art-a",
        "repos": ["r1"],
        "blob_count": 0,
        "total_bytes": 0,
        "entries": [],
        "removed": [],
    }
    m = archive.Manifest.from_bytes(json.dumps(v2).encode("utf-8"))
    assert m.schema == 2
    assert m.parent_cycle_id == "old-cycle"
    assert m.chunk_seq == 1
    assert m.chunk_total == 1
    assert m.is_final_chunk


# -------- single-chunk path is byte-identical to legacy --------

def test_single_chunk_uses_legacy_filename(tmp_path: Path) -> None:
    sha = "a" * 40
    _make_blob(tmp_path / "filestore", sha, 100)
    settings = _settings(tmp_path, max_archive_bytes=0, spool_min_free_bytes=0)
    export = _make_export(tmp_path)
    cycle_id = archive.new_cycle_id()
    ok, _deferred = sender._emit_archives(
        settings,
        store=fs_store(settings.filestore_root),
        cycle_id=cycle_id,
        prev_cycle_id=None,
        export_contents=export,
        new_entries=[_entry("r1", "a", sha, 100)],
        removed_entries=[],
    )
    assert ok
    archives = list(settings.spool_dir.glob("*.tar.zst"))
    assert len(archives) == 1
    assert archives[0].name == f"{cycle_id}.tar.zst"
    m = archive.read_manifest(archives[0])
    assert m.chunk_total == 1
    assert m.chunk_seq == 1
    assert m.parent_cycle_id == cycle_id


# -------- multi-chunk emission --------

def test_multi_chunk_emission_layout(tmp_path: Path) -> None:
    # Three blobs of 600 bytes each; threshold 1000 forces 3 chunks.
    shas = [f"{i:0>40}" for i in ("a", "b", "c")]
    for s in shas:
        _make_blob(tmp_path / "filestore", s, 600)
    settings = _settings(
        tmp_path,
        max_archive_bytes=1000,
        spool_min_free_bytes=0,
    )
    export = _make_export(tmp_path)
    cycle_id = archive.new_cycle_id()
    entries = [_entry("r1", f"f{i}", s, 600) for i, s in enumerate(shas)]
    ok, _deferred = sender._emit_archives(
        settings,
        store=fs_store(settings.filestore_root),
        cycle_id=cycle_id,
        prev_cycle_id="prev",
        export_contents=export,
        new_entries=entries,
        removed_entries=[],
    )
    assert ok
    archives = sorted(settings.spool_dir.glob("*.tar.zst"))
    assert [p.name for p in archives] == [
        f"{cycle_id}-c001.tar.zst",
        f"{cycle_id}-c002.tar.zst",
        f"{cycle_id}-c003.tar.zst",
    ]
    manifests = [archive.read_manifest(p) for p in archives]
    assert [m.chunk_seq for m in manifests] == [1, 2, 3]
    assert all(m.chunk_total == 3 for m in manifests)
    assert all(m.parent_cycle_id == cycle_id for m in manifests)
    # Only the final chunk ships the metadata tree.
    import tarfile
    import zstandard as zstd
    def has_metadata(p: Path) -> bool:
        dctx = zstd.ZstdDecompressor()
        with open(p, "rb") as fh, dctx.stream_reader(fh) as zr:
            with tarfile.open(fileobj=zr, mode="r|") as tar:
                for member in tar:
                    if member.name.startswith("metadata/"):
                        return True
        return False
    assert not has_metadata(archives[0])
    assert not has_metadata(archives[1])
    assert has_metadata(archives[2])


def test_multi_chunk_final_carries_removed(tmp_path: Path) -> None:
    shas = ["a" * 40, "b" * 40]
    for s in shas:
        _make_blob(tmp_path / "filestore", s, 600)
    settings = _settings(tmp_path, max_archive_bytes=1000, spool_min_free_bytes=0)
    export = _make_export(tmp_path)
    cycle_id = archive.new_cycle_id()
    entries = [_entry("r1", f"f{i}", s, 600) for i, s in enumerate(shas)]
    removed = [_entry("r1", "old.bin", "f" * 40, 7)]
    ok, _deferred = sender._emit_archives(
        settings,
        store=fs_store(settings.filestore_root),
        cycle_id=cycle_id,
        prev_cycle_id="prev",
        export_contents=export,
        new_entries=entries,
        removed_entries=removed,
    )
    assert ok
    archives = sorted(settings.spool_dir.glob("*.tar.zst"))
    manifests = [archive.read_manifest(p) for p in archives]
    assert manifests[0].removed == []
    assert manifests[-1].removed == [
        {"sha1": "f" * 40, "repo": "r1", "path": "old.bin", "size": 7}
    ]


# -------- backpressure --------

def test_backpressure_aborts_before_first_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    _make_blob(tmp_path / "filestore", sha, 100)
    settings = _settings(
        tmp_path,
        max_archive_bytes=0,
        spool_min_free_bytes=10 * 1024**3,  # impossibly large min_free
    )
    export = _make_export(tmp_path)
    cycle_id = archive.new_cycle_id()

    class _Usage:
        free = 1 * 1024**3  # 1 GiB free, below min_free

    monkeypatch.setattr(sender.shutil, "disk_usage", lambda _p: _Usage())

    ok, _deferred = sender._emit_archives(
        settings,
        store=fs_store(settings.filestore_root),
        cycle_id=cycle_id,
        prev_cycle_id=None,
        export_contents=export,
        new_entries=[_entry("r1", "a", sha, 100)],
        removed_entries=[],
    )
    assert ok is False
    # No archives, no partials.
    assert list(settings.spool_dir.glob("*.tar.zst*")) == []


def test_backpressure_mid_stream_cleans_up_prior_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three chunks expected; the second one should trip backpressure and
    # take the first chunk down with it on the way out.
    shas = ["a" * 40, "b" * 40, "c" * 40]
    for s in shas:
        _make_blob(tmp_path / "filestore", s, 600)
    settings = _settings(
        tmp_path,
        max_archive_bytes=1000,
        spool_min_free_bytes=0,
    )
    export = _make_export(tmp_path)
    cycle_id = archive.new_cycle_id()
    entries = [_entry("r1", f"f{i}", s, 600) for i, s in enumerate(shas)]

    calls = {"n": 0}

    class _Usage:
        def __init__(self, free: int) -> None:
            self.free = free

    def fake_usage(_p):
        calls["n"] += 1
        # Plenty of room on the first call (first chunk fine), starved on
        # subsequent calls so chunk 2's projection exceeds free.
        if calls["n"] == 1:
            return _Usage(_OVERHEAD + 1_000_000)
        return _Usage(1)

    monkeypatch.setattr(sender.shutil, "disk_usage", fake_usage)

    ok, _deferred = sender._emit_archives(
        settings,
        store=fs_store(settings.filestore_root),
        cycle_id=cycle_id,
        prev_cycle_id="prev",
        export_contents=export,
        new_entries=entries,
        removed_entries=[],
    )
    assert ok is False
    # All chunks for the parent removed on abort, including the one that
    # successfully landed before backpressure tripped.
    assert list(settings.spool_dir.glob(f"{cycle_id}*.tar.zst*")) == []

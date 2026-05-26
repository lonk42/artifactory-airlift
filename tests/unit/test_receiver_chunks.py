"""Receiver-side tests for chunked-cycle archives.

The "commit" chunk (chunk_seq == chunk_total) is the only one that runs
import_repositories and applies removals. Earlier chunks just stage
blobs into the filestore and record themselves in processed.jsonl with
status="blob-staged". A final chunk that arrives before its earlier
siblings stays in spool and is re-evaluated on the next receiver tick.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from artifactory_airlift import archive, receiver
from artifactory_airlift.config import Settings
from artifactory_airlift.export_unpacker import ArtifactEntry


class _StubClient:
    def __init__(self) -> None:
        self.import_calls: list[Path] = []
        self.delete_calls: list[tuple[str, str]] = []

    def ping(self) -> bool:
        return True

    def import_repositories(self, path: Path) -> str:
        self.import_calls.append(path)
        return ""

    def delete_artifact(self, repo_key: str, repo_path: str) -> int:
        self.delete_calls.append((repo_key, repo_path))
        return 200


def _settings(tmp_path: Path) -> Settings:
    state_dir = tmp_path / "state"
    spool_dir = tmp_path / "spool"
    filestore_root = tmp_path / "fs"
    for p in (state_dir, spool_dir, filestore_root, spool_dir / ".done"):
        p.mkdir(parents=True, exist_ok=True)
    return Settings(
        mode="receiver",
        state_dir=state_dir,
        spool_dir=spool_dir,
        filestore_root=filestore_root,
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
    )


def _make_blob(filestore_root: Path, sha1: str, size: int) -> None:
    blob = filestore_root / sha1[:2] / sha1
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"x" * size)


def _make_export(tmp_path: Path) -> Path:
    root = tmp_path / "export"
    (root / "repositories" / "r1").mkdir(parents=True)
    return root


def _build_chunk(
    tmp_path: Path,
    *,
    parent_cycle_id: str,
    chunk_seq: int,
    chunk_total: int,
    entry_sha: str,
    include_metadata: bool,
) -> Path:
    """Build a single chunk archive directly in the receiver's spool."""
    _make_blob(tmp_path / "fs_src", entry_sha, 11)
    export = _make_export(tmp_path)
    spool = tmp_path / "build_spool"
    return archive.build(
        spool_dir=spool,
        cycle_id=f"{parent_cycle_id}-c{chunk_seq:03d}",
        prev_cycle_id="prev",
        source_instance="art-a",
        export_root=export,
        entries=[ArtifactEntry(repo_key="r1", repo_path=f"f{chunk_seq}", sha1=entry_sha, size=11)],
        filestore_root=tmp_path / "fs_src",
        archive_name=f"{parent_cycle_id}-c{chunk_seq:03d}.tar.zst",
        parent_cycle_id=parent_cycle_id,
        chunk_seq=chunk_seq,
        chunk_total=chunk_total,
        include_metadata=include_metadata,
    )


def _last_processed(processed_path: Path) -> dict:
    line = processed_path.read_text().splitlines()[-1]
    return json.loads(line)


def test_blob_only_chunk_skips_import_and_records_staged(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    parent = "1700000000-aaaaaaaa"
    p = _build_chunk(
        tmp_path,
        parent_cycle_id=parent,
        chunk_seq=1,
        chunk_total=3,
        entry_sha="a" * 40,
        include_metadata=False,
    )
    target = settings.spool_dir / p.name
    p.replace(target)

    client = _StubClient()
    parent_chunks: dict[str, set[int]] = {}
    receiver._process_one(
        settings,
        client=client,
        archive_path=target,
        processed=set(),
        parent_chunks=parent_chunks,
        processed_path=settings.state_dir / "processed.jsonl",
        done_dir=settings.spool_dir / ".done",
    )

    # No import, no delete.
    assert client.import_calls == []
    assert client.delete_calls == []
    # Ledger row marks the chunk staged and tracks the parent.
    row = _last_processed(settings.state_dir / "processed.jsonl")
    assert row["status"] == "blob-staged"
    assert row["parent_cycle_id"] == parent
    assert row["chunk_seq"] == 1
    assert row["chunk_total"] == 3
    # parent_chunks updated so a later final chunk for this parent passes.
    assert parent_chunks[parent] == {1}
    # Archive moved to .done.
    assert not target.exists()
    assert (settings.spool_dir / ".done" / target.name).exists()


def test_final_chunk_waits_when_earlier_chunks_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    parent = "1700000001-bbbbbbbb"
    # Build the final chunk only; chunks 1 and 2 have not been processed.
    p = _build_chunk(
        tmp_path,
        parent_cycle_id=parent,
        chunk_seq=3,
        chunk_total=3,
        entry_sha="b" * 40,
        include_metadata=True,
    )
    target = settings.spool_dir / p.name
    p.replace(target)

    client = _StubClient()
    parent_chunks: dict[str, set[int]] = {}  # nothing seen yet
    receiver._process_one(
        settings,
        client=client,
        archive_path=target,
        processed=set(),
        parent_chunks=parent_chunks,
        processed_path=settings.state_dir / "processed.jsonl",
        done_dir=settings.spool_dir / ".done",
    )

    # Skipped: no import, no delete, archive still in spool, no ledger row.
    assert client.import_calls == []
    assert client.delete_calls == []
    assert target.exists()
    assert not (settings.state_dir / "processed.jsonl").exists()


def test_final_chunk_runs_import_once_predecessors_recorded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    parent = "1700000002-cccccccc"
    p = _build_chunk(
        tmp_path,
        parent_cycle_id=parent,
        chunk_seq=2,
        chunk_total=2,
        entry_sha="c" * 40,
        include_metadata=True,
    )
    target = settings.spool_dir / p.name
    p.replace(target)

    client = _StubClient()
    # Predecessor chunk 1 already staged.
    parent_chunks: dict[str, set[int]] = {parent: {1}}
    receiver._process_one(
        settings,
        client=client,
        archive_path=target,
        processed=set(),
        parent_chunks=parent_chunks,
        processed_path=settings.state_dir / "processed.jsonl",
        done_dir=settings.spool_dir / ".done",
    )

    assert len(client.import_calls) == 1
    row = _last_processed(settings.state_dir / "processed.jsonl")
    assert row["status"] == "ok"
    assert row["parent_cycle_id"] == parent
    assert row["chunk_seq"] == 2
    assert row["chunk_total"] == 2
    assert parent_chunks[parent] == {1, 2}

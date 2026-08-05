"""Receiver-side tests for deletion propagation."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ._store import fs_store
from artifactory_airlift import archive, receiver
from artifactory_airlift.config import Settings
from artifactory_airlift.export_unpacker import ArtifactEntry


class _StubClient:
    def __init__(
        self,
        *,
        delete_status: dict[tuple[str, str], int] | None = None,
        delete_raise: set[tuple[str, str]] | None = None,
    ) -> None:
        self.delete_status = delete_status or {}
        self.delete_raise = delete_raise or set()
        self.delete_calls: list[tuple[str, str]] = []
        self.import_calls: list[Path] = []

    def ping(self) -> bool:
        return True

    def import_repositories(self, path: Path) -> str:
        self.import_calls.append(path)
        return ""

    def delete_artifact(self, repo_key: str, repo_path: str) -> int:
        self.delete_calls.append((repo_key, repo_path))
        key = (repo_key, repo_path)
        if key in self.delete_raise:
            raise RuntimeError("simulated delete failure")
        return self.delete_status.get(key, 200)


def _build_archive_with_removals(
    tmp_path: Path, removed_entries: list[ArtifactEntry]
) -> Path:
    """Build a minimal real archive (no entries, just removals)."""
    export_root = tmp_path / "export"
    (export_root / "repositories").mkdir(parents=True)
    spool = tmp_path / "spool"
    cycle_id = archive.new_cycle_id()
    built, _ = archive.build(
        spool_dir=spool,
        cycle_id=cycle_id,
        prev_cycle_id="prev",
        source_instance="art-a",
        export_root=export_root,
        entries=[],
        store=fs_store(tmp_path / "filestore"),
        removed=removed_entries,
    )
    return built


def _settings(tmp_path: Path) -> Settings:
    state_dir = tmp_path / "state"
    spool_dir = tmp_path / "spool"
    filestore_root = tmp_path / "fs"
    for p in (state_dir, spool_dir, filestore_root):
        p.mkdir(parents=True, exist_ok=True)
    return Settings(
        mode="receiver",
        state_dir=state_dir,
        spool_dir=spool_dir,
        filestore_root=filestore_root,
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
    )


def _last_processed_entry(processed_path: Path) -> dict:
    line = processed_path.read_text().splitlines()[-1]
    return json.loads(line)


def test_receiver_applies_removals_success(tmp_path: Path) -> None:
    archive_path = _build_archive_with_removals(
        tmp_path,
        [
            ArtifactEntry(repo_key="r1", repo_path="a.bin", sha1="a" * 40, size=1),
            ArtifactEntry(repo_key="r1", repo_path="b.bin", sha1="b" * 40, size=2),
        ],
    )
    settings = _settings(tmp_path)
    # Move archive into the receiver's spool.
    target = settings.spool_dir / archive_path.name
    archive_path.replace(target)

    done_dir = settings.spool_dir / ".done"
    done_dir.mkdir()
    processed_path = settings.state_dir / "processed.jsonl"

    client = _StubClient()
    receiver._process_one(
        settings,
        client=client,
        store=fs_store(settings.filestore_root),
        archive_path=target,
        processed=set(),
        parent_chunks={},
        processed_path=processed_path,
        done_dir=done_dir,
    )

    assert sorted(client.delete_calls) == [("r1", "a.bin"), ("r1", "b.bin")]
    entry = _last_processed_entry(processed_path)
    assert entry["status"] == "ok"
    assert entry["deleted_count"] == 2
    assert entry["delete_failures"] == []


def test_receiver_404_logged_not_partial(tmp_path: Path) -> None:
    archive_path = _build_archive_with_removals(
        tmp_path,
        [ArtifactEntry(repo_key="r1", repo_path="gone.bin", sha1="a" * 40, size=1)],
    )
    settings = _settings(tmp_path)
    target = settings.spool_dir / archive_path.name
    archive_path.replace(target)
    done_dir = settings.spool_dir / ".done"
    done_dir.mkdir()
    processed_path = settings.state_dir / "processed.jsonl"

    client = _StubClient(delete_status={("r1", "gone.bin"): 404})
    receiver._process_one(
        settings,
        client=client,
        store=fs_store(settings.filestore_root),
        archive_path=target,
        processed=set(),
        parent_chunks={},
        processed_path=processed_path,
        done_dir=done_dir,
    )

    entry = _last_processed_entry(processed_path)
    # 404 lands in delete_failures but status stays "ok": the desired state
    # (artifact gone on B) is already achieved.
    assert entry["status"] == "ok"
    assert entry["deleted_count"] == 1
    assert entry["delete_failures"] == ["r1/gone.bin: 404"]


def test_receiver_delete_exception_logged_not_partial(tmp_path: Path) -> None:
    archive_path = _build_archive_with_removals(
        tmp_path,
        [ArtifactEntry(repo_key="r1", repo_path="boom.bin", sha1="a" * 40, size=1)],
    )
    settings = _settings(tmp_path)
    target = settings.spool_dir / archive_path.name
    archive_path.replace(target)
    done_dir = settings.spool_dir / ".done"
    done_dir.mkdir()
    processed_path = settings.state_dir / "processed.jsonl"

    client = _StubClient(delete_raise={("r1", "boom.bin")})
    receiver._process_one(
        settings,
        client=client,
        store=fs_store(settings.filestore_root),
        archive_path=target,
        processed=set(),
        parent_chunks={},
        processed_path=processed_path,
        done_dir=done_dir,
    )

    entry = _last_processed_entry(processed_path)
    assert entry["status"] == "ok"  # only import failures flip to partial
    assert entry["deleted_count"] == 0
    assert len(entry["delete_failures"]) == 1
    assert "boom.bin" in entry["delete_failures"][0]


def test_receiver_no_removals_field_is_noop(tmp_path: Path) -> None:
    archive_path = _build_archive_with_removals(tmp_path, [])
    settings = _settings(tmp_path)
    target = settings.spool_dir / archive_path.name
    archive_path.replace(target)
    done_dir = settings.spool_dir / ".done"
    done_dir.mkdir()
    processed_path = settings.state_dir / "processed.jsonl"

    client = _StubClient()
    receiver._process_one(
        settings,
        client=client,
        store=fs_store(settings.filestore_root),
        archive_path=target,
        processed=set(),
        parent_chunks={},
        processed_path=processed_path,
        done_dir=done_dir,
    )

    assert client.delete_calls == []
    entry = _last_processed_entry(processed_path)
    assert entry["deleted_count"] == 0
    assert entry["delete_failures"] == []

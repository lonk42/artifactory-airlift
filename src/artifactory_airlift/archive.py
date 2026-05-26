from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import zstandard as zstd

from . import log
from .export_unpacker import ArtifactEntry

logger = log.get("artifactory.archive")

SCHEMA_VERSION = 3
MANIFEST_NAME = "manifest.json"
BLOBS_PREFIX = "blobs"
METADATA_PREFIX = "metadata"


@dataclass(slots=True)
class Manifest:
    schema: int
    cycle_id: str
    prev_cycle_id: str | None
    created_at: int
    source_instance: str
    repos: list[str]
    blob_count: int
    total_bytes: int
    entries: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    # Chunking metadata. For unchunked cycles parent_cycle_id == cycle_id and
    # chunk_seq == chunk_total == 1; chunked cycles share parent_cycle_id
    # across N archives and use chunk_seq to order them. Schema-v2 archives
    # omit these fields; from_bytes falls back to the single-chunk view.
    parent_cycle_id: str | None = None
    chunk_seq: int = 1
    chunk_total: int = 1

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "schema": self.schema,
                "cycle_id": self.cycle_id,
                "prev_cycle_id": self.prev_cycle_id,
                "created_at": self.created_at,
                "source_instance": self.source_instance,
                "repos": sorted(self.repos),
                "blob_count": self.blob_count,
                "total_bytes": self.total_bytes,
                "entries": self.entries,
                "removed": self.removed,
                "parent_cycle_id": self.parent_cycle_id or self.cycle_id,
                "chunk_seq": self.chunk_seq,
                "chunk_total": self.chunk_total,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "Manifest":
        d = json.loads(data)
        cycle_id = str(d["cycle_id"])
        return cls(
            schema=int(d["schema"]),
            cycle_id=cycle_id,
            prev_cycle_id=d.get("prev_cycle_id"),
            created_at=int(d["created_at"]),
            source_instance=str(d.get("source_instance", "")),
            repos=list(d.get("repos", [])),
            blob_count=int(d.get("blob_count", 0)),
            total_bytes=int(d.get("total_bytes", 0)),
            entries=list(d.get("entries", [])),
            removed=list(d.get("removed", [])),
            parent_cycle_id=d.get("parent_cycle_id") or cycle_id,
            chunk_seq=int(d.get("chunk_seq", 1)),
            chunk_total=int(d.get("chunk_total", 1)),
        )

    @property
    def is_final_chunk(self) -> bool:
        return self.chunk_seq >= self.chunk_total


def new_cycle_id() -> str:
    return f"{int(time.time()):010d}-{uuid.uuid4().hex[:8]}"


def build(
    *,
    spool_dir: Path,
    cycle_id: str,
    prev_cycle_id: str | None,
    source_instance: str,
    export_root: Path,
    entries: Iterable[ArtifactEntry],
    filestore_root: Path,
    removed: Iterable[ArtifactEntry] = (),
    zstd_level: int = 10,
    archive_name: str | None = None,
    parent_cycle_id: str | None = None,
    chunk_seq: int = 1,
    chunk_total: int = 1,
    include_metadata: bool = True,
    skip_blob_sha1s: Iterable[str] = (),
) -> Path:
    """Build a per-cycle archive atomically in ``spool_dir``.

    Returns the final archive path. Caller is responsible for ensuring
    ``spool_dir`` exists.

    Chunking: when ``chunk_total > 1`` the caller is splitting one logical
    cycle's diff across multiple archives. Pass ``parent_cycle_id`` (shared
    across chunks), ``chunk_seq`` (1-based), and ``archive_name`` to control
    the on-disk filename. Non-final chunks pass ``include_metadata=False``
    to skip the export tree (only the final chunk's import call needs it),
    and ``skip_blob_sha1s`` to suppress blobs already packed in earlier
    chunks (deduplication across the whole logical cycle).
    """
    spool_dir.mkdir(parents=True, exist_ok=True)
    staging = spool_dir / ".staging" / cycle_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    entries_list = list(entries)
    removed_list = list(removed)
    repos = sorted({e.repo_key for e in entries_list} | {e.repo_key for e in removed_list})
    total_bytes = 0
    skip_set = set(skip_blob_sha1s)

    final_name = archive_name or f"{cycle_id}.tar.zst"
    partial_path = spool_dir / f"{final_name}.partial"
    final_path = spool_dir / final_name

    cctx = zstd.ZstdCompressor(level=zstd_level)
    with open(partial_path, "wb") as out_fh, cctx.stream_writer(out_fh) as zwriter:
        with tarfile.open(fileobj=zwriter, mode="w|") as tar:
            # 1. Full system-export tree under metadata/. We need the
            #    siblings (etc/, artifactory.config.xml, artifactory.repository.config.json,
            #    licenses/, ...) because /api/import/system rejects partial
            #    trees, and the per-repo /import endpoint is broken in
            #    Artifactory 7.146.x (routes to updateRepository with an
            #    NPE on repositoryConfigMap).
            #
            #    Chunked cycles defer metadata to the final chunk (the
            #    "commit" chunk). Earlier chunks just stage blobs; the
            #    receiver only runs /api/import/repositories on the chunk
            #    that ships the tree, by which point all blobs from the
            #    sibling chunks are already in the filestore.
            if include_metadata:
                for entry in sorted(export_root.iterdir()):
                    tar.add(entry, arcname=f"{METADATA_PREFIX}/{entry.name}")

            # 2. Raw filestore blobs (dedup by sha1, plus skip any sha1
            #    already packed by an earlier chunk).
            seen: set[str] = set(skip_set)
            packed: set[str] = set()
            for entry in entries_list:
                if entry.sha1 in seen:
                    continue
                seen.add(entry.sha1)
                packed.add(entry.sha1)
                blob = filestore_root / entry.sha1[:2] / entry.sha1
                if not blob.is_file():
                    logger.warning(
                        "archive.missing_blob",
                        sha1=entry.sha1,
                        repo=entry.repo_key,
                        path=entry.repo_path,
                    )
                    continue
                size = blob.stat().st_size
                total_bytes += size
                tar.add(
                    blob,
                    arcname=f"{BLOBS_PREFIX}/{entry.sha1[:2]}/{entry.sha1}",
                )

            # 3. Manifest last so verifying readers can quickly find it
            #    by scanning a freshly written archive (though we extract
            #    it explicitly by name on the receive side).
            manifest = Manifest(
                schema=SCHEMA_VERSION,
                cycle_id=cycle_id,
                prev_cycle_id=prev_cycle_id,
                created_at=int(time.time()),
                source_instance=source_instance,
                repos=repos,
                blob_count=len(packed),
                total_bytes=total_bytes,
                entries=[
                    {
                        "sha1": e.sha1,
                        "repo": e.repo_key,
                        "path": e.repo_path,
                        "size": e.size,
                    }
                    for e in entries_list
                ],
                removed=[
                    {
                        "sha1": e.sha1,
                        "repo": e.repo_key,
                        "path": e.repo_path,
                        "size": e.size,
                    }
                    for e in removed_list
                ],
                parent_cycle_id=parent_cycle_id or cycle_id,
                chunk_seq=chunk_seq,
                chunk_total=chunk_total,
            )
            manifest_bytes = manifest.to_json()
            info = tarfile.TarInfo(name=MANIFEST_NAME)
            info.size = len(manifest_bytes)
            info.mtime = manifest.created_at
            info.mode = 0o644
            with tempfile.SpooledTemporaryFile() as fh:
                fh.write(manifest_bytes)
                fh.seek(0)
                tar.addfile(info, fh)

        out_fh.flush()
        os.fsync(out_fh.fileno())

    os.replace(partial_path, final_path)
    shutil.rmtree(staging, ignore_errors=True)
    archive_bytes = final_path.stat().st_size
    logger.info(
        "archive.built",
        path=str(final_path),
        blob_count=manifest.blob_count,
        total_bytes=manifest.total_bytes,
        total_bytes_human=log.human_bytes(manifest.total_bytes),
        size_bytes=archive_bytes,
        size_human=log.human_bytes(archive_bytes),
        repo_count=len(repos),
        repos=repos,
    )
    return final_path


def read_manifest(archive_path: Path) -> Manifest:
    dctx = zstd.ZstdDecompressor()
    with open(archive_path, "rb") as in_fh, dctx.stream_reader(in_fh) as zreader:
        with tarfile.open(fileobj=zreader, mode="r|") as tar:
            for member in tar:
                if member.name == MANIFEST_NAME:
                    fh = tar.extractfile(member)
                    if fh is None:
                        break
                    return Manifest.from_bytes(fh.read())
    raise ValueError(f"manifest not found in {archive_path}")


def extract(archive_path: Path, dest_dir: Path) -> Manifest:
    """Extract an archive to ``dest_dir``. Returns the parsed manifest.

    The archive may legitimately contain the manifest before or after
    blobs/metadata depending on writer ordering, so we stream everything
    out and parse the manifest from disk at the end.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dctx = zstd.ZstdDecompressor()
    with open(archive_path, "rb") as in_fh, dctx.stream_reader(in_fh) as zreader:
        with tarfile.open(fileobj=zreader, mode="r|") as tar:
            tar.extractall(path=dest_dir)
    manifest_path = dest_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"{MANIFEST_NAME} missing after extract of {archive_path}")
    return Manifest.from_bytes(manifest_path.read_bytes())

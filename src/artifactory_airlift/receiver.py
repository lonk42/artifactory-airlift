from __future__ import annotations

import shutil
import time
from collections import Counter
from pathlib import Path

from . import archive, binarystore, log, state
from .artifactory_client import ArtifactoryClient
from .config import Settings

logger = log.get("artifactory.receiver")


def run(settings: Settings) -> int:
    state_dir = settings.state_dir
    processed_path = state_dir / "processed.jsonl"
    lock_path = state_dir / "receiver.lock"
    done_dir = settings.spool_dir / ".done"

    for p in (state_dir, settings.spool_dir, done_dir):
        p.mkdir(parents=True, exist_ok=True)

    # The transport mechanism may have crashed mid-write, leaving a
    # ``.partial`` file in spool. The cycle loop's ``*.tar.zst`` glob
    # ignores partials so they would not cause processing issues, but
    # they sit on the spool PVC forever. Sweep once on startup.
    partials, staging = archive.sweep_orphan_partials(settings.spool_dir)
    if partials or staging:
        logger.info(
            "receiver.startup_sweep",
            partials_removed=partials,
            staging_dirs_removed=staging,
        )

    try:
        store = binarystore.resolve(settings)
    except Exception as exc:
        # A binarystore we cannot address is fatal: every cycle would write
        # blobs nowhere useful. Fail at boot rather than each cycle.
        logger.error("receiver.binarystore_unavailable", error=str(exc))
        return 2

    try:
        # Not just OSError any more: an object-storage backend reports an
        # unreachable endpoint or bad credentials as an HTTP/transport error.
        store.probe()
    except Exception as exc:
        logger.error("receiver.filestore_probe_failed", error=str(exc))
        store.close()
        return 2

    try:
        with state.file_lock(lock_path):
            return _loop(
                settings,
                store=store,
                processed_path=processed_path,
                done_dir=done_dir,
            )
    except RuntimeError as exc:
        logger.error("receiver.lock_held", error=str(exc))
        return 1
    finally:
        store.close()


def _loop(
    settings: Settings,
    *,
    store: "binarystore.BlobStore",
    processed_path: Path,
    done_dir: Path,
) -> int:
    client = ArtifactoryClient.from_settings(settings)
    try:
        while True:
            try:
                _cycle(
                    settings,
                    client=client,
                    store=store,
                    processed_path=processed_path,
                    done_dir=done_dir,
                )
            except Exception:
                logger.exception("receiver.cycle_failed")
            time.sleep(settings.cycle_seconds)
    finally:
        client.close()


def _cycle(
    settings: Settings,
    *,
    client: ArtifactoryClient,
    store: "binarystore.BlobStore",
    processed_path: Path,
    done_dir: Path,
) -> None:
    if not client.ping():
        logger.warning("receiver.ping_not_ok")
        return

    processed, parent_chunks = _load_processed(processed_path)
    _prune_done(done_dir, settings.done_keep_hours)

    archives = sorted(settings.spool_dir.glob("*.tar.zst"))
    if not archives:
        return

    for archive_path in archives:
        cycle_id = archive_path.name.removesuffix(".tar.zst")
        if cycle_id in processed:
            continue

        try:
            _process_one(
                settings,
                client=client,
                store=store,
                archive_path=archive_path,
                processed=processed,
                parent_chunks=parent_chunks,
                processed_path=processed_path,
                done_dir=done_dir,
            )
        except Exception:
            logger.exception("receiver.archive_failed", cycle_id=cycle_id)


def _process_one(
    settings: Settings,
    *,
    client: ArtifactoryClient,
    store: "binarystore.BlobStore",
    archive_path: Path,
    processed: set[str],
    parent_chunks: dict[str, set[int]],
    processed_path: Path,
    done_dir: Path,
) -> None:
    cycle_id = archive_path.name.removesuffix(".tar.zst")
    # Extract under the state PVC, not the artifactory data dir;
    # /api/import/repositories rejects paths under /var/opt/jfrog/artifactory/
    # the same way /api/import/system does. The state PVC is mounted in
    # both the airlift and artifactory containers, so the artifactory
    # process can read what we write here.
    work_dir = settings.state_dir / "import" / cycle_id
    if work_dir.exists():
        shutil.rmtree(work_dir)

    archive_size = archive_path.stat().st_size
    logger.info(
        "receiver.extract_start",
        cycle_id=cycle_id,
        path=str(archive_path),
        size_bytes=archive_size,
        size_human=log.human_bytes(archive_size),
    )
    manifest = archive.extract(archive_path, work_dir)
    logger.info(
        "receiver.manifest_loaded",
        cycle_id=cycle_id,
        blob_count=manifest.blob_count,
        total_bytes=manifest.total_bytes,
        total_bytes_human=log.human_bytes(manifest.total_bytes),
        repo_count=len(manifest.repos),
        repos=manifest.repos,
        removed_count=len(manifest.removed),
    )

    if manifest.cycle_id != cycle_id:
        logger.warning(
            "receiver.cycle_id_mismatch",
            file_cycle_id=cycle_id,
            manifest_cycle_id=manifest.cycle_id,
        )
        cycle_id = manifest.cycle_id

    prev = manifest.prev_cycle_id
    if prev is not None and prev not in processed:
        logger.warning(
            "receiver.gap_detected",
            cycle_id=cycle_id,
            prev_cycle_id=prev,
            note="processing anyway",
        )

    # Chunked-cycle ordering guard. The sender's "commit" chunk (the final
    # one) carries the metadata tree and the removed[] list; it must not be
    # processed until every earlier chunk for the same parent has staged
    # its blobs into the filestore, otherwise import_repositories will fail
    # for artifacts whose bytes haven't landed yet. Leave the archive in
    # spool; the next receiver tick re-evaluates after the missing chunks
    # are processed.
    is_chunked = manifest.chunk_total > 1
    parent = manifest.parent_cycle_id or cycle_id
    if is_chunked and manifest.is_final_chunk:
        seen = parent_chunks.get(parent, set())
        expected = set(range(1, manifest.chunk_total))
        missing = sorted(expected - seen)
        if missing:
            logger.info(
                "receiver.chunk_waiting",
                cycle_id=cycle_id,
                parent_cycle_id=parent,
                chunk_seq=manifest.chunk_seq,
                chunk_total=manifest.chunk_total,
                missing=missing,
            )
            shutil.rmtree(work_dir, ignore_errors=True)
            return

    blobs_root = work_dir / archive.BLOBS_PREFIX
    written = 0
    skipped = 0
    if blobs_root.is_dir():
        for two in sorted(blobs_root.iterdir()):
            if not two.is_dir():
                continue
            for blob_file in sorted(two.iterdir()):
                if not blob_file.is_file():
                    continue
                sha1 = blob_file.name
                created = store.write(blob_file, sha1)
                if created:
                    written += 1
                else:
                    skipped += 1

    logger.info(
        "receiver.blobs_written",
        cycle_id=cycle_id,
        written=written,
        skipped=skipped,
    )

    # Non-final chunks of a chunked cycle just stage blobs into the
    # binarystore. The "commit" chunk (with chunk_seq == chunk_total) is the
    # one that carries the metadata tree and runs import_repositories +
    # deletes against the now-complete blob set. Record the staged chunk
    # in processed.jsonl with a distinct status so operators can grep for
    # parent_cycle_id and see the full chunk set.
    if is_chunked and not manifest.is_final_chunk:
        state.append_jsonl(
            processed_path,
            {
                "cycle_id": cycle_id,
                "parent_cycle_id": parent,
                "chunk_seq": manifest.chunk_seq,
                "chunk_total": manifest.chunk_total,
                "status": "blob-staged",
                "blob_count": manifest.blob_count,
                "total_bytes": manifest.total_bytes,
                "repos": manifest.repos,
                "processed_at": int(time.time()),
            },
        )
        processed.add(cycle_id)
        parent_chunks.setdefault(parent, set()).add(manifest.chunk_seq)
        shutil.rmtree(work_dir, ignore_errors=True)
        done_target = done_dir / archive_path.name
        archive_path.replace(done_target)
        logger.info(
            "receiver.chunk_staged",
            cycle_id=cycle_id,
            parent_cycle_id=parent,
            chunk_seq=manifest.chunk_seq,
            chunk_total=manifest.chunk_total,
            written=written,
            skipped=skipped,
            moved_to=str(done_target),
        )
        return

    # Per-repo summary of what this archive carries, derived from the manifest
    # entries[]. This is the same data Artifactory will see in the import call,
    # so a "10 to airlift-rpm-local" line up here matches the import outcome
    # below.
    added_per_repo = Counter(e.get("repo", "") for e in manifest.entries)
    removed_per_repo = Counter(r.get("repo", "") for r in manifest.removed)
    if added_per_repo or removed_per_repo:
        repos_seen = sorted(set(added_per_repo) | set(removed_per_repo))
        parts = []
        for repo in repos_seen:
            a, r = added_per_repo.get(repo, 0), removed_per_repo.get(repo, 0)
            if a and r:
                parts.append(f"{repo}=+{a}/-{r}")
            elif a:
                parts.append(f"{repo}=+{a}")
            else:
                parts.append(f"{repo}=-{r}")
        logger.info(
            "receiver.per_repo_changes",
            cycle_id=cycle_id,
            added=dict(added_per_repo),
            removed=dict(removed_per_repo),
            summary=", ".join(parts),
        )

    metadata_root = work_dir / archive.METADATA_PREFIX / "repositories"
    failures: list[str] = []
    response_text = ""
    if metadata_root.is_dir():
        try:
            response_text = client.import_repositories(metadata_root)
        except Exception as exc:
            logger.exception("receiver.import_failed", path=str(metadata_root))
            failures.append(f"import_repositories: {exc}")
        else:
            # The endpoint returns 200 even when individual repos fail.
            # Per-repo failures show up as lines like:
            #   "500 : No directory for repository <repo> found at <path>"
            # System repos that have no exported content (e.g.
            # artifactory-build-info, jfrog-usage-logs) commonly hit
            # this; they're benign for our use case but worth recording.
            for line in response_text.splitlines():
                stripped = line.strip()
                if stripped.startswith(("500 :", "400 :", "404 :", "Error")):
                    failures.append(stripped)
            if failures:
                logger.warning(
                    "receiver.import_partial",
                    cycle_id=cycle_id,
                    failures=len(failures),
                )
                # Emit one WARN per failure so they grep cleanly and the count
                # above reconciles with detail lines below it.
                for i, line in enumerate(failures, 1):
                    code, _, detail = line.partition(" : ")
                    logger.warning(
                        "receiver.import_failure",
                        cycle_id=cycle_id,
                        index=i,
                        total=len(failures),
                        code=code.strip(),
                        detail=detail.strip() or line,
                    )

    # Apply removals after imports: if a sha1 appears in both entries[] and
    # removed[] under different (repo, path)s, the import re-creates the new
    # link before we drop the old one.
    delete_failures: list[str] = []
    deleted = 0
    for r in manifest.removed:
        repo_key = r.get("repo", "")
        repo_path = r.get("path", "")
        try:
            status_code = client.delete_artifact(repo_key, repo_path)
        except Exception as exc:
            logger.warning(
                "receiver.delete_failed",
                repo=repo_key,
                path=repo_path,
                error=str(exc),
            )
            delete_failures.append(f"{repo_key}/{repo_path}: {exc}")
            continue
        if status_code == 404:
            logger.warning(
                "receiver.delete_missing",
                repo=repo_key,
                path=repo_path,
            )
            delete_failures.append(f"{repo_key}/{repo_path}: 404")
        deleted += 1

    if manifest.removed:
        logger.info(
            "receiver.deletes_applied",
            cycle_id=cycle_id,
            deleted=deleted,
            failed=len(delete_failures),
        )

    status = "ok" if not failures else "partial"
    row = {
        "cycle_id": cycle_id,
        "status": status,
        "blob_count": manifest.blob_count,
        "total_bytes": manifest.total_bytes,
        "repos": manifest.repos,
        "failures": failures,
        "deleted_count": deleted,
        "delete_failures": delete_failures,
        "processed_at": int(time.time()),
    }
    if is_chunked:
        row["parent_cycle_id"] = parent
        row["chunk_seq"] = manifest.chunk_seq
        row["chunk_total"] = manifest.chunk_total
    state.append_jsonl(processed_path, row)
    processed.add(cycle_id)
    if is_chunked:
        parent_chunks.setdefault(parent, set()).add(manifest.chunk_seq)
    shutil.rmtree(work_dir, ignore_errors=True)

    done_target = done_dir / archive_path.name
    archive_path.replace(done_target)
    logger.info(
        "receiver.cycle_done",
        cycle_id=cycle_id,
        status=status,
        moved_to=str(done_target),
    )


def _load_processed(path: Path) -> tuple[set[str], dict[str, set[int]]]:
    """Return (cycle_ids, parent->{chunk_seq}) from the ledger.

    The first set drives the per-archive dedup check. The second map is
    consulted by chunked-cycle "commit" chunks to verify all earlier
    chunks have already staged their blobs.
    """
    seen: set[str] = set()
    parent_chunks: dict[str, set[int]] = {}
    for entry in state.read_jsonl(path):
        cid = entry.get("cycle_id")
        if isinstance(cid, str):
            seen.add(cid)
        parent = entry.get("parent_cycle_id")
        seq = entry.get("chunk_seq")
        if isinstance(parent, str) and isinstance(seq, int):
            parent_chunks.setdefault(parent, set()).add(seq)
    return seen, parent_chunks


def _prune_done(done_dir: Path, hours: int) -> None:
    if hours <= 0 or not done_dir.is_dir():
        return
    cutoff = time.time() - hours * 3600
    for p in done_dir.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue

from __future__ import annotations

import shutil
import time
from collections import Counter
from pathlib import Path

from . import archive, export_unpacker, log, state
from .artifactory_client import ArtifactoryClient
from .config import Settings

logger = log.get("artifactory.sender")


def run(settings: Settings) -> int:
    state_dir = settings.state_dir
    snapshots_dir = state_dir / "snapshots"
    exports_dir = state_dir / "exports"
    cursor_path = state_dir / "cursor.json"
    lock_path = state_dir / "sender.lock"

    for p in (state_dir, snapshots_dir, exports_dir, settings.spool_dir):
        p.mkdir(parents=True, exist_ok=True)

    try:
        with state.file_lock(lock_path):
            return _loop(
                settings,
                snapshots_dir=snapshots_dir,
                exports_dir=exports_dir,
                cursor_path=cursor_path,
            )
    except RuntimeError as exc:
        logger.error("sender.lock_held", error=str(exc))
        return 1


def _loop(
    settings: Settings,
    *,
    snapshots_dir: Path,
    exports_dir: Path,
    cursor_path: Path,
) -> int:
    client = ArtifactoryClient(
        settings.artifactory_url,
        settings.artifactory_token,
        username=settings.artifactory_username,
        password=settings.artifactory_password,
    )
    try:
        while True:
            try:
                _cycle(
                    settings,
                    client=client,
                    snapshots_dir=snapshots_dir,
                    exports_dir=exports_dir,
                    cursor_path=cursor_path,
                )
            except Exception:
                logger.exception("sender.cycle_failed")
            time.sleep(settings.cycle_seconds)
    finally:
        client.close()


def _cycle(
    settings: Settings,
    *,
    client: ArtifactoryClient,
    snapshots_dir: Path,
    exports_dir: Path,
    cursor_path: Path,
) -> None:
    if not client.ping():
        logger.warning("sender.ping_not_ok")
        return

    cycle_id = archive.new_cycle_id()
    export_root = exports_dir / cycle_id
    export_root.mkdir(parents=True, exist_ok=True)

    logger.info("sender.cycle_start", cycle_id=cycle_id)
    client.export_system(export_root)

    # Artifactory writes the export into a single timestamped subdirectory
    # under our exportPath (e.g. `20260511.024656/`), with `repositories/`
    # and friends inside it. Find that one subdir and treat it as the
    # actual export root.
    export_contents = _locate_export_contents(export_root)
    excluded = _resolve_excluded_repos(client, settings)
    if excluded:
        logger.info(
            "sender.repos_excluded",
            cycle_id=cycle_id,
            repos=sorted(excluded),
            count=len(excluded),
        )
    snapshot_path = snapshots_dir / f"{cycle_id}.jsonl"
    count = export_unpacker.write_snapshot(
        export_contents, snapshot_path, excluded_repos=excluded
    )
    snapshot_repo_counts = _count_snapshot_repos(snapshot_path)
    logger.info(
        "sender.snapshot_written",
        cycle_id=cycle_id,
        count=count,
        repo_count=len(snapshot_repo_counts),
    )
    if snapshot_repo_counts:
        logger.info(
            "sender.per_repo_counts",
            cycle_id=cycle_id,
            repos=dict(snapshot_repo_counts),
            summary=log._fmt_counter_dict(snapshot_repo_counts),
        )

    cursor = state.read_json(cursor_path, default={}) or {}
    prev_cycle_id = cursor.get("last_cycle_id") if isinstance(cursor, dict) else None
    prev_snapshot = (
        snapshots_dir / f"{prev_cycle_id}.jsonl" if prev_cycle_id else None
    )
    if prev_snapshot is not None and not prev_snapshot.exists():
        logger.warning(
            "sender.previous_snapshot_missing",
            prev_cycle_id=prev_cycle_id,
            path=str(prev_snapshot),
        )
        prev_snapshot = None

    from . import diff

    new_entries = list(diff.added(prev_snapshot, snapshot_path))

    removed_entries: list = []
    if settings.propagate_deletes:
        if prev_snapshot is None:
            # Cold start: a missing baseline cannot distinguish "deleted on A"
            # from "never seen", so we never emit removals on the first cycle.
            logger.info("sender.removals_skipped_cold_start", cycle_id=cycle_id)
        else:
            removed_entries = list(diff.removed(prev_snapshot, snapshot_path))

    logger.info(
        "sender.diff_computed",
        cycle_id=cycle_id,
        prev_cycle_id=prev_cycle_id,
        added=len(new_entries),
        removed=len(removed_entries),
    )

    if new_entries or removed_entries:
        added_per_repo = Counter(e.repo_key for e in new_entries)
        removed_per_repo = Counter(e.repo_key for e in removed_entries)
        # Combined summary like "airlift-rpm-local=+3, airlift-npm-local=-1".
        # When both directions touch the same repo the entry shows the net,
        # e.g. "airlift-foo=+2/-1".
        repos = sorted(set(added_per_repo) | set(removed_per_repo))
        summary_parts = []
        for repo in repos:
            a, r = added_per_repo.get(repo, 0), removed_per_repo.get(repo, 0)
            if a and r:
                summary_parts.append(f"{repo}=+{a}/-{r}")
            elif a:
                summary_parts.append(f"{repo}=+{a}")
            else:
                summary_parts.append(f"{repo}=-{r}")
        logger.info(
            "sender.per_repo_changes",
            cycle_id=cycle_id,
            added=dict(added_per_repo),
            removed=dict(removed_per_repo),
            summary=", ".join(summary_parts),
        )

    if not new_entries and not removed_entries and prev_cycle_id is not None:
        # No new data; don't emit an empty archive, but advance the cursor so
        # the next cycle's diff baseline rolls forward.
        _advance_cursor(cursor_path, cycle_id)
        _prune_history(
            settings,
            snapshots_dir=snapshots_dir,
            exports_dir=exports_dir,
        )
        logger.info("sender.no_changes", cycle_id=cycle_id)
        return

    if not _emit_archives(
        settings,
        cycle_id=cycle_id,
        prev_cycle_id=prev_cycle_id,
        export_contents=export_contents,
        new_entries=new_entries,
        removed_entries=removed_entries,
    ):
        # Backpressure or other abort. Do not advance the cursor: next
        # cycle re-runs the diff against the same baseline.
        return

    _advance_cursor(cursor_path, cycle_id)
    _prune_history(
        settings,
        snapshots_dir=snapshots_dir,
        exports_dir=exports_dir,
    )


# Headroom budgeted on top of the raw blob bytes when projecting a chunk's
# on-disk footprint. Covers the metadata tree (final chunk only), tar
# block padding, and a margin for compressed sizes occasionally exceeding
# raw for already-compressed package formats. 256 MiB is plenty for the
# JFrog system-export tree on a populated cluster.
_CHUNK_OVERHEAD_BYTES = 256 * 1024 * 1024


def _group_into_chunks(
    entries: list,
    max_archive_bytes: int,
) -> list[list]:
    """Split entries into chunks whose cumulative new-blob bytes stay under
    ``max_archive_bytes``.

    Entries sharing a sha1 with one already placed in an earlier chunk
    do not consume budget in their own chunk (the blob ships once and is
    referenced by manifest entries in later chunks via filestore lookup
    on the receiver). A single entry larger than the threshold gets its
    own chunk rather than being split (we never fragment a blob).
    """
    if max_archive_bytes <= 0 or not entries:
        return [entries] if entries else []

    chunks: list[list] = []
    current: list = []
    current_bytes = 0
    seen_sha1s: set[str] = set()
    for e in entries:
        new_blob = e.sha1 not in seen_sha1s
        cost = e.size if new_blob else 0
        if current and new_blob and current_bytes + cost > max_archive_bytes:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(e)
        if new_blob:
            seen_sha1s.add(e.sha1)
            current_bytes += cost
    if current:
        chunks.append(current)
    return chunks


def _emit_archives(
    settings: Settings,
    *,
    cycle_id: str,
    prev_cycle_id: str | None,
    export_contents: Path,
    new_entries: list,
    removed_entries: list,
) -> bool:
    """Write all archives for one logical cycle.

    Returns True on success (every chunk written and finalised), False if
    backpressure aborted the cycle. On failure any partial chunks already
    landed in spool for this parent are removed so the receiver never sees
    an incomplete chunk set.
    """
    # Sort entries deterministically so chunk boundaries are stable across
    # re-runs (matters when the cycle aborts mid-stream and the next loop
    # tick re-emits the same diff).
    sorted_entries = sorted(new_entries, key=lambda e: (e.repo_key, e.repo_path))
    groups = _group_into_chunks(sorted_entries, settings.max_archive_bytes)
    if not groups:
        # No add-blobs but we still have removals to ship. Emit one
        # metadata+removed-only chunk.
        groups = [[]]
    chunk_total = len(groups)

    if chunk_total > 1:
        total_raw = sum(e.size for e in sorted_entries)
        logger.info(
            "sender.cycle_chunked",
            cycle_id=cycle_id,
            chunk_total=chunk_total,
            raw_bytes=total_raw,
            raw_bytes_human=log.human_bytes(total_raw),
            threshold_bytes=settings.max_archive_bytes,
            threshold_human=log.human_bytes(settings.max_archive_bytes),
        )

    seen_sha1s: set[str] = set()
    written_paths: list[Path] = []
    for i, group in enumerate(groups, 1):
        is_final = i == chunk_total
        # Project this chunk's on-disk size from its unseen-sha1 blob bytes
        # plus a fixed overhead for tar/zstd framing and (final-chunk only)
        # the metadata tree.
        chunk_bytes = sum(e.size for e in group if e.sha1 not in seen_sha1s)
        projected = chunk_bytes + _CHUNK_OVERHEAD_BYTES

        free = shutil.disk_usage(settings.spool_dir).free
        required = settings.spool_min_free_bytes + projected
        if free < required:
            logger.warning(
                "sender.spool_backpressure",
                cycle_id=cycle_id,
                chunk_seq=i,
                chunk_total=chunk_total,
                free_bytes=free,
                free_human=log.human_bytes(free),
                required_bytes=required,
                required_human=log.human_bytes(required),
                min_free_human=log.human_bytes(settings.spool_min_free_bytes),
                projected_human=log.human_bytes(projected),
            )
            _cleanup_partial_chunks(settings.spool_dir, cycle_id, written_paths)
            return False

        chunk_archive_name, chunk_cycle_id = _chunk_names(cycle_id, i, chunk_total)
        archive_path = archive.build(
            spool_dir=settings.spool_dir,
            cycle_id=chunk_cycle_id,
            prev_cycle_id=prev_cycle_id,
            source_instance=settings.instance_name,
            export_root=export_contents,
            entries=group,
            filestore_root=settings.filestore_root,
            removed=removed_entries if is_final else [],
            archive_name=chunk_archive_name,
            parent_cycle_id=cycle_id,
            chunk_seq=i,
            chunk_total=chunk_total,
            include_metadata=is_final,
            skip_blob_sha1s=seen_sha1s,
        )
        for e in group:
            seen_sha1s.add(e.sha1)
        written_paths.append(archive_path)

        archive_size = archive_path.stat().st_size
        archive_repos = sorted({e.repo_key for e in group})
        if chunk_total > 1:
            logger.info(
                "sender.chunk_finalized",
                cycle_id=chunk_cycle_id,
                parent_cycle_id=cycle_id,
                chunk_seq=i,
                chunk_total=chunk_total,
                path=str(archive_path),
                size_bytes=archive_size,
                size_human=log.human_bytes(archive_size),
                blob_count=len(group),
                repo_count=len(archive_repos),
                repos=archive_repos,
            )
        else:
            logger.info(
                "sender.archive_finalized",
                cycle_id=chunk_cycle_id,
                path=str(archive_path),
                size_bytes=archive_size,
                size_human=log.human_bytes(archive_size),
                blob_count=len(group),
                repo_count=len(archive_repos),
                repos=archive_repos,
            )
    return True


def _chunk_names(parent_cycle_id: str, seq: int, total: int) -> tuple[str, str]:
    """Return (archive_filename, chunk_cycle_id) for a chunk.

    Single-chunk cycles keep today's naming (``<cycle_id>.tar.zst``) so
    existing log greps and receiver bookkeeping are unaffected. Multi-chunk
    cycles use ``<parent_cycle_id>-cNNN.tar.zst`` so all chunks sort
    lexically after the bare cycle id and group together across multiple
    cycles in the spool listing.
    """
    if total <= 1:
        return f"{parent_cycle_id}.tar.zst", parent_cycle_id
    suffix = f"c{seq:03d}"
    chunk_cycle_id = f"{parent_cycle_id}-{suffix}"
    return f"{chunk_cycle_id}.tar.zst", chunk_cycle_id


def _cleanup_partial_chunks(
    spool_dir: Path, parent_cycle_id: str, written_paths: list[Path]
) -> None:
    """Remove any chunk files already on disk for ``parent_cycle_id``.

    Called when backpressure aborts a chunked cycle mid-stream. We delete
    both the recorded successful chunks and any stragglers matching the
    parent prefix (e.g. an in-flight ``.partial`` left by a crashed build).
    The cursor is not advanced, so the next cycle re-emits from the same
    snapshot diff.
    """
    for p in written_paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    for p in spool_dir.glob(f"{parent_cycle_id}*.tar.zst*"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _locate_export_contents(export_root: Path) -> Path:
    """Return the directory that actually contains `repositories/`.

    Artifactory nests the export tree inside a single timestamped
    subdirectory (e.g. `20260511.024656`). If that's the only entry,
    descend into it. Otherwise assume `repositories/` is at the top.
    """
    if (export_root / "repositories").is_dir():
        return export_root
    subdirs = [p for p in export_root.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "repositories").is_dir():
        return subdirs[0]
    return export_root


def _advance_cursor(cursor_path: Path, cycle_id: str) -> None:
    state.write_json_atomic(
        cursor_path,
        {"last_cycle_id": cycle_id, "last_success_at": int(time.time())},
    )


def _prune_history(
    settings: Settings,
    *,
    snapshots_dir: Path,
    exports_dir: Path,
    now: float | None = None,
) -> None:
    # Snapshots: GFS retention. Each tier keeps the newest snapshot per
    # non-empty bucket within its wall-clock window from now. The just-
    # written snapshot always wins its current bucket in any non-zero
    # tier, so the diff baseline for the next cycle is preserved.
    #
    # ``now`` is exposed for tests that need to pin the wall clock to a
    # known UTC time so bucket boundaries (especially the day boundary)
    # are deterministic across runs; production passes None and falls
    # back to time.time() inside gfs_keepers.
    snapshot_paths = list(snapshots_dir.glob("*.jsonl"))
    keepers = state.gfs_keepers(
        snapshot_paths,
        hours=settings.snapshot_retention_hours,
        days=settings.snapshot_retention_days,
        months=settings.snapshot_retention_months,
        now=now,
    )
    state.prune_to_keepers(snapshots_dir, keepers, pattern="*.jsonl")

    # Exports remain count-based; they are heavy scratch space and decoupled
    # from snapshot retention (a 6-month-old snapshot kept by the monthly
    # tier has no matching export tree, which is fine for the breadcrumb
    # use case).
    entries = sorted(p for p in exports_dir.iterdir() if p.is_dir())
    if len(entries) > settings.history_keep:
        for p in entries[: len(entries) - settings.history_keep]:
            shutil.rmtree(p, ignore_errors=True)


def _resolve_excluded_repos(
    client: ArtifactoryClient, settings: Settings
) -> set[str]:
    """Build the set of repo keys to drop from this cycle's snapshot.

    Combines the literal-name denylist with any repo whose packageType
    matches the configured denylist. If /api/repositories fails we fall
    back to the name-only set rather than aborting the cycle: the name
    list alone catches the common JFrog system repos.
    """
    excluded = set(settings.excluded_repos)
    type_deny = set(settings.excluded_package_types)
    if not type_deny:
        return excluded
    try:
        repos = client.list_repositories()
    except Exception as exc:
        logger.warning("sender.list_repositories_failed", error=str(exc))
        return excluded
    for r in repos:
        if r.get("packageType") in type_deny:
            key = r.get("key")
            if key:
                excluded.add(key)
    return excluded


def _count_snapshot_repos(snapshot_path: Path) -> Counter:
    """Return a Counter of artifact counts per repo from a JSONL snapshot."""
    counts: Counter = Counter()
    try:
        with snapshot_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Snapshot entries are JSON dicts with keys including "repo".
                # Cheap parse: extract the repo key without loading json.
                # Falls back to json on any oddity.
                try:
                    import json

                    counts[json.loads(line)["repo"]] += 1
                except (KeyError, ValueError):
                    continue
    except FileNotFoundError:
        return counts
    return counts

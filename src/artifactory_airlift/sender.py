from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path

from . import aql, archive, binarystore, log, metadata_synth, state
from .artifactory_client import ArtifactoryClient
from .config import Settings

logger = log.get("artifactory.sender")


def run(settings: Settings) -> int:
    state_dir = settings.state_dir
    snapshots_dir = state_dir / "snapshots"
    exports_dir = state_dir / "exports"
    cursor_path = state_dir / "cursor.json"
    ledger_path = state_dir / "cycles.jsonl"
    lock_path = state_dir / "sender.lock"

    for p in (state_dir, snapshots_dir, exports_dir, settings.spool_dir):
        p.mkdir(parents=True, exist_ok=True)

    # Clean up half-written archives left behind by a SIGKILL on a prior
    # run (OOM, node drain, container restart mid-build). Neither the
    # receiver nor the pending-gate look at .partial files, so they would
    # otherwise sit on the spool PVC indefinitely.
    partials, staging = archive.sweep_orphan_partials(settings.spool_dir)
    if partials or staging:
        logger.info(
            "sender.startup_sweep",
            partials_removed=partials,
            staging_dirs_removed=staging,
        )

    try:
        with state.file_lock(lock_path):
            return _loop(
                settings,
                snapshots_dir=snapshots_dir,
                exports_dir=exports_dir,
                cursor_path=cursor_path,
                ledger_path=ledger_path,
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
    ledger_path: Path | None = None,
) -> int:
    client = ArtifactoryClient.from_settings(settings)
    store: binarystore.BlobStore | None = None
    attempt = 0
    try:
        while True:
            # Resolved in the loop rather than before it: an unusable
            # binarystore leaves this cycle idle instead of ending the
            # process, which would take Artifactory's pod down with it.
            if store is None:
                attempt += 1
                store = binarystore.acquire(
                    settings, component="sender", attempt=attempt, probe=False
                )
                if store is None:
                    time.sleep(settings.cycle_seconds)
                    continue
                attempt = 0

            try:
                _cycle(
                    settings,
                    client=client,
                    store=store,
                    snapshots_dir=snapshots_dir,
                    exports_dir=exports_dir,
                    cursor_path=cursor_path,
                    ledger_path=ledger_path,
                )
            except Exception as exc:
                logger.exception("sender.cycle_failed")
                _record_cycle(ledger_path, status="failed", note=str(exc))
            time.sleep(settings.cycle_seconds)
    finally:
        if store is not None:
            store.close()
        client.close()


def _cycle(
    settings: Settings,
    *,
    client: ArtifactoryClient,
    store: "binarystore.BlobStore",
    snapshots_dir: Path,
    exports_dir: Path,
    cursor_path: Path,
    ledger_path: Path | None = None,
) -> None:
    started = time.time()
    if not client.ping():
        logger.warning("sender.ping_not_ok")
        _record_cycle(ledger_path, status="ping-failed", started=started)
        return

    # "One delta in flight" gate. If a prior cycle's archives are still in
    # spool waiting for the transport to pick them up, do not start a new
    # cycle. Producing another delta on top of pending ones piles snapshot
    # and export-tree orphans on the state PVC (no _prune_history call
    # happens until a cycle succeeds), produces deltas that race the
    # transport, and muddies the "diff since the last delivered cycle"
    # mental model. Glob only finalised "*.tar.zst" files; "*.partial"
    # files left by SIGKILL during a build are out of scope here and
    # should be reaped by a separate startup sweep.
    pending = sorted(settings.spool_dir.glob("*.tar.zst"))
    if pending:
        logger.info(
            "sender.cycle_skipped_pending",
            pending_count=len(pending),
            pending=[p.name for p in pending],
        )
        _record_cycle(
            ledger_path,
            status="skipped-pending",
            started=started,
            note=f"{len(pending)} archive(s) still in spool",
            archives=[p.name for p in pending],
        )
        return

    cycle_id = archive.new_cycle_id()

    logger.info("sender.cycle_start", cycle_id=cycle_id)

    excluded = _resolve_excluded_repos(client, settings)
    if excluded:
        logger.info(
            "sender.repos_excluded",
            cycle_id=cycle_id,
            repos=sorted(excluded),
            count=len(excluded),
        )
    included = set(settings.included_repos)
    if included:
        logger.info(
            "sender.repos_included",
            cycle_id=cycle_id,
            repos=sorted(included),
            count=len(included),
        )
    snapshot_path = snapshots_dir / f"{cycle_id}.jsonl"
    count = aql.write_snapshot(
        client,
        snapshot_path,
        excluded_repos=excluded,
        included_repos=included,
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

    # Deletion brake. Enumeration is now a database query, which can come
    # back short while still looking healthy; a filesystem walk failed
    # loudly. A short enumeration is indistinguishable from mass deletion,
    # so cap how much of the mirror one cycle may remove and refuse rather
    # than guess. Cause-agnostic on purpose: it catches a collapsed
    # projection, a partial result, a hidden repo, or a genuinely
    # catastrophic delete on the source, all the same way.
    if removed_entries and prev_snapshot is not None:
        baseline = _count_snapshot_entries(prev_snapshot)
        fraction = len(removed_entries) / baseline if baseline else 1.0
        if fraction > settings.max_delete_fraction:
            logger.error(
                "sender.delete_brake_tripped",
                cycle_id=cycle_id,
                removed=len(removed_entries),
                baseline=baseline,
                fraction=round(fraction, 4),
                limit=settings.max_delete_fraction,
            )
            # No archive, no cursor advance. The next cycle re-runs the diff
            # against the same baseline, so a transient short read recovers
            # by itself and a real mass deletion keeps tripping until an
            # operator raises the limit deliberately.
            _record_cycle(
                ledger_path,
                status="brake-refused",
                started=started,
                cycle_id=cycle_id,
                prev_cycle_id=prev_cycle_id,
                snapshot_count=count,
                repo_count=len(snapshot_repo_counts),
                added=len(new_entries),
                removed=len(removed_entries),
                note=(
                    f"{len(removed_entries)} of {baseline} "
                    f"({fraction:.1%}) exceeds max_delete_fraction "
                    f"{settings.max_delete_fraction}"
                ),
            )
            return

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
        _record_cycle(
            ledger_path,
            status="no-changes",
            started=started,
            cycle_id=cycle_id,
            prev_cycle_id=prev_cycle_id,
            snapshot_count=count,
            repo_count=len(snapshot_repo_counts),
        )
        return

    # Synthesise the metadata tree the receiver's import needs, covering only
    # the artifacts this cycle ships. This is what used to arrive as a full
    # system export of the whole instance.
    synth_root = exports_dir / cycle_id
    if synth_root.exists():
        shutil.rmtree(synth_root)
    synth_root.mkdir(parents=True, exist_ok=True)
    meta_rows = aql.fetch_metadata(client, new_entries)
    written, unresolved = metadata_synth.build_tree(
        synth_root, new_entries, meta_rows
    )
    if unresolved:
        # The artifact was in the enumeration but its metadata query returned
        # nothing, so it was almost certainly deleted between the two calls.
        # Drop it from this cycle; the next enumeration will not list it.
        gone = {(e.repo_key, e.repo_path) for e in unresolved}
        new_entries = [e for e in new_entries if (e.repo_key, e.repo_path) not in gone]
        logger.warning(
            "sender.metadata_unresolved",
            cycle_id=cycle_id,
            count=len(unresolved),
        )
    logger.info(
        "sender.metadata_synthesised",
        cycle_id=cycle_id,
        entries=written,
    )

    ok, deferred = _emit_archives(
        settings,
        store=store,
        cycle_id=cycle_id,
        prev_cycle_id=prev_cycle_id,
        export_contents=synth_root,
        new_entries=new_entries,
        removed_entries=removed_entries,
    )
    if not ok:
        # Backpressure or other abort. Do not advance the cursor: next
        # cycle re-runs the diff against the same baseline.
        _record_cycle(
            ledger_path,
            status="backpressure",
            started=started,
            cycle_id=cycle_id,
            prev_cycle_id=prev_cycle_id,
            snapshot_count=count,
            repo_count=len(snapshot_repo_counts),
            added=len(new_entries),
            removed=len(removed_entries),
            note="spool free space below the projected chunk size",
        )
        return

    if deferred:
        # Some blobs were not in the store when we went to read them. Strip
        # them from the snapshot before it becomes the baseline, so the next
        # cycle sees them as added again and retries. Without this the cursor
        # would advance past artifacts whose bytes never shipped and they
        # would be missed permanently.
        dropped = _defer_entries(snapshot_path, deferred)
        logger.warning(
            "sender.entries_deferred",
            cycle_id=cycle_id,
            blob_count=len(deferred),
            entry_count=dropped,
        )

    archives = sorted(settings.spool_dir.glob(f"{cycle_id}*.tar.zst"))
    _record_cycle(
        ledger_path,
        status="ok",
        started=started,
        cycle_id=cycle_id,
        prev_cycle_id=prev_cycle_id,
        snapshot_count=count,
        repo_count=len(snapshot_repo_counts),
        added=len(new_entries),
        removed=len(removed_entries),
        repos_added=dict(Counter(e.repo_key for e in new_entries)),
        repos_removed=dict(Counter(e.repo_key for e in removed_entries)),
        archives=[p.name for p in archives],
        archive_bytes=sum(p.stat().st_size for p in archives),
        chunk_total=len(archives),
        deferred_blobs=len(deferred),
        deferred_sha1s=sorted(deferred)[:_LEDGER_DEFERRED_SAMPLE],
    )

    _advance_cursor(cursor_path, cycle_id)
    _prune_history(
        settings,
        snapshots_dir=snapshots_dir,
        exports_dir=exports_dir,
        ledger_path=ledger_path,
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
    store: "binarystore.BlobStore",
    cycle_id: str,
    prev_cycle_id: str | None,
    export_contents: Path,
    new_entries: list,
    removed_entries: list,
) -> tuple[bool, set[str]]:
    """Write all archives for one logical cycle.

    Returns (ok, deferred_sha1s). ``ok`` is True when every chunk was written
    and finalised, False if backpressure aborted the cycle; on failure any
    partial chunks already landed in spool for this parent are removed so the
    receiver never sees an incomplete chunk set. ``deferred_sha1s`` are blobs
    the store could not serve, unioned across chunks.
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
    deferred: set[str] = set()
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
            return False, deferred

        chunk_archive_name, chunk_cycle_id = _chunk_names(cycle_id, i, chunk_total)
        archive_path, chunk_deferred = archive.build(
            spool_dir=settings.spool_dir,
            cycle_id=chunk_cycle_id,
            prev_cycle_id=prev_cycle_id,
            source_instance=settings.instance_name,
            export_root=export_contents,
            entries=group,
            store=store,
            removed=removed_entries if is_final else [],
            archive_name=chunk_archive_name,
            parent_cycle_id=cycle_id,
            chunk_seq=i,
            chunk_total=chunk_total,
            include_metadata=is_final,
            skip_blob_sha1s=seen_sha1s,
        )
        deferred |= chunk_deferred
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
    return True, deferred


def _defer_entries(snapshot_path: Path, deferred_sha1s: set[str]) -> int:
    """Rewrite a snapshot without the given sha1s. Returns entries removed.

    The snapshot is this cycle's record of what the source holds, and it
    becomes the baseline the next diff runs against. Dropping an entry here is
    what makes the next cycle re-detect it as added. Written through a temp
    file and renamed so a crash mid-rewrite cannot leave a truncated baseline.
    """
    if not deferred_sha1s or not snapshot_path.is_file():
        return 0
    tmp = snapshot_path.with_suffix(snapshot_path.suffix + f".tmp-{os.getpid()}")
    removed = 0
    with open(snapshot_path, encoding="utf-8") as src, open(
        tmp, "w", encoding="utf-8"
    ) as dst:
        for line in src:
            if not line.strip():
                continue
            if json.loads(line)["sha1"] in deferred_sha1s:
                removed += 1
                continue
            dst.write(line)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, snapshot_path)
    return removed


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


# How many rows of state/cycles.jsonl to retain, and the slack allowed above
# that before a trim actually rewrites the file. The ledger is an operator
# breadcrumb trail, not load-bearing state (unlike the receiver's
# processed.jsonl, which drives idempotency), so it is trimmed by count rather
# than kept forever. At a 30-second cycle this is about two days of history.
_LEDGER_KEEP_ROWS = 5000
_LEDGER_TRIM_SLACK = 1000

# Deferred sha1s recorded per row. A handful is enough to start looking; the
# count beside them is the number that matters.
_LEDGER_DEFERRED_SAMPLE = 20


def _record_cycle(
    ledger_path: Path | None,
    *,
    status: str,
    started: float | None = None,
    **fields,
) -> None:
    """Append one row to the sender's cycle ledger.

    The receiver has ``processed.jsonl`` and the sender had nothing
    equivalent: the cursor records only the last cycle that succeeded, so
    everything else (a cycle the brake refused, a cycle skipped because the
    transport is behind, blobs deferred because they were not in the store
    yet) lived in the log and nowhere else. That made history unanswerable
    from state alone once the log had rotated.

    Writing is best-effort. A cycle that did real work must not be reported
    as failed because its breadcrumb could not be written.
    """
    if ledger_path is None:
        return
    row = {"status": status, "at": int(time.time())}
    if started is not None:
        row["duration_ms"] = int((time.time() - started) * 1000)
    row.update(
        {k: _ledger_value(v) for k, v in fields.items() if v not in (None, [], {}, 0)}
    )
    try:
        state.append_jsonl(ledger_path, row)
        _trim_ledger(ledger_path)
    except OSError as exc:
        logger.warning("sender.ledger_write_failed", error=str(exc))


# Longest string kept in a ledger field. An exception message is the only
# thing that gets near it, and a ledger row is a breadcrumb rather than a log
# record: the full text was already logged when it happened.
_LEDGER_FIELD_CHARS = 300


def _ledger_value(value):
    """Flatten a value into something a one-line-per-cycle ledger can hold.

    Exception messages are the reason this exists. httpx renders a failed
    status over two lines with a documentation URL on the second, so a raw
    error string turns a ledger row into something that cannot be shown in a
    table and is awkward to grep.
    """
    if isinstance(value, str):
        flattened = " ".join(value.split())
        if len(flattened) > _LEDGER_FIELD_CHARS:
            return flattened[: _LEDGER_FIELD_CHARS - 1] + "\u2026"
        return flattened
    return value


def _trim_ledger(ledger_path: Path) -> None:
    """Keep the newest ``_LEDGER_KEEP_ROWS`` rows, rewritten atomically.

    Only rewrites once the file has grown a whole slack allowance past the
    target, so the common case is a plain append.
    """
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= _LEDGER_KEEP_ROWS + _LEDGER_TRIM_SLACK:
        return
    keep = lines[-_LEDGER_KEEP_ROWS:]
    tmp = ledger_path.with_suffix(ledger_path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.replace(tmp, ledger_path)


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
    ledger_path: Path | None = None,
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

    if ledger_path is not None and ledger_path.exists():
        _trim_ledger(ledger_path)


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


def _count_snapshot_entries(snapshot_path: Path) -> int:
    """Count entries in a JSONL snapshot. Returns 0 when unreadable.

    Used as the denominator for the deletion brake, so it counts lines
    rather than parsing: a malformed line would still represent an entry
    that existed in the baseline.
    """
    try:
        with snapshot_path.open() as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


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

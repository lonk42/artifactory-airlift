from __future__ import annotations

import shutil
import time
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
    snapshot_path = snapshots_dir / f"{cycle_id}.jsonl"
    count = export_unpacker.write_snapshot(export_contents, snapshot_path)
    logger.info("sender.snapshot_written", cycle_id=cycle_id, count=count)

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
    logger.info(
        "sender.diff_computed",
        cycle_id=cycle_id,
        prev_cycle_id=prev_cycle_id,
        added=len(new_entries),
    )

    if not new_entries and prev_cycle_id is not None:
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

    archive_path = archive.build(
        spool_dir=settings.spool_dir,
        cycle_id=cycle_id,
        prev_cycle_id=prev_cycle_id,
        source_instance=settings.instance_name,
        export_root=export_contents,
        entries=new_entries,
        filestore_root=settings.filestore_root,
    )
    logger.info("sender.archive_finalized", path=str(archive_path))

    _advance_cursor(cursor_path, cycle_id)
    _prune_history(
        settings,
        snapshots_dir=snapshots_dir,
        exports_dir=exports_dir,
    )


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
) -> None:
    # Snapshots: GFS retention. Each tier keeps the newest snapshot per
    # non-empty bucket within its wall-clock window from now. The just-
    # written snapshot always wins its current bucket in any non-zero
    # tier, so the diff baseline for the next cycle is preserved.
    snapshot_paths = list(snapshots_dir.glob("*.jsonl"))
    keepers = state.gfs_keepers(
        snapshot_paths,
        hours=settings.snapshot_retention_hours,
        days=settings.snapshot_retention_days,
        months=settings.snapshot_retention_months,
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

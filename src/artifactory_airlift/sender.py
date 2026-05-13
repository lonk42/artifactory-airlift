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

    archive_path = archive.build(
        spool_dir=settings.spool_dir,
        cycle_id=cycle_id,
        prev_cycle_id=prev_cycle_id,
        source_instance=settings.instance_name,
        export_root=export_contents,
        entries=new_entries,
        filestore_root=settings.filestore_root,
        removed=removed_entries,
    )
    archive_size = archive_path.stat().st_size
    archive_repos = sorted(
        {e.repo_key for e in new_entries} | {e.repo_key for e in removed_entries}
    )
    logger.info(
        "sender.archive_finalized",
        cycle_id=cycle_id,
        path=str(archive_path),
        size_bytes=archive_size,
        size_human=log.human_bytes(archive_size),
        blob_count=len(new_entries),
        repo_count=len(archive_repos),
        repos=archive_repos,
    )

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

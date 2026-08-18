"""Commands that change something: ad-hoc exports, cursor edits, replays.

Concurrency, and why it is not what you would expect
----------------------------------------------------
The daemon takes ``state/<mode>.lock`` around its whole run loop, not around
one cycle, so it holds that lock for as long as the container lives. A CLI
command cannot take it without failing every time the sidecar is healthy.

So the locking here is between CLI invocations (``state/cli.lock``), and the
daemon is handled by telling the truth about it: a mutating command warns
when the daemon is live and asks for confirmation, because an in-flight cycle
can overwrite a cursor edit or pick up an archive mid-write. Retrying is
always safe; the operations below are idempotent by construction.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import aql, archive as archive_mod, binarystore, diff, metadata_synth, sender, state
from ..artifactory_client import ArtifactoryClient
from ..config import Settings
from ..export_unpacker import ArtifactEntry
from . import common, views
from .common import CLIError

# Time fields an ad-hoc window can select on. Verified against Artifactory
# 7.146.10: all three accept "$gt"/"$lt" with an ISO 8601 instant, and a
# range must be written as an "$and" of two clauses because a JSON object
# cannot carry the same field twice.
TIME_FIELDS = ("created", "modified", "updated")


@contextlib.contextmanager
def cli_lock(settings: Settings) -> Iterator[None]:
    """Serialise CLI mutations against each other."""
    try:
        with state.file_lock(settings.state_dir / "cli.lock"):
            yield
    except RuntimeError as exc:
        raise CLIError(f"another airlift command is running ({exc})") from exc


def confirm(settings: Settings, what: str, *, assume_yes: bool) -> None:
    """Warn about a live daemon and confirm, unless --yes was given."""
    running = common.daemon_running(settings)
    if running:
        print(
            f"The {settings.mode} daemon is running. {what}\n"
            "A cycle already in flight can overwrite this; re-run the command "
            "if it does not take effect."
        )
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise CLIError("not a terminal, so pass --yes to confirm")
    if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        raise CLIError("cancelled", code=0)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


# -- selection --------------------------------------------------------------


def _time_criteria(
    *, time_field: str, since: float | None, until: float | None
) -> list[dict[str, Any]]:
    if time_field not in TIME_FIELDS:
        raise CLIError(f"--time-field must be one of {', '.join(TIME_FIELDS)}")
    clauses: list[dict[str, Any]] = []
    if since is not None:
        clauses.append({time_field: {"$gt": _iso(since)}})
    if until is not None:
        clauses.append({time_field: {"$lt": _iso(until)}})
    return clauses


def select_entries(
    settings: Settings,
    client: ArtifactoryClient,
    *,
    since: float | None = None,
    until: float | None = None,
    time_field: str = "modified",
    repos: list[str] | None = None,
    artifacts: list[str] | None = None,
    from_snapshot: str | None = None,
    to_snapshot: str | None = None,
    everything: bool = False,
) -> tuple[list[ArtifactEntry], dict[str, Any]]:
    """Resolve what an ad-hoc export should ship, plus a description of how.

    Snapshot-based selection reads retained baselines and never queries the
    source, which is what makes it usable for gap recovery: the archive for a
    cycle can be rebuilt from the pair of snapshots that bracket it even if
    the source has moved on since.
    """
    if from_snapshot:
        prev = common.snapshot_path(settings, from_snapshot)
        if to_snapshot:
            current = common.snapshot_path(settings, to_snapshot)
            entries = list(diff.added(prev, current))
            how = {
                "selector": "snapshot-delta",
                "from": prev.name,
                "to": current.name,
            }
        else:
            entries = [
                ArtifactEntry.from_json(line)
                for line in prev.read_text().splitlines()
                if line.strip()
            ]
            how = {"selector": "snapshot", "from": prev.name}
        if repos:
            entries = [e for e in entries if e.repo_key in set(repos)]
            how["repos"] = repos
        return entries, how

    included = set(repos) if repos else set(settings.included_repos)
    excluded = set(settings.excluded_repos)
    extra = _time_criteria(time_field=time_field, since=since, until=until)
    entries = list(
        aql.iter_artifacts(
            client,
            included_repos=included or None,
            excluded_repos=excluded or None,
            extra_criteria=extra or None,
        )
    )
    how = {
        "selector": "query",
        "repos": sorted(included) if included else "all",
        "since": _iso(since) if since else None,
        "until": _iso(until) if until else None,
        "time_field": time_field if (since or until) else None,
    }
    if artifacts:
        for a in artifacts:
            if "/" not in a:
                raise CLIError(
                    f"--artifact takes <repo>/<path>, so {a!r} is missing the path"
                )
        wanted = set(artifacts)
        entries = [e for e in entries if f"{e.repo_key}/{e.repo_path}" in wanted]
        how["artifacts"] = sorted(wanted)
        missing = wanted - {f"{e.repo_key}/{e.repo_path}" for e in entries}
        if missing:
            how["not_found"] = sorted(missing)
    elif not (since or until or everything):
        raise CLIError(
            "give a selector: --since/--until, --repo with --all, "
            "--artifact, or --from-snapshot"
        )
    return entries, how


# -- export -----------------------------------------------------------------


def export(
    settings: Settings,
    *,
    out_dir: Path | None,
    label: str,
    dry_run: bool,
    max_archive_bytes: int | None,
    **selection,
) -> dict[str, Any]:
    """Build archives for an arbitrary selection, outside the cycle machinery.

    The cursor and the snapshots are untouched, so an ad-hoc export never
    changes what the next ordinary cycle does. ``prev_cycle_id`` is left
    unset: the receiver warns about a gap when an archive names a predecessor
    it has not seen, and an ad-hoc archive genuinely has no predecessor.
    """
    with ArtifactoryClient.from_settings(settings) as client:
        entries, how = select_entries(settings, client, **selection)

        target = out_dir or settings.spool_dir
        update: dict[str, Any] = {"spool_dir": target}
        if target != settings.spool_dir:
            # spool_min_free_bytes exists to stop a cycle filling the spool
            # the transport depends on. Writing somewhere the operator named
            # explicitly is not that, and applying a 2 GiB floor to a scratch
            # directory refuses a two-kilobyte export for no reason. The
            # projection headroom inside _emit_archives still applies, so the
            # write is not unguarded.
            update["spool_min_free_bytes"] = 0
        if max_archive_bytes is not None:
            update["max_archive_bytes"] = max_archive_bytes
        emit_settings = settings.model_copy(update=update)

        raw_bytes = sum(e.size for e in entries)
        chunks = sender._group_into_chunks(
            sorted(entries, key=lambda e: (e.repo_key, e.repo_path)),
            emit_settings.max_archive_bytes,
        )
        plan = {
            "selection": how,
            "entries": len(entries),
            "raw_bytes": raw_bytes,
            "repos": dict(Counter(e.repo_key for e in entries)),
            "chunks": max(len(chunks), 1),
            "out_dir": str(target),
            "writes_to_spool": target == settings.spool_dir,
        }
        if dry_run:
            plan["dry_run"] = True
            return plan
        if not entries:
            raise CLIError("selection is empty, so there is nothing to export")

        cycle_id = f"{int(time.time()):010d}-{label}"
        plan["cycle_id"] = cycle_id
        target.mkdir(parents=True, exist_ok=True)

        tree = Path(tempfile.mkdtemp(prefix="airlift-export-", dir=settings.state_dir))
        store = binarystore.resolve(settings)
        try:
            rows = aql.fetch_metadata(client, entries)
            written, unresolved = metadata_synth.build_tree(tree, entries, rows)
            if unresolved:
                gone = {(e.repo_key, e.repo_path) for e in unresolved}
                entries = [
                    e for e in entries if (e.repo_key, e.repo_path) not in gone
                ]
                plan["unresolved"] = len(unresolved)
            plan["metadata_entries"] = written

            ok, deferred = sender._emit_archives(
                emit_settings,
                store=store,
                cycle_id=cycle_id,
                prev_cycle_id=None,
                export_contents=tree,
                new_entries=entries,
                removed_entries=[],
            )
        finally:
            store.close()
            shutil.rmtree(tree, ignore_errors=True)

        if not ok:
            raise CLIError(
                "aborted: free space in "
                f"{target} is below spool_min_free_bytes plus the projected "
                "archive size. Free space, lower --max-archive-bytes, or "
                "write elsewhere with --out.",
                code=2,
            )
        archives = sorted(target.glob(f"{cycle_id}*.tar.zst"))
        plan["archives"] = [p.name for p in archives]
        plan["archive_bytes"] = sum(p.stat().st_size for p in archives)
        plan["deferred_blobs"] = len(deferred)
        return plan


# -- plan -------------------------------------------------------------------


def plan_next_cycle(settings: Settings) -> dict[str, Any]:
    """What the next ordinary cycle would do, without doing any of it.

    Enumerates the source and diffs against the current baseline exactly as a
    cycle would, then throws the result away. Nothing is written outside a
    temporary directory, so this is safe to run beside a live daemon.
    """
    pending = sorted(settings.spool_dir.glob("*.tar.zst"))
    cursor = common.read_cursor(settings)
    baseline_id = cursor.get("last_cycle_id")
    baseline = (
        common.snapshots_dir(settings) / f"{baseline_id}.jsonl" if baseline_id else None
    )
    if baseline is not None and not baseline.is_file():
        baseline = None

    with ArtifactoryClient.from_settings(settings) as client:
        excluded = sender._resolve_excluded_repos(client, settings)
        included = set(settings.included_repos)
        work = Path(tempfile.mkdtemp(prefix="airlift-plan-"))
        try:
            snapshot = work / "current.jsonl"
            count = aql.write_snapshot(
                client, snapshot, excluded_repos=excluded, included_repos=included
            )
            added = list(diff.added(baseline, snapshot))
            removed = (
                list(diff.removed(baseline, snapshot))
                if baseline is not None and settings.propagate_deletes
                else []
            )
            baseline_count = common.count_lines(baseline) if baseline else 0
        finally:
            shutil.rmtree(work, ignore_errors=True)

    fraction = (len(removed) / baseline_count) if baseline_count else 0.0
    raw_bytes = sum(e.size for e in added)
    chunks = sender._group_into_chunks(
        sorted(added, key=lambda e: (e.repo_key, e.repo_path)),
        settings.max_archive_bytes,
    )
    would_skip = bool(pending)
    would_brake = bool(removed) and fraction > settings.max_delete_fraction
    return {
        "baseline": baseline_id,
        "baseline_entries": baseline_count,
        "cold_start": baseline is None,
        "source_entries": count,
        "added": len(added),
        "removed": len(removed),
        "raw_bytes": raw_bytes,
        "repos_added": dict(Counter(e.repo_key for e in added)),
        "repos_removed": dict(Counter(e.repo_key for e in removed)),
        "chunks": max(len(chunks), 1),
        "delete_fraction": round(fraction, 4),
        "max_delete_fraction": settings.max_delete_fraction,
        "pending_archives": [p.name for p in pending],
        "outcome": (
            "skipped-pending"
            if would_skip
            else "brake-refused"
            if would_brake
            else "no-changes"
            if not added and not removed
            else "ok"
        ),
    }


# -- cursor -----------------------------------------------------------------


def cursor_show(settings: Settings) -> dict[str, Any]:
    cursor = common.read_cursor(settings)
    baseline_id = cursor.get("last_cycle_id")
    baseline = (
        common.snapshots_dir(settings) / f"{baseline_id}.jsonl" if baseline_id else None
    )
    return {
        "path": str(common.cursor_path(settings)),
        "present": common.cursor_path(settings).exists(),
        "last_cycle_id": baseline_id,
        "last_success_at": cursor.get("last_success_at"),
        "baseline_snapshot_present": bool(baseline and baseline.is_file()),
        "baseline_entries": common.count_lines(baseline) if baseline and baseline.is_file() else 0,
    }


def cursor_clear(settings: Settings) -> dict[str, Any]:
    """Drop the cursor so the next cycle is a cold start.

    The documented way to change sync scope. A narrowed scope removes
    everything outside it from the snapshot, the diff reads that as deleting
    it from the destination, and the brake refuses the cycle. A cold start
    emits no removals at all and re-adds everything in the new scope, which
    is cheap: the receiver already holds the blobs and the import is
    idempotent.
    """
    path = common.cursor_path(settings)
    before = common.read_cursor(settings)
    existed = path.exists()
    if existed:
        path.unlink()
    return {"cleared": existed, "was": before.get("last_cycle_id"), "path": str(path)}


def cursor_set(settings: Settings, cycle_id: str) -> dict[str, Any]:
    """Rewind (or advance) the baseline to a retained snapshot.

    Refuses a cycle id with no snapshot behind it: a cursor pointing at a
    snapshot that is not there is treated as a cold start on the next cycle,
    which is the opposite of a deliberate rewind.
    """
    snapshot = common.snapshots_dir(settings) / f"{cycle_id}.jsonl"
    if not snapshot.is_file():
        raise CLIError(
            f"no snapshot {snapshot.name} is retained, so the baseline cannot "
            f"be set to {cycle_id}"
        )
    was = common.read_cursor(settings).get("last_cycle_id")
    state.write_json_atomic(
        common.cursor_path(settings),
        {"last_cycle_id": cycle_id, "last_success_at": int(time.time())},
    )
    return {"was": was, "now": cycle_id, "entries": common.count_lines(snapshot)}


# -- receiver-side ----------------------------------------------------------


def ledger_forget(settings: Settings, cycle_id: str) -> dict[str, Any]:
    """Remove a cycle's rows from the receiver ledger so it can be reprocessed.

    The documented recovery used to be deleting the whole ledger, which makes
    every archive still on disk eligible again. Reprocessing is safe (blobs
    are content-addressed and the import is idempotent) but it is a much
    bigger hammer than the situation usually needs.
    """
    path = common.receiver_ledger_path(settings)
    rows = common.read_ledger(path)
    keep = [
        r
        for r in rows
        if r.get("cycle_id") != cycle_id and r.get("parent_cycle_id") != cycle_id
    ]
    dropped = len(rows) - len(keep)
    if not dropped:
        raise CLIError(f"no ledger rows for {cycle_id!r}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in keep))
    tmp.replace(path)
    return {"cycle_id": cycle_id, "rows_dropped": dropped, "rows_kept": len(keep)}


def replay(settings: Settings, cycle_id: str) -> dict[str, Any]:
    """Move a processed archive back into the spool and forget its ledger rows.

    Deliberately does not import anything itself. The receiver owns the
    import path, and a second process running it concurrently is exactly the
    race that ordering guard exists to prevent, so this stages the work and
    lets the next receiver cycle do it.
    """
    done = common.done_dir(settings)
    archives = sorted(done.glob(f"{cycle_id}*.tar.zst")) if done.is_dir() else []
    if not archives:
        raise CLIError(f"no archive for {cycle_id!r} in {done}")

    moved = []
    for path in archives:
        target = settings.spool_dir / path.name
        if target.exists():
            raise CLIError(f"{target} already exists; it is queued already")
        path.replace(target)
        moved.append(target.name)

    rows = common.read_ledger(common.receiver_ledger_path(settings))
    names = {p.removesuffix(".tar.zst") for p in moved}
    keep = [r for r in rows if r.get("cycle_id") not in names]
    dropped = len(rows) - len(keep)
    if dropped:
        path = common.receiver_ledger_path(settings)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in keep))
        tmp.replace(path)

    return {
        "cycle_id": cycle_id,
        "archives": moved,
        "ledger_rows_dropped": dropped,
        "daemon_running": common.daemon_running(settings),
    }


def import_tree(settings: Settings, path: Path) -> dict[str, Any]:
    """Call /api/import/repositories against a tree, and report per-repo lines.

    Point it at the directory that *contains* per-repository directories. The
    endpoint returns 200 even when individual repositories fail, so the body
    is parsed the way the receiver parses it, including dropping the notices
    that only mean "this tree did not carry that repository".
    """
    if not path.is_dir():
        raise CLIError(f"{path} is not a directory")
    shipped = {p.name for p in path.iterdir() if p.is_dir()}
    if not shipped:
        raise CLIError(
            f"{path} holds no repository directories; point at the directory "
            "that contains them (the 'repositories' directory of a tree)"
        )
    from ..receiver import _is_absent_repo_notice

    with ArtifactoryClient.from_settings(settings) as client:
        body = client.import_repositories(path)

    failures, absent = [], 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("500 :", "400 :", "404 :", "Error")):
            continue
        if _is_absent_repo_notice(stripped, shipped):
            absent += 1
            continue
        failures.append(stripped)
    return {
        "path": str(path),
        "repositories": sorted(shipped),
        "failures": failures,
        "absent_repo_notices": absent,
        "status": "ok" if not failures else "partial",
    }

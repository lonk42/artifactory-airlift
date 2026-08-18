"""Data gathering for every CLI command.

Each function returns plain dictionaries and lists, with no printing and no
``sys.exit``. That keeps the commands testable without a terminal and leaves
a web UI free to call them directly.

Network and blob-store access is opened lazily and per command, never held
open across one, because the CLI runs beside a daemon that already holds
long-lived clients.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

from tenacity import stop_after_attempt

from .. import archive as archive_mod
from .. import binarystore, diff, log
from ..artifactory_client import ArtifactoryClient
from ..config import Settings
from . import common
from .common import CLIError

# Ledger statuses that mean "this cycle did nothing", hidden from history
# listings unless asked for. A stalled transport writes one of these every
# cycle_seconds, which would otherwise bury the cycles that did work.
QUIET_STATUSES = frozenset({"no-changes", "skipped-pending", "ping-failed"})


# -- status -----------------------------------------------------------------


def status(settings: Settings, *, offline: bool = False) -> dict[str, Any]:
    from .. import __version__

    out: dict[str, Any] = {
        "version": __version__,
        "mode": settings.mode,
        "instance_name": settings.instance_name,
        "artifactory_url": settings.artifactory_url,
        "cycle_seconds": settings.cycle_seconds,
        "daemon_running": common.daemon_running(settings),
        "state_dir": str(settings.state_dir),
        "spool_dir": str(settings.spool_dir),
        "artifactory": {"checked": not offline},
        "binarystore": {"checked": not offline},
    }

    if not offline:
        out["artifactory"] = _probe_artifactory(settings)
        out["binarystore"] = _probe_binarystore(settings)

    out["spool"] = spool_summary(settings)
    if settings.mode == "sender":
        out["sender"] = _sender_status(settings)
    else:
        out["receiver"] = _receiver_status(settings)
    return out


def call_once(client: ArtifactoryClient, method: str, *args):
    """Call a client method with its retry chain reduced to one attempt.

    The daemon's five-attempt exponential backoff is right for a cycle that
    would otherwise be lost to a 503 during Artifactory's boot, and wrong for
    a command whose entire job is to report whether the instance answers:
    fifteen seconds of silence before "connection refused" is a worse answer
    than an immediate one.
    """
    fn = getattr(type(client), method).retry_with(stop=stop_after_attempt(1))
    return fn(client, *args)


def _probe_artifactory(settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {"checked": True, "url": settings.artifactory_url}
    try:
        with ArtifactoryClient.from_settings(settings) as client:
            started = time.monotonic()
            result["reachable"] = call_once(client, "ping")
            result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except Exception as exc:
        result["reachable"] = False
        result["error"] = str(exc)
    return result


def _probe_binarystore(settings: Settings) -> dict[str, Any]:
    """Resolve the backend without probing it.

    Resolution is the part worth reporting: it is where a wrong provider or a
    missing binarystore.xml shows up. The write probe is a separate, explicit
    act (``doctor --write-probe``) because the sender is expected to work with
    a read-only credential on the source.
    """
    result: dict[str, Any] = {"checked": True}
    store = None
    try:
        store = binarystore.resolve(settings)
        result["backend"] = store.kind
        result["detail"] = store.describe()
        result["prefix_override"] = settings.binarystore_prefix or None
    except Exception as exc:
        result["backend"] = None
        result["error"] = str(exc)
    finally:
        if store is not None:
            store.close()
    return result


def _sender_status(settings: Settings) -> dict[str, Any]:
    cursor = common.read_cursor(settings)
    snaps = sorted(common.snapshots_dir(settings).glob("*.jsonl"))
    rows = common.read_ledger(common.sender_ledger_path(settings))
    working = [r for r in rows if r.get("status") not in QUIET_STATUSES]

    out: dict[str, Any] = {
        "cursor_cycle_id": cursor.get("last_cycle_id"),
        "last_success_at": cursor.get("last_success_at"),
        "snapshot_count": len(snaps),
        "newest_snapshot": snaps[-1].name.removesuffix(".jsonl") if snaps else None,
        "newest_snapshot_entries": common.count_lines(snaps[-1]) if snaps else 0,
        "synthesised_trees": _count_dirs(common.exports_dir(settings)),
        "ledger_rows": len(rows),
        "last_cycle": rows[-1] if rows else None,
        "last_working_cycle": working[-1] if working else None,
        "deferred_blobs": rows[-1].get("deferred_blobs", 0) if rows else 0,
        "included_repos": list(settings.included_repos),
        "max_delete_fraction": settings.max_delete_fraction,
        "propagate_deletes": settings.propagate_deletes,
    }
    recent = rows[-20:]
    out["recent_statuses"] = dict(Counter(r.get("status", "?") for r in recent))
    return out


def _receiver_status(settings: Settings) -> dict[str, Any]:
    rows = common.read_ledger(common.receiver_ledger_path(settings))
    done = common.done_dir(settings)
    out: dict[str, Any] = {
        "ledger_rows": len(rows),
        "last_cycle": rows[-1] if rows else None,
        "done_archives": len(list(done.glob("*.tar.zst"))) if done.is_dir() else 0,
        "recent_statuses": dict(Counter(r.get("status", "?") for r in rows[-20:])),
    }
    incomplete = _incomplete_chunk_sets(rows)
    if incomplete:
        out["incomplete_chunk_sets"] = incomplete
    return out


def _incomplete_chunk_sets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chunked cycles whose commit chunk has not been processed yet.

    A final chunk parks in spool until every earlier chunk has staged its
    blobs, so a parent with staged chunks and no terminal row is the shape of
    a transport that delivered a chunk set out of order or dropped one.
    """
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        parent = row.get("parent_cycle_id")
        total = row.get("chunk_total")
        seq = row.get("chunk_seq")
        if not isinstance(parent, str) or not isinstance(total, int):
            continue
        rec = seen.setdefault(parent, {"parent_cycle_id": parent, "chunk_total": total,
                                       "have": set(), "committed": False})
        if isinstance(seq, int):
            rec["have"].add(seq)
        if row.get("status") in ("ok", "partial"):
            rec["committed"] = True
    out = []
    for rec in seen.values():
        if rec["committed"]:
            continue
        missing = sorted(set(range(1, rec["chunk_total"] + 1)) - rec["have"])
        out.append(
            {
                "parent_cycle_id": rec["parent_cycle_id"],
                "chunk_total": rec["chunk_total"],
                "missing": missing,
            }
        )
    return out


def spool_summary(settings: Settings) -> dict[str, Any]:
    pending = sorted(settings.spool_dir.glob("*.tar.zst"))
    partials = sorted(settings.spool_dir.glob("*.tar.zst.partial"))
    try:
        usage = shutil.disk_usage(settings.spool_dir)
        free, total = usage.free, usage.total
    except OSError:
        free = total = 0
    oldest = min((p.stat().st_mtime for p in pending), default=None)
    return {
        "pending_count": len(pending),
        "pending_bytes": sum(p.stat().st_size for p in pending),
        "pending": [p.name for p in pending],
        "partials": len(partials),
        "oldest_pending_at": int(oldest) if oldest else None,
        "free_bytes": free,
        "total_bytes": total,
        "min_free_bytes": settings.spool_min_free_bytes,
        "free_below_minimum": bool(free and free < settings.spool_min_free_bytes),
    }


def _count_dirs(path: Path) -> int:
    try:
        return sum(1 for p in path.iterdir() if p.is_dir())
    except OSError:
        return 0


# -- config -----------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Coerce a settings value into something JSON can carry.

    Several settings are ``Path`` objects. The text renderer would stringify
    them anyway, but a caller serialising a view directly (``--json``, or an
    HTTP API later) must not have to supply its own encoder.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def effective_config(
    settings: Settings,
    file_values: dict[str, Any],
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Every setting with its resolved value and where that value came from.

    The origin column is the point of this command. Env vars outrank the
    mounted ConfigMap, and the Helm chart renders every key into that
    ConfigMap whether or not the operator set it, so "the file says X" and
    "airlift is using X" are different statements. A file value that env has
    overridden is reported as masked rather than silently dropped.
    """
    fields = []
    for name in sorted(type(settings).model_fields):
        value = getattr(settings, name)
        env_raw = common.env_value(name)
        in_file = name in file_values
        if env_raw is not None:
            origin = "env"
        elif in_file:
            origin = "file"
        else:
            origin = "default"
        entry = {
            "name": name,
            "value": _jsonable(common.redact(name, value)),
            "origin": origin,
            "env_var": common.env_var_for(name),
        }
        if origin == "env" and in_file:
            entry["masked_file_value"] = _jsonable(
                common.redact(name, file_values[name])
            )
        fields.append(entry)
    return {
        "config_path": str(config_path or common.DEFAULT_CONFIG_PATH),
        "config_present": bool(file_values),
        "fields": fields,
    }


# -- doctor -----------------------------------------------------------------


def doctor(settings: Settings, *, write_probe: bool = False) -> dict[str, Any]:
    """Active checks, each returning ok/failed plus a one-line detail."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str, *, fatal: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fatal": fatal})

    # State and spool directories.
    for label, path in (("state dir", settings.state_dir), ("spool dir", settings.spool_dir)):
        if not path.is_dir():
            add(label, False, f"{path} does not exist")
        else:
            probe = path / f".airlift-cli-probe-{int(time.time())}"
            try:
                probe.touch()
                probe.unlink()
                add(label, True, f"{path} is writable")
            except OSError as exc:
                add(label, False, f"{path} is not writable: {exc}")

    spool = spool_summary(settings)
    if spool["free_below_minimum"]:
        add(
            "spool free space",
            False,
            f"{log.human_bytes(spool['free_bytes'])} free, below "
            f"spool_min_free_bytes {log.human_bytes(spool['min_free_bytes'])}",
        )
    else:
        add(
            "spool free space",
            True,
            f"{log.human_bytes(spool['free_bytes'])} free",
        )

    # The import endpoint rejects any path under Artifactory's own data
    # directory, which is why the receiver extracts under the state PVC.
    if settings.mode == "receiver" and str(settings.state_dir).startswith(
        "/var/opt/jfrog/artifactory"
    ):
        add(
            "import path",
            False,
            f"state_dir {settings.state_dir} is under Artifactory's data directory; "
            "/api/import/repositories rejects those paths",
        )
    else:
        add("import path", True, "state_dir is outside Artifactory's data directory")

    # Artifactory.
    art = _probe_artifactory(settings)
    add(
        "artifactory ping",
        bool(art.get("reachable")),
        art.get("error") or f"{settings.artifactory_url} responded in "
        f"{art.get('latency_ms', '?')}ms",
    )
    if art.get("reachable"):
        try:
            with ArtifactoryClient.from_settings(settings) as client:
                repos = call_once(client, "list_repositories")
            add("authentication", True, f"listed {len(repos)} repositories")
        except Exception as exc:
            add("authentication", False, f"/api/repositories failed: {exc}")

    # Binarystore.
    store = None
    try:
        store = binarystore.resolve(settings)
        add("binarystore", True, store.describe())
        if write_probe or settings.mode == "receiver":
            try:
                store.probe()
                add("binarystore write", True, "probe object written and removed")
            except Exception as exc:
                add("binarystore write", False, str(exc))
    except Exception as exc:
        add("binarystore", False, str(exc))
    finally:
        if store is not None:
            store.close()

    # Role-specific state.
    if settings.mode == "sender":
        cursor = common.read_cursor(settings)
        if cursor.get("last_cycle_id"):
            add(
                "cursor",
                True,
                f"baseline {cursor['last_cycle_id']} "
                f"({common.fmt_age(cursor.get('last_success_at'))})",
            )
        else:
            add(
                "cursor",
                None,
                "no cursor yet; the next cycle is a cold start and emits no removals",
                fatal=False,
            )
        baseline = common.snapshots_dir(settings) / f"{cursor.get('last_cycle_id')}.jsonl"
        if cursor.get("last_cycle_id") and not baseline.is_file():
            add(
                "baseline snapshot",
                False,
                f"cursor points at {cursor['last_cycle_id']} but "
                f"{baseline.name} is not retained; the next cycle is a cold start",
            )
    else:
        rows = common.read_ledger(common.receiver_ledger_path(settings))
        add("ledger", True, f"{len(rows)} row(s) recorded", fatal=False)
        incomplete = _incomplete_chunk_sets(rows)
        if incomplete:
            add(
                "chunk sets",
                False,
                f"{len(incomplete)} chunked cycle(s) waiting on missing chunks",
            )

    failed = [c for c in checks if c["ok"] is False and c["fatal"]]
    return {"checks": checks, "failed": len(failed), "ok": not failed}


# -- repositories -----------------------------------------------------------


def repos(settings: Settings, *, counts: bool = False) -> dict[str, Any]:
    """Every repository with its packageType and whether the filters admit it.

    Answers "what will actually sync" before a cycle answers it for you.
    Filter precedence matches the sender: allowlist first, then both
    denylists, so a system repository named in the allowlist still does not
    get through.
    """
    with ArtifactoryClient.from_settings(settings) as client:
        listing = client.list_repositories()
        included = set(settings.included_repos)
        excluded = set(settings.excluded_repos)
        type_deny = set(settings.excluded_package_types)

        out_rows = []
        for repo in sorted(listing, key=lambda r: r.get("key", "")):
            key = repo.get("key", "")
            package_type = repo.get("packageType", "")
            if included and key not in included:
                verdict, reason = False, "not in included_repos"
            elif key in excluded:
                verdict, reason = False, "in excluded_repos"
            elif package_type in type_deny:
                verdict, reason = False, f"packageType {package_type} excluded"
            else:
                verdict, reason = True, ""
            row = {
                "key": key,
                "type": repo.get("type", ""),
                "package_type": package_type,
                "synced": verdict,
                "reason": reason,
            }
            if counts:
                row["artifacts"] = client.aql_count(
                    json.dumps({"repo": key, "type": "file"}, separators=(",", ":"))
                )
            out_rows.append(row)

    return {
        "repos": out_rows,
        "synced_count": sum(1 for r in out_rows if r["synced"]),
        "total": len(out_rows),
        "allowlist_active": bool(included),
    }


# -- history ----------------------------------------------------------------


def cycles(
    settings: Settings,
    *,
    limit: int = 20,
    since: float | None = None,
    until: float | None = None,
    status_filter: str | None = None,
    repo: str | None = None,
    include_quiet: bool = False,
) -> dict[str, Any]:
    """The cycle history for this side, newest last.

    The sender reads ``state/cycles.jsonl`` and the receiver
    ``state/processed.jsonl``. The two ledgers do not carry the same fields
    (one records what was sent, the other what was applied), so the rows are
    normalised into a common shape here and the role-specific extras are kept
    alongside under ``raw``.
    """
    if settings.mode == "sender":
        path = common.sender_ledger_path(settings)
        rows = common.read_ledger(path)
    else:
        path = common.receiver_ledger_path(settings)
        rows = common.read_ledger(path)

    normalised = [_normalise_cycle_row(r, settings.mode) for r in rows]

    if not include_quiet:
        normalised = [r for r in normalised if r["status"] not in QUIET_STATUSES]
    if status_filter:
        wanted = {s.strip() for s in status_filter.split(",") if s.strip()}
        normalised = [r for r in normalised if r["status"] in wanted]
    if since is not None:
        normalised = [r for r in normalised if (r["at"] or 0) >= since]
    if until is not None:
        normalised = [r for r in normalised if (r["at"] or 0) <= until]
    if repo:
        normalised = [r for r in normalised if repo in r["repos"]]

    total = len(normalised)
    if limit > 0:
        normalised = normalised[-limit:]
    return {
        "mode": settings.mode,
        "ledger": str(path),
        "ledger_present": path.exists(),
        "matched": total,
        "shown": len(normalised),
        "cycles": normalised,
    }


def _normalise_cycle_row(row: dict[str, Any], mode: str) -> dict[str, Any]:
    repos_added = row.get("repos_added") or {}
    repos_removed = row.get("repos_removed") or {}
    if mode == "receiver":
        repos_list = list(row.get("repos") or [])
        added = row.get("blob_count", 0)
        removed = row.get("deleted_count", 0)
        size = row.get("total_bytes", 0)
        note = ""
        failures = row.get("failures") or []
        delete_failures = row.get("delete_failures") or []
        if failures:
            note = f"{len(failures)} import failure(s)"
        elif delete_failures:
            note = f"{len(delete_failures)} delete failure(s)"
    else:
        repos_list = sorted(set(repos_added) | set(repos_removed))
        added = row.get("added", 0)
        removed = row.get("removed", 0)
        size = row.get("archive_bytes", 0)
        note = row.get("note", "")
        if row.get("deferred_blobs"):
            deferred = f"{row['deferred_blobs']} blob(s) deferred"
            note = f"{note}; {deferred}" if note else deferred
    return {
        "cycle_id": row.get("cycle_id", ""),
        "parent_cycle_id": row.get("parent_cycle_id"),
        "chunk": (
            f"{row['chunk_seq']}/{row['chunk_total']}"
            if row.get("chunk_total", 1) > 1
            else ""
        ),
        "status": row.get("status", "?"),
        "at": row.get("at") or row.get("processed_at"),
        "added": added,
        "removed": removed,
        "bytes": size,
        "repos": repos_list,
        "note": note,
        "raw": row,
    }


def show_cycle(settings: Settings, cycle_id: str) -> dict[str, Any]:
    """Everything known about one cycle, from every place it left a trace."""
    cycle_id = common.resolve_cycle_ref(settings, cycle_id)
    out: dict[str, Any] = {"cycle_id": cycle_id, "mode": settings.mode}

    ledger = (
        common.sender_ledger_path(settings)
        if settings.mode == "sender"
        else common.receiver_ledger_path(settings)
    )
    matches = [
        r
        for r in common.read_ledger(ledger)
        if r.get("cycle_id") == cycle_id or r.get("parent_cycle_id") == cycle_id
    ]
    out["ledger_rows"] = matches

    snapshot = common.snapshots_dir(settings) / f"{cycle_id}.jsonl"
    if snapshot.is_file():
        out["snapshot"] = {
            "path": str(snapshot),
            "entries": common.count_lines(snapshot),
            "bytes": snapshot.stat().st_size,
        }
    tree = common.exports_dir(settings) / cycle_id
    if tree.is_dir():
        out["metadata_tree"] = {
            "path": str(tree),
            "repos": sorted(
                p.name for p in (tree / "repositories").iterdir() if p.is_dir()
            )
            if (tree / "repositories").is_dir()
            else [],
        }

    archives = []
    for directory, where in (
        (settings.spool_dir, "spool"),
        (common.done_dir(settings), "done"),
    ):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(f"{cycle_id}*.tar.zst")):
            entry = {"path": str(path), "where": where, "bytes": path.stat().st_size}
            try:
                manifest = archive_mod.read_manifest(path)
                entry["manifest"] = _manifest_summary(manifest)
            except Exception as exc:
                entry["error"] = str(exc)
            archives.append(entry)
    out["archives"] = archives
    if not matches and not archives and "snapshot" not in out:
        raise CLIError(f"nothing recorded for cycle {cycle_id!r}")
    return out


def _manifest_summary(manifest) -> dict[str, Any]:
    return {
        "schema": manifest.schema,
        "cycle_id": manifest.cycle_id,
        "prev_cycle_id": manifest.prev_cycle_id,
        "parent_cycle_id": manifest.parent_cycle_id,
        "chunk_seq": manifest.chunk_seq,
        "chunk_total": manifest.chunk_total,
        "created_at": manifest.created_at,
        "source_instance": manifest.source_instance,
        "repos": manifest.repos,
        "blob_count": manifest.blob_count,
        "total_bytes": manifest.total_bytes,
        "entry_count": len(manifest.entries),
        "removed_count": len(manifest.removed),
    }


def snapshots(settings: Settings) -> dict[str, Any]:
    directory = common.snapshots_dir(settings)
    cursor_id = common.read_cursor(settings).get("last_cycle_id")
    rows = []
    for path in sorted(directory.glob("*.jsonl")):
        cycle_id = path.name.removesuffix(".jsonl")
        rows.append(
            {
                "cycle_id": cycle_id,
                "entries": common.count_lines(path),
                "bytes": path.stat().st_size,
                "at": int(path.stat().st_mtime),
                "is_baseline": cycle_id == cursor_id,
            }
        )
    return {
        "dir": str(directory),
        "snapshots": rows,
        "baseline": cursor_id,
        "retention": {
            "hours": settings.snapshot_retention_hours,
            "days": settings.snapshot_retention_days,
            "months": settings.snapshot_retention_months,
        },
    }


def snapshot_diff(
    settings: Settings,
    previous: str,
    current: str,
    *,
    repo: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Added and removed between two snapshots, using the sender's own diff.

    This is the tool for reading a refused cycle: it answers what the brake
    saw, against any two retained baselines rather than only the live pair.
    """
    prev_path = common.snapshot_path(settings, previous)
    cur_path = common.snapshot_path(settings, current)

    added = list(diff.added(prev_path, cur_path))
    removed = list(diff.removed(prev_path, cur_path))
    if repo:
        added = [e for e in added if e.repo_key == repo]
        removed = [e for e in removed if e.repo_key == repo]

    baseline = common.count_lines(prev_path)
    fraction = (len(removed) / baseline) if baseline else 0.0
    out: dict[str, Any] = {
        "previous": str(prev_path),
        "current": str(cur_path),
        "baseline_entries": baseline,
        "current_entries": common.count_lines(cur_path),
        "added": len(added),
        "removed": len(removed),
        "added_bytes": sum(e.size for e in added),
        "repos_added": dict(Counter(e.repo_key for e in added)),
        "repos_removed": dict(Counter(e.repo_key for e in removed)),
        "delete_fraction": round(fraction, 4),
        "max_delete_fraction": settings.max_delete_fraction,
        "would_trip_brake": bool(removed) and fraction > settings.max_delete_fraction,
    }
    if full:
        out["added_entries"] = [
            {"repo": e.repo_key, "path": e.repo_path, "sha1": e.sha1, "size": e.size}
            for e in added
        ]
        out["removed_entries"] = [
            {"repo": e.repo_key, "path": e.repo_path, "sha1": e.sha1, "size": e.size}
            for e in removed
        ]
    return out


# -- archives ---------------------------------------------------------------


def archives(settings: Settings, *, where: str = "spool") -> dict[str, Any]:
    directories = []
    if where in ("spool", "all"):
        directories.append((settings.spool_dir, "spool"))
    if where in ("done", "all"):
        directories.append((common.done_dir(settings), "done"))

    rows = []
    for directory, label in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.tar.zst")):
            row = {
                "name": path.name,
                "where": label,
                "bytes": path.stat().st_size,
                "at": int(path.stat().st_mtime),
            }
            try:
                manifest = archive_mod.read_manifest(path)
                row.update(
                    {
                        "cycle_id": manifest.cycle_id,
                        "chunk": f"{manifest.chunk_seq}/{manifest.chunk_total}",
                        "blob_count": manifest.blob_count,
                        "total_bytes": manifest.total_bytes,
                        "repos": manifest.repos,
                        "removed": len(manifest.removed),
                        "source": manifest.source_instance,
                    }
                )
            except Exception as exc:
                row["error"] = str(exc)
            rows.append(row)
    return {"archives": rows, "count": len(rows)}


def find_archive(settings: Settings, ref: str) -> Path:
    """Resolve an archive reference: a path, a name, a cycle id, latest/prev.

    ``latest`` and ``prev`` are by write time across both spool and ``.done``,
    which is what an operator means by "the last archive" regardless of
    whether the receiver has already consumed it.
    """
    candidate = Path(ref)
    if candidate.is_file():
        return candidate

    if ref in ("latest", "prev"):
        found = []
        for directory in (settings.spool_dir, common.done_dir(settings)):
            if directory.is_dir():
                found.extend(directory.glob("*.tar.zst"))
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            raise CLIError("no archives in spool or .done")
        if ref == "latest":
            return found[0]
        if len(found) < 2:
            raise CLIError("only one archive present, so 'prev' resolves to nothing")
        return found[1]

    for directory in (settings.spool_dir, common.done_dir(settings)):
        for name in (ref, f"{ref}.tar.zst"):
            path = directory / name
            if path.is_file():
                return path
        matches = sorted(directory.glob(f"{ref}*.tar.zst"))
        if matches:
            return matches[0]
    raise CLIError(f"no archive matching {ref!r} in spool or .done")


def inspect_archive(
    settings: Settings, ref: str, *, entries: bool = False, verify: bool = False
) -> dict[str, Any]:
    """Manifest summary for one archive, optionally verifying its blobs.

    ``verify`` recomputes the sha1 of every blob in the tar and checks it
    against the name it is stored under, which is the only check that can
    tell a truncated transport hop from a healthy one before the receiver
    imports it.
    """
    path = find_archive(settings, ref)
    manifest = archive_mod.read_manifest(path)
    out: dict[str, Any] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "manifest": _manifest_summary(manifest),
    }
    if entries:
        out["entries"] = manifest.entries
        out["removed"] = manifest.removed
    if verify:
        out["verification"] = _verify_archive(path)
    return out


def _verify_archive(path: Path) -> dict[str, Any]:
    import tarfile

    import zstandard as zstd

    checked = 0
    bad: list[str] = []
    dctx = zstd.ZstdDecompressor()
    with open(path, "rb") as raw, dctx.stream_reader(raw) as reader:
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                if not member.name.startswith(f"{archive_mod.BLOBS_PREFIX}/"):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    bad.append(f"{member.name}: unreadable")
                    continue
                digest = hashlib.sha1()
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
                checked += 1
                expected = Path(member.name).name
                if digest.hexdigest() != expected:
                    bad.append(f"{expected}: content hashes to {digest.hexdigest()}")
    return {"blobs_checked": checked, "mismatches": bad, "ok": not bad}


# -- blobs ------------------------------------------------------------------


def blob(
    settings: Settings, sha1: str, *, get: Path | None = None
) -> dict[str, Any]:
    """Where the configured backend looks for a blob, and whether it is there.

    A wrong key prefix and a blob Artifactory has not written yet both read
    as a 404, so the address is reported whether or not the read succeeds:
    comparing it against a blob known to exist is what tells the two apart.
    """
    sha1 = sha1.strip().lower()
    if len(sha1) != 40 or not all(c in "0123456789abcdef" for c in sha1):
        raise CLIError(f"not a sha1: {sha1!r}")

    store = binarystore.resolve(settings)
    try:
        out: dict[str, Any] = {
            "sha1": sha1,
            "backend": store.kind,
            "location": store.location(sha1),
            "detail": store.describe(),
        }
        opened = store.open(sha1)
        if opened is None:
            out["present"] = False
            return out
        reader, size = opened
        out["present"] = True
        out["size"] = size
        try:
            if get is not None:
                digest = hashlib.sha1()
                written = 0
                with open(get, "wb") as dst:
                    for block in iter(lambda: reader.read(1024 * 1024), b""):
                        digest.update(block)
                        dst.write(block)
                        written += len(block)
                out["written_to"] = str(get)
                out["written_bytes"] = written
                out["sha1_verified"] = digest.hexdigest() == sha1
        finally:
            reader.close()
        return out
    finally:
        store.close()


# -- aql --------------------------------------------------------------------

_KEY_FIELDS = ("repo", "path", "name")


def run_aql(
    settings: Settings,
    query: str | None,
    *,
    count_criteria: str | None = None,
    force: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Run an ad-hoc AQL query, refusing a projection that omits the item key.

    AQL collapses adjacent duplicate rows over the projected fields, silently
    and with a healthy 200. A diagnostic query that omits repo, path or name
    can therefore return a fraction of the real rows and read as proof of a
    problem that does not exist, so the guard applies here too rather than
    only in the sender's own queries.
    """
    with ArtifactoryClient.from_settings(settings) as client:
        if count_criteria is not None:
            return {
                "criteria": count_criteria,
                "total": client.aql_count(count_criteria),
            }

        assert query is not None
        warnings = []
        if ".include(" in query:
            missing = [f for f in _KEY_FIELDS if f'"{f}"' not in query]
            if missing:
                message = (
                    f"the projection omits {', '.join(missing)}, so AQL will "
                    "collapse adjacent duplicate rows and under-report"
                )
                if not force:
                    raise CLIError(f"refusing to run: {message} (--force to override)")
                warnings.append(message)

        rows = client.aql(query)
        return {
            "query": query,
            "rows": len(rows),
            "warnings": warnings,
            "results": rows[:limit] if limit > 0 else rows,
            "truncated": bool(limit > 0 and len(rows) > limit),
        }

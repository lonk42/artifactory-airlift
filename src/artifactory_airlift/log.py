"""Logging configuration.

Two output formats are supported, chosen by `AIRLIFT_LOG_FORMAT`:

  console (default) - human-readable single-line records with a
                       friendly message per known event_name and a
                       trailing key=value tail for any extra fields.
                       Example:
                         2026-05-13 02:15:33 INFO  sender   cycle=1778636042 diff vs prev=1778635981: 5 added, 2 removed
  json              - the original structlog JSON output. Use when
                       piping logs to a processor that wants strict
                       structured data.

Pass either lower- or upper-case via the env var.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


# event_name -> human template. Templates use str.format and may reference
# any of the keys present in the event dict (plus `cycle_id`, which is
# pulled out separately and reattached as a tag column).
#
# Keep messages short, terse, and matter-of-fact: one fact per line. Counts
# come last so readers can scan a column of cycles for the delta.
_EVENT_TEMPLATES: dict[str, str] = {
    # Process lifecycle.
    "startup": "Starting up. Mode={mode}, instance={instance}, cycle_seconds={cycle_seconds}.",
    "unknown_mode": "Unknown mode: {mode}.",

    # Sender lifecycle.
    "sender.cycle_start": "Cycle starting; running Artifactory system export.",
    "sender.cycle_skipped_pending": "Skipping cycle: {pending_count} archive(s) still in spool awaiting transport.",
    "sender.startup_sweep": "Startup sweep: removed {partials_removed} orphan .partial file(s) and {staging_dirs_removed} staging dir(s).",
    "sender.repos_excluded": "Excluding {count} repo(s) from sync: {repos}",
    "sender.repos_included": "Allowlist active: syncing only {count} repo(s): {repos}",
    "sender.list_repositories_failed": "Could not list repositories for packageType filter ({error}); falling back to name-only exclusion.",
    "sender.snapshot_written": "Snapshot written: {count} artifacts across {repo_count} repos.",
    "sender.per_repo_counts": "Per-repo counts on source: {summary}",
    "sender.previous_snapshot_missing": "Previous snapshot missing (prev={prev_cycle_id}); treating this as a cold start.",
    "sender.removals_skipped_cold_start": "Cold start: not emitting removals this cycle.",
    "sender.diff_computed": "Diff vs prev={prev_cycle_id}: {added} added, {removed} removed.",
    "sender.per_repo_changes": "Per-repo changes: {summary}",
    "sender.no_changes": "No changes; cursor advanced.",
    "sender.archive_finalized": "Archive ready: {path} ({size_human}, {blob_count} blob(s), {repo_count} repo(s)).",
    "sender.cycle_chunked": "Cycle split into {chunk_total} chunks (raw {raw_bytes_human} > threshold {threshold_human}).",
    "sender.chunk_finalized": "Chunk {chunk_seq}/{chunk_total} ready: {path} ({size_human}, {blob_count} blob(s), {repo_count} repo(s)).",
    "sender.spool_backpressure": "Spool free {free_human} < required {required_human} (min {min_free_human} + chunk {projected_human}); aborting cycle.",
    "sender.ping_not_ok": "Artifactory ping failed; skipping cycle.",
    "sender.lock_held": "Another sender holds the lock; skipping cycle ({error}).",
    "sender.cycle_failed": "Cycle failed (see traceback).",

    # Receiver lifecycle.
    "receiver.extract_start": "Extracting archive: {path} ({size_human}).",
    "receiver.manifest_loaded": "Manifest loaded: {blob_count} blob(s) ({total_bytes_human} uncompressed) across {repo_count} repo(s); {removed_count} pending deletion(s).",
    "receiver.cycle_id_mismatch": "Archive cycle_id mismatch (file={file_cycle_id}, manifest={manifest_cycle_id}).",
    "receiver.gap_detected": "Gap detected (last processed={prev_cycle_id}); processing anyway.",
    "receiver.blobs_written": "Blobs: {written} written, {skipped} skipped (already in filestore).",
    "receiver.per_repo_changes": "Per-repo changes applied: {summary}",
    "receiver.import_failed": "Import failed: {path}.",
    "receiver.import_partial": "Import partial: {failures} repo failure(s). Detailed lines follow.",
    "receiver.import_failure": "Import failure {index}/{total}: HTTP {code} {detail}",
    "receiver.delete_failed": "Delete failed: {repo}/{path}: {error}",
    "receiver.delete_missing": "Delete: {repo}/{path} was already absent (404).",
    "receiver.deletes_applied": "Deletes: {deleted} applied, {failed} failed.",
    "receiver.cycle_done": "Cycle done: status={status}.",
    "receiver.startup_sweep": "Startup sweep: removed {partials_removed} orphan .partial file(s) and {staging_dirs_removed} staging dir(s).",
    "receiver.chunk_waiting": "Final chunk {chunk_seq}/{chunk_total} for parent={parent_cycle_id} waiting for chunks: {missing}.",
    "receiver.chunk_staged": "Chunk {chunk_seq}/{chunk_total} staged (parent={parent_cycle_id}): {written} blob(s) written, {skipped} skipped.",
    "receiver.ping_not_ok": "Artifactory ping failed; skipping cycle.",
    "receiver.lock_held": "Another receiver holds the lock; skipping cycle ({error}).",
    "receiver.filestore_probe_failed": "Filestore probe failed: {error}",
    "receiver.cycle_failed": "Cycle failed (see traceback).",
    "receiver.archive_failed": "Archive processing failed; see traceback.",

    # Artifactory client passthrough.
    "export.start": "Artifactory: starting system export to {path}.",
    "export.done": "Artifactory: system export complete.",
    "import.repositories_done": "Artifactory: import_repositories returned.",

    # Archive / export internals.
    "archive.built": "Archive built: {blob_count} blob(s), {total_bytes_human} uncompressed, packed to {size_human} ({repo_count} repo(s)) -> {path}",
    "archive.missing_blob": "Missing blob in binarystore: sha1={sha1} repo={repo} path={path}",
    "binarystore.selected": "Binarystore backend: {backend} ({detail}).",
    "binarystore.azure_identity": (
        "Azure: authenticating as a platform identity ({detail}); no account key in use."
    ),
    "binarystore.azure_token_acquired": "Azure: access token acquired from {source}.",
    "binarystore.credentials_encrypted": (
        "binarystore.xml holds encrypted credentials; using the configured "
        "binarystore keys instead."
    ),
    "binarystore.multipart_started": (
        "Multipart upload started: sha1={sha1} size={size} part_bytes={part_bytes}."
    ),
    "binarystore.multipart_done": "Multipart upload complete: sha1={sha1} in {parts} part(s).",
    "binarystore.multipart_abort_failed": "Multipart abort failed: {error}",
    "receiver.binarystore_unavailable": "Binarystore unavailable: {error}",
    "sender.binarystore_unavailable": "Binarystore unavailable: {error}",
    "sender.filestore_probe_failed": "Filestore probe failed: {error}",
    "receiver.binarystore_recovered": "Binarystore usable again after {attempts} attempt(s).",
    "sender.binarystore_recovered": "Binarystore usable again after {attempts} attempt(s).",
    "config_invalid": "Configuration is invalid: {error}. Idling; fix the config and restart.",
    "unknown_mode": "Unknown mode {mode}. Idling; set mode to sender or receiver.",
    "parked_after_exit": "Idling after exit code {code} in mode {mode}.",
    "parked_after_fatal": "Idling after an unhandled error: {error}.",
    "sender.entries_deferred": (
        "Deferred {entry_count} entr(ies) covering {blob_count} blob(s) not yet in "
        "the binarystore; they will retry next cycle."
    ),
    "export.no_repositories_dir": "No repositories/ dir in export at {path}.",

    # Filestore internals (debug-level).
    "filestore.chown_skipped": "chown skipped: {path}",
}


# Fields the console renderer pulls out and renders in dedicated columns;
# everything else is tacked on as a key=value tail.
_RESERVED_KEYS = {
    "timestamp",
    "level",
    "event",
    "cycle_id",
    "exc_info",
    "stack_info",
    "logger",
    "func_name",
    "module",
    "filename",
    "lineno",
    "process",
    "thread",
    "process_name",
    "thread_name",
}


# Per-event fields that are kept in the structured event for JSON consumers
# but suppressed from the console tail because the human template already
# surfaces the same information (typically via a pre-formatted `summary`
# string sitting beside the structured dict).
_EXTRA_CONSUMED: dict[str, set[str]] = {
    "sender.per_repo_counts": {"repos"},
    "sender.per_repo_changes": {"added", "removed"},
    "receiver.per_repo_changes": {"added", "removed"},
    "sender.archive_finalized": {"size_bytes", "repos"},
    "sender.chunk_finalized": {"size_bytes", "repos", "parent_cycle_id"},
    "sender.cycle_chunked": {"raw_bytes", "threshold_bytes"},
    "sender.spool_backpressure": {"free_bytes", "required_bytes"},
    "sender.cycle_skipped_pending": {"pending"},
    "receiver.chunk_staged": {"parent_cycle_id"},
    "archive.built": {"size_bytes", "total_bytes", "repos"},
    "receiver.extract_start": {"size_bytes"},
    "receiver.manifest_loaded": {"total_bytes", "repos"},
}


def _component_from_event(event_name: str) -> str:
    """Logical component label for the leftmost column.

    'sender.cycle_start'           -> 'sender'
    'receiver.blobs_written'       -> 'receiver'
    'archive.built'                -> 'archive'
    'startup'                      -> 'main'
    """
    if "." in event_name:
        return event_name.split(".", 1)[0]
    return "main"


def _short_cycle_id(cycle_id: str) -> str:
    # Cycle IDs are <epoch>-<hex>; the epoch alone is enough to identify a
    # run when scanning logs, and the hex suffix bloats the line.
    if not cycle_id:
        return ""
    return cycle_id.split("-", 1)[0]


def _short_ts(ts: str) -> str:
    # structlog's iso TimeStamper produces e.g. "2026-05-13T01:30:12.024369Z";
    # rewrite the 'T' separator to a space and strip microseconds + 'Z' so
    # the line scans like a syslog record.
    if not ts:
        return ""
    if "T" in ts:
        head, _, _ = ts.partition(".")
        return head.replace("T", " ")
    return ts


# Map structlog level names to fixed-width 5-char labels so the level column
# never bumps the component column off its alignment.
_LEVEL_LABELS = {
    "DEBUG": "DEBUG",
    "INFO": "INFO ",
    "WARN": "WARN ",
    "WARNING": "WARN ",
    "ERROR": "ERROR",
    "CRITICAL": "CRIT ",
}


def _console_renderer(_logger, _method_name, event_dict):
    ts = _short_ts(event_dict.pop("timestamp", ""))
    raw_level = str(event_dict.pop("level", "info")).upper()
    level = _LEVEL_LABELS.get(raw_level, raw_level[:5].ljust(5))
    event_name = str(event_dict.pop("event", ""))
    cycle_id = event_dict.pop("cycle_id", None)

    component = _component_from_event(event_name)
    template = _EVENT_TEMPLATES.get(event_name)

    if template is not None:
        try:
            body = template.format(**event_dict, cycle_id=cycle_id or "")
        except (KeyError, IndexError):
            body = event_name
        # When the template renders cleanly, every key it consumed is still in
        # event_dict; we surface the rest as a tail so structured detail is
        # never lost, minus any explicit also-consumed fields (the structured
        # backing of a human summary string lives in event_dict for JSON
        # consumers but should not duplicate the message in console mode).
        consumed = _format_keys(template) | _EXTRA_CONSUMED.get(event_name, set())
        extras = {k: v for k, v in event_dict.items() if k not in consumed and k not in _RESERVED_KEYS}
    else:
        body = event_name or "(no event)"
        extras = {k: v for k, v in event_dict.items() if k not in _RESERVED_KEYS}

    tail = ""
    if extras:
        tail = " " + " ".join(f"{k}={_fmt_value(v)}" for k, v in extras.items())

    cycle_tag = f" cycle={_short_cycle_id(cycle_id)}" if cycle_id else ""
    # Fixed-width level (5) and component (8) so columns align for eyeballing.
    line = f"{ts} {level} {component:<8}{cycle_tag} {body}{tail}".rstrip()

    # Preserve stack/traceback rendering by appending it after the line; the
    # format_exc_info processor will have already populated `exception` if
    # logger.exception was called.
    exc = event_dict.get("exception")
    if exc:
        line = f"{line}\n{exc}"
    return line


def _format_keys(template: str) -> set[str]:
    """Return the set of names referenced by a str.format template."""
    import string

    formatter = string.Formatter()
    keys: set[str] = set()
    for _, field_name, _, _ in formatter.parse(template):
        if field_name:
            # field_name can be "foo.bar" or "foo[0]"; take just the root.
            root = field_name.split(".", 1)[0].split("[", 1)[0]
            if root:
                keys.add(root)
    return keys


def _fmt_value(value) -> str:
    if isinstance(value, dict):
        return _fmt_counter_dict(value)
    s = str(value)
    if not s or any(c.isspace() for c in s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _fmt_counter_dict(d: dict) -> str:
    """Render a Counter-like {key: int} mapping as "k=v, k=v" for human logs.

    Keys are sorted by count desc, then alphabetically, so the biggest
    contributor is leftmost.
    """
    if not d:
        return "{}"
    items = sorted(d.items(), key=lambda kv: (-(kv[1] if isinstance(kv[1], int) else 0), kv[0]))
    return ", ".join(f"{k}={v}" for k, v in items)


def human_bytes(n: int | float) -> str:
    """Format a byte count in IEC units with one decimal, e.g. 1.2MiB."""
    if n is None:
        return "?"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(n)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}PiB"


def configure(level: str | None = None) -> None:
    lvl = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    fmt = os.environ.get("AIRLIFT_LOG_FORMAT", "console").lower()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, lvl, logging.INFO),
    )

    if fmt == "json":
        final_processor = structlog.processors.JSONRenderer()
    else:
        final_processor = _console_renderer

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            final_processor,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, lvl, logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)

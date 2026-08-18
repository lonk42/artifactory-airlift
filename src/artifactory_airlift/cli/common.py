"""Shared plumbing for the CLI: settings, time formatting, state lookups."""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from .. import config as config_mod
from ..config import Settings

DEFAULT_CONFIG_PATH = Path("/etc/airlift/config.yaml")

# Settings whose value must never be printed. Matched exactly rather than by
# substring: binarystore_prefix and artifactory_uid contain no secret, and a
# fuzzy rule that redacted them would make the output useless for the very
# debugging the CLI exists to do.
SENSITIVE_FIELDS = frozenset(
    {
        "artifactory_token",
        "artifactory_password",
        "binarystore_access_key",
        "binarystore_secret_key",
        "binarystore_account_key",
    }
)


class CLIError(Exception):
    """A failure to report to the operator, without a traceback."""

    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def load_settings(args) -> Settings:
    """Load settings the way the daemon does, with CLI path overrides on top.

    ``--state-dir`` and ``--spool-dir`` exist because the CLI is also pointed
    at a copy of a state directory when debugging, not only at the live one.
    """
    path = Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
    try:
        settings = config_mod.load(path)
    except Exception as exc:
        raise CLIError(f"settings are invalid: {exc}") from exc
    if getattr(args, "state_dir", None):
        settings.state_dir = Path(args.state_dir)
    if getattr(args, "spool_dir", None):
        settings.spool_dir = Path(args.spool_dir)
    return settings


def require_mode(settings: Settings, mode: str, command: str) -> None:
    if settings.mode != mode:
        raise CLIError(
            f"'{command}' applies to the {mode}; this instance runs as "
            f"{settings.mode!r} (set AIRLIFT_MODE or use --config to point at "
            f"the other side)"
        )


def config_path_for(args) -> Path:
    """The config file this invocation reads, honouring ``--config``."""
    return Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG_PATH


def config_file_values(args) -> dict[str, Any]:
    """The raw YAML the daemon would read, or an empty dict."""
    path = config_path_for(args)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# -- paths ------------------------------------------------------------------


def snapshots_dir(settings: Settings) -> Path:
    return settings.state_dir / "snapshots"


def exports_dir(settings: Settings) -> Path:
    return settings.state_dir / "exports"


def cursor_path(settings: Settings) -> Path:
    return settings.state_dir / "cursor.json"


def sender_ledger_path(settings: Settings) -> Path:
    return settings.state_dir / "cycles.jsonl"


def receiver_ledger_path(settings: Settings) -> Path:
    return settings.state_dir / "processed.jsonl"


def done_dir(settings: Settings) -> Path:
    return settings.spool_dir / ".done"


def lock_path(settings: Settings) -> Path:
    return settings.state_dir / f"{settings.mode}.lock"


def read_cursor(settings: Settings) -> dict[str, Any]:
    data = None
    try:
        data = json.loads(cursor_path(settings).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL ledger, skipping unparseable lines.

    A truncated final line is possible if the daemon was killed mid-append,
    and is not a reason to refuse to show the rest of the history.
    """
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def daemon_running(settings: Settings) -> bool | None:
    """True when the daemon holds the state lock, None when it cannot be told.

    Opened for append rather than write: ``state.file_lock`` truncates the
    file it locks, and this must not disturb the pid the running daemon
    wrote there.
    """
    path = lock_path(settings)
    if not path.exists():
        return False
    try:
        fh = open(path, "a")
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return None
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def count_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


# -- cycle id resolution ----------------------------------------------------


def resolve_cycle_ref(settings: Settings, ref: str) -> str:
    """Turn ``latest``/``prev``/an id into a cycle id.

    ``latest`` and ``prev`` mean different things per role and that is
    intentional: on the sender the interesting cycle is the one the cursor
    points at, on the receiver it is the last row of the ledger.
    """
    if ref not in ("latest", "prev"):
        return ref

    if settings.mode == "sender":
        ids = [row["cycle_id"] for row in read_ledger(sender_ledger_path(settings))
               if isinstance(row.get("cycle_id"), str)]
        cursor_id = read_cursor(settings).get("last_cycle_id")
        if not ids and isinstance(cursor_id, str):
            ids = [cursor_id]
    else:
        ids = [row["cycle_id"] for row in read_ledger(receiver_ledger_path(settings))
               if isinstance(row.get("cycle_id"), str)]

    if not ids:
        raise CLIError(f"no cycles recorded, so {ref!r} resolves to nothing")
    if ref == "latest":
        return ids[-1]
    if len(ids) < 2:
        raise CLIError("only one cycle recorded, so 'prev' resolves to nothing")
    return ids[-2]


def snapshot_path(settings: Settings, ref: str) -> Path:
    """Resolve a snapshot reference: a path, a cycle id, ``latest`` or ``prev``."""
    candidate = Path(ref)
    if candidate.is_file():
        return candidate

    snaps = sorted(snapshots_dir(settings).glob("*.jsonl"))
    if ref in ("latest", "prev"):
        if not snaps:
            raise CLIError(f"no snapshots in {snapshots_dir(settings)}")
        if ref == "latest":
            return snaps[-1]
        if len(snaps) < 2:
            raise CLIError("only one snapshot retained, so 'prev' resolves to nothing")
        return snaps[-2]

    direct = snapshots_dir(settings) / f"{ref}.jsonl"
    if direct.is_file():
        return direct
    raise CLIError(f"no snapshot for {ref!r} (looked in {snapshots_dir(settings)})")


# -- time -------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_when(value: str, *, now: float | None = None) -> float:
    """Parse a point in time: ISO 8601, epoch seconds, or relative (``7d``).

    Relative values are always in the past, because every use of them here
    (a history filter, an export window) is looking backwards. Naive ISO
    timestamps are read as UTC, matching the rest of airlift's bucketing.
    """
    current = time.time() if now is None else now
    v = value.strip()
    if not v:
        raise CLIError("empty timestamp")
    if v in ("now", "today"):
        return current
    m = _RELATIVE_RE.match(v)
    if m:
        return current - int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    if v.isdigit() and len(v) >= 9:
        return float(v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CLIError(
            f"cannot read {value!r} as a time: use ISO 8601 (2026-08-18T12:00), "
            f"epoch seconds, or a relative window like 7d"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fmt_ts(epoch: float | int | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def fmt_age(epoch: float | int | None, *, now: float | None = None) -> str:
    """Age of an epoch timestamp as a compact human string."""
    if not epoch:
        return "-"
    delta = (time.time() if now is None else now) - float(epoch)
    if delta < 0:
        return "in the future"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h{int((delta % 3600) // 60)}m ago"
    return f"{int(delta // 86400)}d{int((delta % 86400) // 3600)}h ago"


def cycle_time(cycle_id: str) -> float | None:
    """Extract the epoch seconds a cycle id starts with, if it has one."""
    head = cycle_id.split("-", 1)[0]
    return float(head) if head.isdigit() and len(head) >= 9 else None


def redact(name: str, value: Any) -> Any:
    if name not in SENSITIVE_FIELDS:
        return value
    text = str(value or "")
    return f"<set, {len(text)} chars>" if text else ""


def env_var_for(field: str) -> str:
    return f"AIRLIFT_{field.upper()}"


def env_value(field: str) -> str | None:
    return os.environ.get(env_var_for(field))

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default: object = None) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise RuntimeError(f"lock held: {path}")
    try:
        fh.write(f"{os.getpid()}\n{int(time.time())}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _hour_bucket(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d%H")


def _day_bucket(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _month_bucket(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m")


def _hour_cutoff(now: float, n: int) -> float:
    dt = datetime.fromtimestamp(now, tz=timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    return dt.timestamp() - (n - 1) * 3600


def _day_cutoff(now: float, n: int) -> float:
    dt = datetime.fromtimestamp(now, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return dt.timestamp() - (n - 1) * 86400


def _month_cutoff(now: float, n: int) -> float:
    dt = datetime.fromtimestamp(now, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    y, m = dt.year, dt.month
    m -= n - 1
    while m <= 0:
        m += 12
        y -= 1
    return dt.replace(year=y, month=m).timestamp()


def gfs_keepers(
    paths: Iterable[Path],
    *,
    hours: int,
    days: int,
    months: int,
    now: float | None = None,
) -> set[Path]:
    """Return the subset of ``paths`` to retain under a GFS policy.

    For each tier with a count > 0, bucket entries by that tier's UTC
    calendar unit, keep the newest entry in each non-empty bucket whose
    bucket start falls within the wall-clock window of the last `count`
    units measured from `now`. Return the union across tiers.
    """
    current = time.time() if now is None else now

    stats: list[tuple[float, Path]] = []
    for p in paths:
        try:
            stats.append((p.stat().st_mtime, p))
        except OSError:
            continue
    stats.sort(key=lambda x: x[0], reverse=True)

    keepers: set[Path] = set()

    tiers = (
        (hours, _hour_cutoff(current, hours) if hours > 0 else 0.0, _hour_bucket),
        (days, _day_cutoff(current, days) if days > 0 else 0.0, _day_bucket),
        (months, _month_cutoff(current, months) if months > 0 else 0.0, _month_bucket),
    )

    for count, cutoff, bucket_fn in tiers:
        if count <= 0:
            continue
        winners: dict[str, Path] = {}
        for mtime, path in stats:
            if mtime < cutoff:
                break
            bk = bucket_fn(mtime)
            # stats is mtime-descending, so the first sighting of a bucket
            # is the newest entry in it.
            winners.setdefault(bk, path)
        keepers.update(winners.values())

    return keepers


def prune_to_keepers(
    directory: Path,
    keepers: set[Path],
    *,
    pattern: str = "*",
) -> list[Path]:
    """Remove entries matching ``pattern`` in ``directory`` not in ``keepers``."""
    if not directory.exists():
        return []
    removed: list[Path] = []
    for p in directory.glob(pattern):
        if p in keepers:
            continue
        try:
            if p.is_dir():
                p.rmdir()
            else:
                p.unlink()
        except OSError:
            continue
        removed.append(p)
    return removed


def prune_oldest(directory: Path, keep: int, *, pattern: str = "*") -> list[Path]:
    if not directory.exists():
        return []
    entries = sorted(directory.glob(pattern))
    if len(entries) <= keep:
        return []
    to_drop = entries[: len(entries) - keep]
    for p in to_drop:
        try:
            if p.is_dir():
                # Empty dirs only; sender writes flat files
                p.rmdir()
            else:
                p.unlink()
        except OSError:
            continue
    return to_drop

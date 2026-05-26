import os
import time
from datetime import datetime, timezone
from pathlib import Path

from artifactory_airlift.config import Settings
from artifactory_airlift.sender import _prune_history


def _touch(path: Path, ts: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    os.utime(path, (ts, ts))
    return path


def _settings(**overrides) -> Settings:
    base = dict(
        snapshot_retention_hours=0,
        snapshot_retention_days=3,
        snapshot_retention_months=0,
    )
    base.update(overrides)
    return Settings(**base)


def test_prune_history_preserves_just_written_snapshot(tmp_path: Path) -> None:
    snaps = tmp_path / "snapshots"
    exports = tmp_path / "exports"
    exports.mkdir()

    now = time.time()
    fresh = _touch(snaps / "fresh.jsonl", now)
    stale = _touch(snaps / "stale.jsonl", now - 100 * 86400)  # 100 days ago

    _prune_history(_settings(), snapshots_dir=snaps, exports_dir=exports)

    assert fresh.exists()
    assert not stale.exists()


def test_prune_history_keeps_gfs_union_drops_rest(tmp_path: Path) -> None:
    snaps = tmp_path / "snapshots"
    exports = tmp_path / "exports"
    exports.mkdir()

    # Pin "now" to noon UTC mid-month so the 5-hour and 5-day offsets used
    # below cannot straddle a UTC day or month boundary (e.g. running this
    # test between 00:00 and 05:00 UTC otherwise puts the 5-hour-old file
    # on the previous UTC day, making it the lone winner of that day-bucket
    # under the days tier).
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    HOUR = 3600
    DAY = 86400

    # Snapshots spread across hours, days, and months.
    fresh = _touch(snaps / "fresh.jsonl", now)
    one_hour = _touch(snaps / "h1.jsonl", now - 1 * HOUR)
    two_hours = _touch(snaps / "h2.jsonl", now - 2 * HOUR)
    five_hours = _touch(snaps / "h5.jsonl", now - 5 * HOUR)
    two_days = _touch(snaps / "d2.jsonl", now - 2 * DAY)
    five_days = _touch(snaps / "d5.jsonl", now - 5 * DAY)
    forty_days = _touch(snaps / "d40.jsonl", now - 40 * DAY)
    ninety_days = _touch(snaps / "d90.jsonl", now - 90 * DAY)

    settings = _settings(
        snapshot_retention_hours=3,
        snapshot_retention_days=3,
        snapshot_retention_months=2,
    )

    _prune_history(settings, snapshots_dir=snaps, exports_dir=exports, now=now)

    # hours=3 keeps fresh, h1, h2 (each in their own hour-bucket).
    # days=3 keeps fresh (today) + maybe an extra day; two_days within window.
    # months=2 keeps fresh (this month) + forty_days (last month).
    assert fresh.exists()
    assert one_hour.exists()
    assert two_hours.exists()
    assert two_days.exists()
    assert forty_days.exists()

    # Out of all tiers' windows:
    # five_hours: hour-bucket beyond last 3 hours, day already represented by fresh.
    # five_days: outside 3-day window, current-month already represented.
    # ninety_days: outside 2-month window.
    assert not five_hours.exists()
    assert not five_days.exists()
    assert not ninety_days.exists()


def test_prune_history_leaves_exports_to_history_keep(tmp_path: Path) -> None:
    snaps = tmp_path / "snapshots"
    exports = tmp_path / "exports"
    snaps.mkdir()
    exports.mkdir()

    # Write a current snapshot so GFS has something to keep.
    _touch(snaps / "fresh.jsonl", time.time())

    # Cycle ids sort lexicographically (epoch-prefixed); use ascending epochs.
    for epoch in range(1_700_000_000, 1_700_000_030):
        (exports / f"{epoch:010d}-aaaaaaaa").mkdir()

    settings = _settings(history_keep=24)
    _prune_history(settings, snapshots_dir=snaps, exports_dir=exports)

    remaining = sorted(p.name for p in exports.iterdir() if p.is_dir())
    # 30 created, history_keep=24 -> 6 oldest removed.
    assert len(remaining) == 24
    # Oldest survivors start at epoch 1_700_000_006.
    assert remaining[0] == "1700000006-aaaaaaaa"

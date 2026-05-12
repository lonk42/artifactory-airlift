import os
from datetime import datetime, timezone
from pathlib import Path

from artifactory_airlift import state


def _touch(path: Path, ts: float) -> Path:
    path.write_text("")
    os.utime(path, (ts, ts))
    return path


def _dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_hours_tier_picks_newest_per_hour_bucket(tmp_path: Path) -> None:
    now = _dt(2026, 5, 13, 12, 30)
    # Two entries each in three consecutive hour-buckets.
    paths = []
    for hour in (10, 11, 12):
        for minute in (5, 45):
            p = _touch(tmp_path / f"{hour:02d}{minute:02d}.jsonl",
                       _dt(2026, 5, 13, hour, minute))
            paths.append(p)

    keepers = state.gfs_keepers(paths, hours=3, days=0, months=0, now=now)

    # Newest per hour-bucket: 10:45, 11:45, 12:45.
    assert {p.name for p in keepers} == {"1045.jsonl", "1145.jsonl", "1245.jsonl"}


def test_hours_tier_drops_buckets_outside_window(tmp_path: Path) -> None:
    now = _dt(2026, 5, 13, 12, 30)
    paths = []
    for hour in (8, 9, 10, 11, 12):
        paths.append(_touch(tmp_path / f"{hour:02d}.jsonl",
                            _dt(2026, 5, 13, hour, 0)))

    # hours=3 -> last 3 hour-buckets (10, 11, 12).
    keepers = state.gfs_keepers(paths, hours=3, days=0, months=0, now=now)
    assert {p.name for p in keepers} == {"10.jsonl", "11.jsonl", "12.jsonl"}


def test_days_tier_picks_newest_per_day_bucket(tmp_path: Path) -> None:
    now = _dt(2026, 5, 13, 12, 0)
    paths = []
    for day in (11, 12, 13):
        for hour in (3, 18):
            p = _touch(
                tmp_path / f"d{day:02d}h{hour:02d}.jsonl",
                _dt(2026, 5, day, hour, 0),
            )
            paths.append(p)

    keepers = state.gfs_keepers(paths, hours=0, days=3, months=0, now=now)
    assert {p.name for p in keepers} == {
        "d11h18.jsonl",
        "d12h18.jsonl",
        "d13h18.jsonl",
    }


def test_months_tier_picks_newest_per_calendar_month(tmp_path: Path) -> None:
    now = _dt(2026, 2, 15, 0, 0)
    # End of January (31st) and a February entry.
    jan = _touch(tmp_path / "jan31.jsonl", _dt(2026, 1, 31, 23, 0))
    feb_early = _touch(tmp_path / "feb01.jsonl", _dt(2026, 2, 1, 0, 0))
    feb_late = _touch(tmp_path / "feb14.jsonl", _dt(2026, 2, 14, 12, 0))

    keepers = state.gfs_keepers(
        [jan, feb_early, feb_late], hours=0, days=0, months=2, now=now
    )
    # Jan bucket: jan31. Feb bucket: feb14 (newest within Feb).
    assert keepers == {jan, feb_late}


def test_calendar_month_wraps_year(tmp_path: Path) -> None:
    now = _dt(2026, 2, 15, 0, 0)
    dec = _touch(tmp_path / "dec.jsonl", _dt(2025, 12, 10, 0, 0))
    nov = _touch(tmp_path / "nov.jsonl", _dt(2025, 11, 30, 0, 0))
    feb = _touch(tmp_path / "feb.jsonl", _dt(2026, 2, 1, 0, 0))

    keepers = state.gfs_keepers([dec, nov, feb], hours=0, days=0, months=3, now=now)
    # months=3 covers Dec 2025, Jan 2026, Feb 2026; Nov is out of window.
    assert keepers == {dec, feb}


def test_union_across_tiers(tmp_path: Path) -> None:
    now = _dt(2026, 5, 13, 12, 0)
    # One snapshot per hour for the last 4 hours; plus one yesterday; plus one last month.
    h09 = _touch(tmp_path / "h09.jsonl", _dt(2026, 5, 13, 9, 0))
    h10 = _touch(tmp_path / "h10.jsonl", _dt(2026, 5, 13, 10, 0))
    h11 = _touch(tmp_path / "h11.jsonl", _dt(2026, 5, 13, 11, 0))
    h12 = _touch(tmp_path / "h12.jsonl", _dt(2026, 5, 13, 12, 0))
    yesterday = _touch(tmp_path / "yest.jsonl", _dt(2026, 5, 12, 8, 0))
    last_month = _touch(tmp_path / "april.jsonl", _dt(2026, 4, 20, 0, 0))

    keepers = state.gfs_keepers(
        [h09, h10, h11, h12, yesterday, last_month],
        hours=2,  # last 2 hour-buckets: 11, 12
        days=2,   # last 2 day-buckets: 12, 13 (h12 satisfies day=13 via newest)
        months=2, # last 2 month-buckets: April, May (h12 satisfies May)
        now=now,
    )
    assert keepers == {h11, h12, yesterday, last_month}


def test_empty_buckets_within_window_are_ok(tmp_path: Path) -> None:
    now = _dt(2026, 5, 13, 12, 0)
    only = _touch(tmp_path / "only.jsonl", _dt(2026, 5, 13, 11, 30))

    keepers = state.gfs_keepers([only], hours=10, days=0, months=0, now=now)
    # Only one non-empty bucket within the 10-hour window.
    assert keepers == {only}


def test_zero_tier_skipped(tmp_path: Path) -> None:
    now = _dt(2026, 5, 13, 12, 0)
    h10 = _touch(tmp_path / "h10.jsonl", _dt(2026, 5, 13, 10, 0))
    h11 = _touch(tmp_path / "h11.jsonl", _dt(2026, 5, 13, 11, 0))

    # hours=0 means the tier contributes nothing.
    keepers = state.gfs_keepers([h10, h11], hours=0, days=1, months=0, now=now)
    # day=1 (just today): newest entry is h11.
    assert keepers == {h11}


def test_no_snapshots_returns_empty_set(tmp_path: Path) -> None:
    keepers = state.gfs_keepers([], hours=10, days=30, months=12)
    assert keepers == set()


def test_missing_path_skipped(tmp_path: Path) -> None:
    now = _dt(2026, 5, 13, 12, 0)
    real = _touch(tmp_path / "real.jsonl", _dt(2026, 5, 13, 11, 0))
    ghost = tmp_path / "ghost.jsonl"  # never created

    keepers = state.gfs_keepers([real, ghost], hours=3, days=0, months=0, now=now)
    assert keepers == {real}

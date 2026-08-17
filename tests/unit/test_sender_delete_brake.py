"""The deletion brake.

Enumeration is a database query now, and a query can come back short while
still looking entirely healthy: AQL collapses adjacent duplicate rows over
the projected fields and returns HTTP 200 with well-formed JSON either way.
A filesystem walk failed loudly; this does not. Since a short enumeration is
indistinguishable from mass deletion, the sender caps how much of the mirror
one cycle may remove and refuses rather than guess.

The brake is deliberately cause-agnostic, so these tests drive it purely
through the size of the removal set.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ._store import fs_store
from artifactory_airlift import sender
from artifactory_airlift.config import Settings


class _FakeClient:
    """Serves a fixed enumeration; metadata queries return nothing.

    Metadata is only ever queried for *added* entries, and no test here adds
    any, so an empty second response is the honest answer rather than a stub.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.enumerations = 0

    def ping(self) -> bool:
        return True

    def list_repositories(self) -> list[dict]:
        return []

    def aql(self, query: str) -> list[dict]:
        if "$or" in query:
            return []
        self.enumerations += 1
        return self._rows


def _rows(n: int, start: int = 0) -> list[dict]:
    return [
        {
            "repo": "airlift-npm-local",
            "path": ".",
            "name": f"a{i:04d}.bin",
            "actual_sha1": f"{i:040x}",
            "size": 1,
        }
        for i in range(start, start + n)
    ]


def _settings(tmp_path: Path, **overrides) -> Settings:
    state = tmp_path / "state"
    spool = tmp_path / "spool"
    for p in (state, spool):
        p.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        mode="sender",
        instance_name="art-a",
        state_dir=state,
        spool_dir=spool,
        snapshot_retention_days=3,
        spool_min_free_bytes=0,
        excluded_package_types=[],
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _seed_baseline(settings: Settings, count: int) -> tuple[Path, Path, Path]:
    """Write a previous snapshot of ``count`` entries and point the cursor at it."""
    snaps = settings.state_dir / "snapshots"
    exports = settings.state_dir / "exports"
    for p in (snaps, exports):
        p.mkdir(parents=True, exist_ok=True)
    prev = snaps / "1700000000-baseline.jsonl"
    with prev.open("w") as fh:
        for row in _rows(count):
            fh.write(
                json.dumps(
                    {
                        "repo": row["repo"],
                        "path": row["name"],
                        "sha1": row["actual_sha1"],
                        "size": 1,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    cursor = settings.state_dir / "cursor.json"
    cursor.write_text(json.dumps({"last_cycle_id": "1700000000-baseline"}))
    return snaps, exports, cursor


def _run_cycle(settings: Settings, client, snaps: Path, exports: Path, cursor: Path):
    sender._cycle(
        settings,
        client=client,
        store=fs_store(settings.filestore_root),
        snapshots_dir=snaps,
        exports_dir=exports,
        cursor_path=cursor,
    )


def test_brake_trips_when_removals_exceed_the_fraction(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_delete_fraction=0.05)
    snaps, exports, cursor = _seed_baseline(settings, 100)

    # 10 of 100 gone: twice the 5% allowance.
    _run_cycle(settings, _FakeClient(_rows(90)), snaps, exports, cursor)

    # No archive shipped and no cursor advance, so the next cycle re-runs the
    # diff against the same baseline and a transient short read self-heals.
    assert list(settings.spool_dir.glob("*.tar.zst")) == []
    assert json.loads(cursor.read_text())["last_cycle_id"] == "1700000000-baseline"
    # No synthesised tree either: the brake sits before synthesis.
    assert list(exports.iterdir()) == []


def test_brake_does_not_trip_below_the_fraction(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_delete_fraction=0.05)
    snaps, exports, cursor = _seed_baseline(settings, 100)

    # 2 of 100 gone: an ordinary deletion, which must propagate.
    _run_cycle(settings, _FakeClient(_rows(98)), snaps, exports, cursor)

    assert len(list(settings.spool_dir.glob("*.tar.zst"))) == 1
    assert json.loads(cursor.read_text())["last_cycle_id"] != "1700000000-baseline"


def test_brake_trips_exactly_above_the_boundary_and_not_at_it(tmp_path: Path) -> None:
    """The limit is a ceiling that is itself allowed: 5 of 100 passes, 6 does not."""
    for name, remaining, expected_archives in (("at", 95, 1), ("over", 94, 0)):
        settings = _settings(tmp_path / name, max_delete_fraction=0.05)
        snaps, exports, cursor = _seed_baseline(settings, 100)
        _run_cycle(settings, _FakeClient(_rows(remaining)), snaps, exports, cursor)
        shipped = len(list(settings.spool_dir.glob("*.tar.zst")))
        assert shipped == expected_archives, f"{remaining} remaining"


def test_brake_is_disabled_at_one(tmp_path: Path) -> None:
    """1.0 lets a cycle delete the whole mirror, for an operator who means it."""
    settings = _settings(tmp_path, max_delete_fraction=1.0)
    snaps, exports, cursor = _seed_baseline(settings, 100)

    # The source enumerated empty: every artifact removed.
    _run_cycle(settings, _FakeClient([]), snaps, exports, cursor)

    assert len(list(settings.spool_dir.glob("*.tar.zst"))) == 1
    assert json.loads(cursor.read_text())["last_cycle_id"] != "1700000000-baseline"


def test_brake_refuses_any_deletion_at_zero(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_delete_fraction=0.0)
    snaps, exports, cursor = _seed_baseline(settings, 100)

    _run_cycle(settings, _FakeClient(_rows(99)), snaps, exports, cursor)

    assert list(settings.spool_dir.glob("*.tar.zst")) == []
    assert json.loads(cursor.read_text())["last_cycle_id"] == "1700000000-baseline"


def test_brake_is_inert_on_a_cold_start(tmp_path: Path) -> None:
    """With no baseline the sender emits no removals at all, so the brake has
    nothing to weigh and an empty source must not stall the first cycle."""
    settings = _settings(tmp_path, max_delete_fraction=0.05)
    snaps = settings.state_dir / "snapshots"
    exports = settings.state_dir / "exports"
    for p in (snaps, exports):
        p.mkdir(parents=True, exist_ok=True)
    cursor = settings.state_dir / "cursor.json"

    client = _FakeClient(_rows(3))
    _run_cycle(settings, client, snaps, exports, cursor)

    assert client.enumerations == 1
    # Three added entries with no blobs in the store: the cycle still runs and
    # writes its snapshot, which is the point being pinned here.
    assert len(list(snaps.glob("*.jsonl"))) == 1


def test_brake_ignores_a_cycle_with_no_removals(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_delete_fraction=0.0)
    snaps, exports, cursor = _seed_baseline(settings, 100)

    _run_cycle(settings, _FakeClient(_rows(100)), snaps, exports, cursor)

    # Nothing changed, so no archive, but the cursor rolls forward.
    assert list(settings.spool_dir.glob("*.tar.zst")) == []
    assert json.loads(cursor.read_text())["last_cycle_id"] != "1700000000-baseline"

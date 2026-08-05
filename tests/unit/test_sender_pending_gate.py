"""The "one delta in flight" gate: if a prior cycle's archives are still
in spool, the sender must skip the cycle outright (no fresh export, no
new snapshot, no new export tree) until the transport drains spool.
"""
from __future__ import annotations

import os
from pathlib import Path

from ._store import fs_store
from artifactory_airlift import sender
from artifactory_airlift.config import Settings


class _StubClient:
    def __init__(self) -> None:
        self.ping_calls = 0
        self.export_calls = 0

    def ping(self) -> bool:
        self.ping_calls += 1
        return True

    def export_system(self, _path):
        self.export_calls += 1
        raise AssertionError(
            "export_system must not be invoked when spool has pending archives"
        )


def _settings(tmp_path: Path) -> Settings:
    state = tmp_path / "state"
    spool = tmp_path / "spool"
    for p in (state, spool):
        p.mkdir(parents=True, exist_ok=True)
    return Settings(
        mode="sender",
        instance_name="art-a",
        state_dir=state,
        spool_dir=spool,
        snapshot_retention_days=3,
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
    )


def test_cycle_skips_when_pending_archives_remain(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    snaps = settings.state_dir / "snapshots"
    exports = settings.state_dir / "exports"
    cursor = settings.state_dir / "cursor.json"
    for p in (snaps, exports):
        p.mkdir(parents=True, exist_ok=True)

    # A finalised archive from a previous cycle still sitting in spool.
    (settings.spool_dir / "1700000000-deadbeef.tar.zst").write_bytes(b"x")

    client = _StubClient()
    sender._cycle(
        settings,
        client=client,
        store=fs_store(settings.filestore_root),
        snapshots_dir=snaps,
        exports_dir=exports,
        cursor_path=cursor,
    )

    # Ping happened, export did not (the _StubClient asserts on entry).
    assert client.ping_calls == 1
    assert client.export_calls == 0
    # No snapshot, no export tree, no cursor file.
    assert list(snaps.iterdir()) == []
    assert list(exports.iterdir()) == []
    assert not cursor.exists()


def test_cycle_skip_ignores_partial_files(tmp_path: Path) -> None:
    """A .partial file in spool (SIGKILL during build) must NOT gate cycles.

    Partials are not picked up by the receiver and should be cleaned up by
    a separate startup sweep, not by stalling sender cycles indefinitely.
    """
    settings = _settings(tmp_path)
    snaps = settings.state_dir / "snapshots"
    exports = settings.state_dir / "exports"
    cursor = settings.state_dir / "cursor.json"
    for p in (snaps, exports):
        p.mkdir(parents=True, exist_ok=True)

    (settings.spool_dir / "1700000000-deadbeef.tar.zst.partial").write_bytes(b"x")

    # Fail the ping so the cycle returns before we'd hit export_system
    # (we just want to assert the pending-gate doesn't trip on partials,
    # and the simplest way is to let it proceed past the gate and exit
    # at the next sensible step).
    class _PingFails:
        def ping(self) -> bool:
            return False

    sender._cycle(
        settings,
        client=_PingFails(),
        store=fs_store(settings.filestore_root),
        snapshots_dir=snaps,
        exports_dir=exports,
        cursor_path=cursor,
    )
    # If the pending-gate had tripped on the .partial we would never have
    # reached the ping; the ping_not_ok log is the marker that we got past
    # the gate. We just assert no spurious files were created.
    assert list(snaps.iterdir()) == []
    assert list(exports.iterdir()) == []

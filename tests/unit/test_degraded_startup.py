"""Airlift must never take Artifactory's pod down with it.

The sidecar shares a pod with Artifactory, so a process that exits is a
process that crashloops, and a crashlooping container keeps the pod out of
Ready and blocks the StatefulSet rollout. Every failure airlift can have is
therefore either retried in the cycle loop or parked on, never fatal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from artifactory_airlift import __main__ as entry
from artifactory_airlift import binarystore, receiver, sender
from artifactory_airlift.artifactory_client import ArtifactoryClient
from artifactory_airlift.config import Settings


class _Stop(Exception):
    """Breaks out of an otherwise infinite loop under test."""


def _sleeper(limit: int):
    """A time.sleep stand-in that stops the loop after ``limit`` calls."""
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) >= limit:
            raise _Stop

    return sleep, calls


class _FakeStore:
    kind = "fake"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def close(self) -> None:
        return None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        spool_dir=tmp_path / "spool",
        cycle_seconds=10,
    )


@pytest.fixture(autouse=True)
def _no_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ArtifactoryClient, "from_settings", classmethod(lambda cls, s: _FakeClient())
    )


def _flaky_acquire(fail_times: int, seen: list[int]):
    """Fail the first ``fail_times`` attempts, then hand back a store."""
    store = _FakeStore()

    def acquire(settings, *, component, attempt=1, probe=True):
        seen.append(attempt)
        return None if attempt <= fail_times else store

    return acquire, store


def test_sender_retries_an_unusable_binarystore(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    seen: list[int] = []
    acquire, store = _flaky_acquire(2, seen)
    monkeypatch.setattr(binarystore, "acquire", acquire)

    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    sleep, _ = _sleeper(3)
    monkeypatch.setattr(sender.time, "sleep", sleep)

    with pytest.raises(_Stop):
        sender._loop(
            settings,
            snapshots_dir=settings.state_dir / "snapshots",
            exports_dir=settings.state_dir / "exports",
            cursor_path=settings.state_dir / "cursor.json",
        )

    # Two idle cycles, then work resumes without a restart. The attempt
    # counter keeps climbing so the logs show one problem, not many.
    assert seen == [1, 2, 3]
    assert cycles == [1]
    assert store.closed is True


def test_receiver_retries_an_unusable_binarystore(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    seen: list[int] = []
    acquire, store = _flaky_acquire(2, seen)
    monkeypatch.setattr(binarystore, "acquire", acquire)

    cycles: list[int] = []
    monkeypatch.setattr(receiver, "_cycle", lambda *a, **k: cycles.append(1))
    sleep, _ = _sleeper(3)
    monkeypatch.setattr(receiver.time, "sleep", sleep)

    with pytest.raises(_Stop):
        receiver._loop(
            settings,
            processed_path=settings.state_dir / "processed.jsonl",
            done_dir=settings.spool_dir / ".done",
        )

    assert seen == [1, 2, 3]
    assert cycles == [1]
    assert store.closed is True


def test_sender_does_not_probe_the_source_store(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The sender only reads, so it must not require a writable source.

    probe() writes a marker object; demanding it would break a sender running
    with a read-only credential on the source binarystore.
    """
    probes: list[bool] = []

    def acquire(settings, *, component, attempt=1, probe=True):
        probes.append(probe)
        return _FakeStore()

    monkeypatch.setattr(binarystore, "acquire", acquire)
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: None)
    sleep, _ = _sleeper(1)
    monkeypatch.setattr(sender.time, "sleep", sleep)

    with pytest.raises(_Stop):
        sender._loop(
            settings,
            snapshots_dir=settings.state_dir / "snapshots",
            exports_dir=settings.state_dir / "exports",
            cursor_path=settings.state_dir / "cursor.json",
        )

    assert probes == [False]


def test_invalid_config_parks_instead_of_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings error is unrecoverable, but still must not exit."""

    def boom() -> Settings:
        raise ValueError("cycle_seconds must be positive")

    monkeypatch.setattr(entry.config, "load", boom)
    sleep, calls = _sleeper(1)
    monkeypatch.setattr(entry.time, "sleep", sleep)

    # _Stop rather than ValueError: the error was logged on a loop, not raised
    # out of main() where it would end the process.
    with pytest.raises(_Stop):
        entry.main()
    assert calls == [entry._PARK_INTERVAL_SECONDS]


def test_unhandled_error_in_run_parks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a bug in airlift leaves Artifactory's pod alone."""
    monkeypatch.setattr(entry.config, "load", lambda: Settings(mode="sender"))
    sleep, calls = _sleeper(1)
    monkeypatch.setattr(entry.time, "sleep", sleep)

    def explode(settings):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sender, "run", explode)

    with pytest.raises(_Stop):
        entry.main()
    assert calls == [entry._PARK_INTERVAL_SECONDS]

"""A changed config file is applied in place, and SIGTERM ends the process.

Restarting the pod to apply a config change restarts Artifactory with it, so
the daemon watches its own config file between cycles and replaces itself
when the content changes. These tests stop at the point of replacement:
``reload.reexec`` is stubbed, since a real exec would replace the test runner.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from artifactory_airlift import __main__ as entry
from artifactory_airlift import binarystore, config, receiver, reload, sender, state
from artifactory_airlift.artifactory_client import ArtifactoryClient
from artifactory_airlift.config import Settings


class _Stop(Exception):
    """Breaks out of an otherwise infinite loop under test."""


class _Exec(BaseException):
    """Stands in for os.execv, which would replace the test process."""


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        f"spool_dir: {tmp_path / 'spool'}\n"
        "cycle_seconds: 30\n"
    )
    monkeypatch.setattr(config, "DEFAULT_PATH", path)
    # Keep a developer's own AIRLIFT_* environment out of the comparison.
    for key in list(os.environ):
        if key.startswith("AIRLIFT_"):
            monkeypatch.delenv(key)
    return path


@pytest.fixture(autouse=True)
def _restore_sigterm():
    saved = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, saved)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(reload.time, "sleep", calls.append)
    return calls


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    seen: list[int] = []

    def fake() -> None:
        seen.append(1)
        raise _Exec

    monkeypatch.setattr(reload, "reexec", fake)
    return seen


# -- the watcher --------------------------------------------------------------


def test_unchanged_file_does_nothing(cfg: Path) -> None:
    watcher = reload.ConfigWatcher()
    watcher.check(config.load())


def test_a_real_change_requests_a_reload(cfg: Path, capsys) -> None:
    watcher = reload.ConfigWatcher()
    current = config.load()
    cfg.write_text(cfg.read_text().replace("cycle_seconds: 30", "cycle_seconds: 60"))

    with pytest.raises(reload.ReloadRequested):
        watcher.check(current)
    assert "cycle_seconds" in capsys.readouterr().out


def test_a_cosmetic_change_does_not_reload(cfg: Path, capsys) -> None:
    watcher = reload.ConfigWatcher()
    current = config.load()
    cfg.write_text("# a comment\n" + cfg.read_text())

    watcher.check(current)
    watcher.check(current)
    # Judged once, not every poll.
    assert capsys.readouterr().out.count("config_reload_noop") == 1


def test_a_change_masked_by_an_env_var_does_not_reload(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIRLIFT_CYCLE_SECONDS", "45")
    watcher = reload.ConfigWatcher()
    current = config.load()
    cfg.write_text(cfg.read_text().replace("cycle_seconds: 30", "cycle_seconds: 60"))

    watcher.check(current)


def test_an_invalid_change_keeps_running_then_a_fix_reloads(cfg: Path, capsys) -> None:
    watcher = reload.ConfigWatcher()
    current = config.load()
    good = cfg.read_text()

    cfg.write_text(good.replace("cycle_seconds: 30", "cycle_seconds: 1"))
    watcher.check(current)
    watcher.check(current)
    out = capsys.readouterr().out
    assert out.count("config_reload_rejected") == 1

    cfg.write_text(good.replace("cycle_seconds: 30", "cycle_seconds: 90"))
    with pytest.raises(reload.ReloadRequested):
        watcher.check(current)


def test_unparseable_yaml_is_rejected_not_fatal(cfg: Path) -> None:
    watcher = reload.ConfigWatcher()
    current = config.load()
    cfg.write_text("cycle_seconds: [unclosed\n")
    watcher.check(current)


def test_a_vanished_file_keeps_the_running_settings(cfg: Path) -> None:
    """A missing file loads as all defaults; reloading into those would be wrong."""
    watcher = reload.ConfigWatcher()
    current = config.load()
    cfg.unlink()
    watcher.check(current)
    watcher.check(current)


def test_a_file_that_appears_is_a_change(cfg: Path) -> None:
    content = cfg.read_text()
    cfg.unlink()
    watcher = reload.ConfigWatcher()
    current = config.load()
    cfg.write_text(content)
    with pytest.raises(reload.ReloadRequested):
        watcher.check(current)


def test_wait_polls_in_slices(cfg: Path, no_sleep: list[float]) -> None:
    watcher = reload.ConfigWatcher()
    watcher.wait(12, config.load())
    assert no_sleep == [5.0, 5.0, 2.0]


def test_wait_returns_early_on_a_change(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = reload.ConfigWatcher()
    current = config.load()
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) == 2:
            cfg.write_text(cfg.read_text().replace("30", "40"))

    monkeypatch.setattr(reload.time, "sleep", sleep)
    with pytest.raises(reload.ReloadRequested):
        watcher.wait(300, current)
    assert len(calls) == 2


def test_changed_fields_names_only() -> None:
    a = Settings(artifactory_token="old", cycle_seconds=30)
    b = Settings(artifactory_token="new", cycle_seconds=30)
    assert reload.changed_fields(a, b) == ["artifactory_token"]


# -- the daemon loops ---------------------------------------------------------


class _FakeStore:
    kind = "fake"

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("mod", [sender, receiver], ids=["sender", "receiver"])
def test_loop_unwinds_and_releases_the_lock(
    mod, cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reload happens between cycles, and leaves nothing held behind."""
    store, client = _FakeStore(), _FakeClient()
    monkeypatch.setattr(binarystore, "acquire", lambda *a, **k: store)
    monkeypatch.setattr(
        ArtifactoryClient, "from_settings", classmethod(lambda cls, s: client)
    )
    cycles: list[int] = []
    monkeypatch.setattr(mod, "_cycle", lambda *a, **k: cycles.append(1))

    def sleep(seconds: float) -> None:
        if len(cycles) == 2:
            cfg.write_text(cfg.read_text().replace("30", "40"))
        if len(cycles) > 3:
            raise _Stop

    monkeypatch.setattr(reload.time, "sleep", sleep)

    settings = config.load()
    watcher = reload.ConfigWatcher()
    with pytest.raises(reload.ReloadRequested):
        mod.run(settings, watcher=watcher)

    assert cycles == [1, 1]
    assert store.closed and client.closed
    lock = settings.state_dir / f"{mod.__name__.rsplit('.', 1)[1]}.lock"
    with state.file_lock(lock):
        pass


def test_main_reexecs_on_reload(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, execs: list[int]
) -> None:
    def run(settings, *, watcher=None):
        assert watcher is not None and watcher.path == cfg
        raise reload.ReloadRequested

    monkeypatch.setattr(sender, "run", run)
    with pytest.raises(_Exec):
        entry.main()
    assert execs == [1]


def test_parked_on_invalid_config_recovers_when_the_file_is_fixed(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, execs: list[int]
) -> None:
    good = cfg.read_text()
    cfg.write_text(good.replace("cycle_seconds: 30", "cycle_seconds: 1"))
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 3:
            cfg.write_text(good)

    monkeypatch.setattr(reload.time, "sleep", sleep)
    with pytest.raises(_Exec):
        entry.main()
    assert execs == [1]


# -- SIGTERM ------------------------------------------------------------------


def test_sigterm_ends_main_cleanly(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def run(settings, *, watcher=None):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt the run loop")

    monkeypatch.setattr(sender, "run", run)
    assert entry.main() == 0
    assert "Received SIGTERM" in capsys.readouterr().out


def test_sigterm_ends_a_parked_process(
    monkeypatch: pytest.MonkeyPatch, cfg: Path
) -> None:
    cfg.write_text("cycle_seconds: 1\n")

    def sleep(seconds: float) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(reload.time, "sleep", sleep)
    assert entry.main() == 0


def test_sigterm_is_not_swallowed_by_a_cycle_handler(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cycle loops catch Exception to survive a bad cycle; SIGTERM must
    still get through them."""
    monkeypatch.setattr(binarystore, "acquire", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(
        ArtifactoryClient, "from_settings", classmethod(lambda cls, s: _FakeClient())
    )

    def cycle(*a, **k):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(sender, "_cycle", cycle)
    assert entry.main() == 0


# -- re-exec ------------------------------------------------------------------


def test_reexec_keeps_the_original_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(reload.os, "execv", lambda path, argv: seen.append((path, argv)))
    monkeypatch.setattr(reload.sys, "orig_argv", ["python", "-m", "artifactory_airlift"])
    reload.reexec()
    assert seen == [(reload.sys.executable, ["python", "-m", "artifactory_airlift"])]

"""Operator control of the daemon: stop, start, run and ship.

The loops are driven with a stubbed sleep that plays the operator: on chosen
sleeps it writes or removes a marker, exactly as the CLI would, and then
the test asserts what the daemon did with it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from artifactory_airlift import binarystore, control, receiver, reload, sender, shipping
from artifactory_airlift.artifactory_client import ArtifactoryClient
from artifactory_airlift.cli import actions, app, views
from artifactory_airlift.cli.common import CLIError
from artifactory_airlift.config import Settings


class _Stop(Exception):
    pass


class _FakeStore:
    kind = "fake"

    def close(self) -> None:
        pass


class _FakeClient:
    def close(self) -> None:
        pass


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        state_dir=tmp_path / "state",
        spool_dir=tmp_path / "spool",
        cycle_seconds=20,
    )
    s.state_dir.mkdir(parents=True)
    s.spool_dir.mkdir(parents=True)
    return s


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ArtifactoryClient, "from_settings", classmethod(lambda cls, s: _FakeClient())
    )
    monkeypatch.setattr(binarystore, "acquire", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(actions, "WAIT_POLL_SECONDS", 0.001)


def _drive(monkeypatch, script: dict[int, callable], limit: int) -> list[float]:
    """Stub time.sleep: run ``script[n]`` on the nth sleep, stop after ``limit``."""
    calls: list[float] = []

    def sleep(seconds: float) -> None:
        calls.append(seconds)
        action = script.get(len(calls))
        if action is not None:
            action()
        if len(calls) >= limit:
            raise _Stop

    monkeypatch.setattr(control.time, "sleep", sleep)
    return calls


def _run_sender(settings: Settings, ctl: control.Control) -> None:
    with pytest.raises(_Stop):
        sender._loop(
            settings,
            snapshots_dir=settings.state_dir / "snapshots",
            exports_dir=settings.state_dir / "exports",
            cursor_path=settings.state_dir / "cursor.json",
            ctl=ctl,
        )


def _activity(settings: Settings) -> dict:
    return json.loads(
        (control.control_dir(settings.state_dir) / control.ACTIVITY).read_text()
    )


# -- the wait -----------------------------------------------------------------


def test_wait_without_control_is_one_sleep(monkeypatch, settings) -> None:
    calls = _drive(monkeypatch, {}, limit=99)
    assert control.wait(20, current=settings) == 0.0
    assert calls == [20]


def test_wait_returns_early_with_the_time_left(monkeypatch, settings) -> None:
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup()
    calls = _drive(
        monkeypatch,
        {2: lambda: control.write_marker(settings.state_dir, control.RUN)},
        limit=99,
    )
    assert control.wait(20, current=settings, control=ctl) == 10.0
    assert calls == [5.0, 5.0]


# -- the sender loop ----------------------------------------------------------


def test_stop_holds_scheduled_cycles(monkeypatch, settings) -> None:
    control.write_marker(settings.state_dir, control.STOPPED, reason="maintenance")
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup(fresh=False)
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    _drive(monkeypatch, {}, limit=12)  # three whole intervals

    _run_sender(settings, ctl)
    assert cycles == []
    assert _activity(settings)["state"] == "stopped"


def test_run_while_stopped_runs_once_and_stays_stopped(monkeypatch, settings) -> None:
    control.write_marker(settings.state_dir, control.STOPPED)
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup(fresh=False)
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    _drive(
        monkeypatch,
        {1: lambda: control.write_marker(settings.state_dir, control.RUN)},
        limit=12,
    )

    _run_sender(settings, ctl)
    assert cycles == [1]
    assert not (control.control_dir(settings.state_dir) / control.RUN).exists()
    act = _activity(settings)
    assert act["state"] == "stopped"
    assert act["served"]["run"] is not None


def test_start_resumes_at_once(monkeypatch, settings) -> None:
    control.write_marker(settings.state_dir, control.STOPPED)
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup(fresh=False)
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    stopped = control.control_dir(settings.state_dir) / control.STOPPED
    # Removed on the first sleep of a 20 s wait: the cycle must run after
    # that slice, not after the other three.
    calls = _drive(monkeypatch, {1: stopped.unlink}, limit=2)

    _run_sender(settings, ctl)
    assert cycles == [1]
    assert calls == [5.0, 5.0]


def test_stop_during_the_wait_is_acknowledged(monkeypatch, settings) -> None:
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup()
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    _drive(
        monkeypatch,
        {1: lambda: control.write_marker(settings.state_dir, control.STOPPED)},
        limit=9,
    )

    _run_sender(settings, ctl)
    # The startup cycle, then nothing.
    assert cycles == [1]
    assert _activity(settings)["state"] == "stopped"


def test_ship_runs_the_shipper_without_a_cycle(monkeypatch, settings) -> None:
    settings.shipping.enabled = True
    settings.shipping.azure.account = "acct"
    settings.shipping.azure.container = "c"
    ships: list[int] = []

    class _Shipper:
        def __init__(self, s) -> None:
            pass

        def run_once(self) -> int:
            ships.append(1)
            return 0

        def close(self) -> None:
            pass

    monkeypatch.setattr(shipping, "Shipper", _Shipper)
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup()
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    calls = _drive(
        monkeypatch,
        {1: lambda: control.write_marker(settings.state_dir, control.SHIP)},
        limit=4,
    )

    _run_sender(settings, ctl)
    # Startup: ship, cycle, ship. Then the request: one more ship, no cycle,
    # and the wait carries on for the rest of the interval rather than
    # starting a fresh one.
    assert cycles == [1]
    assert ships == [1, 1, 1]
    assert calls == [5.0, 5.0, 5.0, 5.0]


def test_ship_without_shipping_is_ignored(monkeypatch, settings) -> None:
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup()
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    _drive(
        monkeypatch,
        {1: lambda: control.write_marker(settings.state_dir, control.SHIP)},
        limit=3,
    )
    _run_sender(settings, ctl)
    assert cycles == [1]
    assert not (control.control_dir(settings.state_dir) / control.SHIP).exists()


def test_receiver_honours_stop_and_run(monkeypatch, settings) -> None:
    control.write_marker(settings.state_dir, control.STOPPED)
    ctl = control.Control(settings.state_dir, component="receiver")
    ctl.startup(fresh=False)
    cycles: list[int] = []
    monkeypatch.setattr(receiver, "_cycle", lambda *a, **k: cycles.append(1))
    _drive(
        monkeypatch,
        {5: lambda: control.write_marker(settings.state_dir, control.RUN)},
        limit=12,
    )
    with pytest.raises(_Stop):
        receiver._loop(
            settings,
            processed_path=settings.state_dir / "processed.jsonl",
            done_dir=settings.spool_dir / ".done",
            ctl=ctl,
        )
    assert cycles == [1]


# -- container start versus reload ---------------------------------------------


def test_a_container_start_clears_a_stop(settings) -> None:
    control.write_marker(settings.state_dir, control.STOPPED)
    control.Control(settings.state_dir, component="sender").startup()
    assert not (control.control_dir(settings.state_dir) / control.STOPPED).exists()


def test_a_reload_keeps_a_stop(monkeypatch, settings) -> None:
    control.write_marker(settings.state_dir, control.STOPPED)
    monkeypatch.setenv(reload.REEXEC_ENV, "1")
    control.Control(settings.state_dir, component="sender").startup()
    assert (control.control_dir(settings.state_dir) / control.STOPPED).exists()


def test_reexec_marks_the_environment(monkeypatch) -> None:
    execs: list[list[str]] = []
    monkeypatch.setattr(reload.os, "execv", lambda exe, argv: execs.append(argv))
    # Registers the variable with monkeypatch so teardown removes it again.
    monkeypatch.setenv(reload.REEXEC_ENV, "")
    reload.reexec()
    assert os.environ[reload.REEXEC_ENV] == "1"
    assert execs


def test_the_first_cycle_runs_after_a_container_start(monkeypatch, settings) -> None:
    """The whole path through run(): a stop left by a dead container is gone."""
    control.write_marker(settings.state_dir, control.STOPPED)
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    _drive(monkeypatch, {}, limit=1)
    with pytest.raises(_Stop):
        sender.run(settings)
    assert cycles == [1]


def test_a_reloaded_process_stays_stopped(monkeypatch, settings) -> None:
    control.write_marker(settings.state_dir, control.STOPPED)
    monkeypatch.setenv(reload.REEXEC_ENV, "1")
    cycles: list[int] = []
    monkeypatch.setattr(sender, "_cycle", lambda *a, **k: cycles.append(1))
    _drive(monkeypatch, {}, limit=8)
    with pytest.raises(_Stop):
        sender.run(settings)
    assert cycles == []


# -- the CLI ------------------------------------------------------------------


def test_cli_stop_and_start(settings) -> None:
    data = actions.control_stop(settings, reason="upgrade")
    assert data["already"] is False and data["marker"]["reason"] == "upgrade"
    again = actions.control_stop(settings, reason="other")
    assert again["already"] is True and again["marker"]["reason"] == "upgrade"
    assert views.control_status(settings)["stopped"]["reason"] == "upgrade"

    started = actions.control_start(settings)
    assert started["was_stopped"] is True
    assert views.control_status(settings)["stopped"] is None
    assert actions.control_start(settings)["was_stopped"] is False


def test_cli_request_and_status(settings) -> None:
    data = actions.control_request(settings, "run")
    assert data["replaced_pending"] is False
    assert actions.control_request(settings, "run")["replaced_pending"] is True
    assert views.status(settings, offline=True)["control"]["pending"]["run"] is not None


def test_cli_ship_refuses_without_shipping(settings, monkeypatch, capsys) -> None:
    monkeypatch.setattr(app.common, "load_settings", lambda args: settings)
    assert app.main(["ship"]) == 1
    assert "shipping is not enabled" in capsys.readouterr().err


def test_cli_ship_refuses_on_a_receiver(settings, monkeypatch, capsys) -> None:
    settings.mode = "receiver"
    monkeypatch.setattr(app.common, "load_settings", lambda args: settings)
    assert app.main(["ship"]) == 1
    assert "applies to the sender" in capsys.readouterr().err


def test_wait_for_sees_its_own_request_served(settings, monkeypatch) -> None:
    marker = control.write_marker(settings.state_dir, control.RUN)
    ctl = control.Control(settings.state_dir, component="sender")
    ctl.startup()
    monkeypatch.setattr(actions.common, "daemon_running", lambda s: True)

    # Not yet served: an idle record from before the request does not count.
    ctl.activity("waiting")
    assert actions.wait_for(settings, "run", marker, timeout=0.01)["done"] is False

    ctl.poll()
    ctl.activity("cycle", requested=True)
    assert actions.wait_for(settings, "run", marker, timeout=0.01)["done"] is False
    ctl.activity("waiting")
    assert actions.wait_for(settings, "run", marker, timeout=0.01)["done"] is True


def test_wait_for_stop(settings, monkeypatch) -> None:
    ctl = control.Control(settings.state_dir, component="sender")
    monkeypatch.setattr(actions.common, "daemon_running", lambda s: True)
    ctl.activity("cycle")
    assert actions.wait_for(settings, "stop", {}, timeout=0.01)["done"] is False
    ctl.activity("stopped")
    assert actions.wait_for(settings, "stop", {}, timeout=0.01)["done"] is True

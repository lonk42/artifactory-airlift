"""Operator control of a running daemon: stop, start, run and ship.

The CLI runs as a separate process and cannot take the daemon's state lock,
which the daemon holds for its whole life. So the two talk through small
marker files under ``state/control/``:

``stopped.json``
    Present while scheduled work is stopped. ``airlift stop`` writes it,
    ``airlift start`` removes it.
``run.json`` / ``ship.json``
    One-off requests. The daemon consumes each one (rename, then read) and
    acts on it, so a request is honoured once however long it waited.
``activity.json``
    Written by the daemon: what it is doing now and since when. Best-effort,
    and only meaningful while the daemon holds its lock.

The daemon checks for requests between cycles, while it waits, every
:data:`reload.POLL_SECONDS`, so a request lands within a few seconds unless a
cycle is in progress, in which case it lands as soon as the cycle ends.
Nothing interrupts a cycle.

A stop never survives a container start. Kubernetes restarts a container to
recover it, and a sidecar that silently stays idle after that is worse than
one that does its job, so a fresh process removes ``stopped.json`` and runs
as usual. A config reload replaces the process in place (same container), and
that does keep the stop: :func:`reload.reexec` marks the environment so the
new process can tell the difference.
"""

from __future__ import annotations

import getpass
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import log, reload, state
from .config import Settings

logger = log.get("artifactory_airlift.control")

# Set by reload.reexec() just before it replaces the process.
REEXEC_ENV = reload.REEXEC_ENV

STOPPED = "stopped.json"
RUN = "run.json"
SHIP = "ship.json"
ACTIVITY = "activity.json"


def control_dir(state_dir: Path) -> Path:
    return state_dir / "control"


def read_marker(path: Path) -> dict[str, Any] | None:
    """A marker's content, ``{}`` if it exists but cannot be read, else None.

    An unreadable stop marker still means stopped: failing safe here costs an
    idle cycle, failing open costs work the operator asked not to happen.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def requester() -> str:
    """Who is asking, as well as a container can tell."""
    try:
        return getpass.getuser()
    except Exception:
        return f"uid {os.getuid()}"


def write_marker(state_dir: Path, name: str, *, reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"at": time.time(), "by": requester()}
    if reason:
        payload["reason"] = reason
    directory = control_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(directory / name, payload)
    return payload


def is_reexec() -> bool:
    return bool(os.environ.get(REEXEC_ENV))


@dataclass
class Requests:
    """What the operator asked for since the last look."""

    run: dict[str, Any] | None = None
    ship: dict[str, Any] | None = None
    stopped: dict[str, Any] | None = None
    resumed: bool = False


class Control:
    """The daemon's side of the markers."""

    def __init__(self, state_dir: Path, *, component: str) -> None:
        self.dir = control_dir(state_dir)
        self.component = component
        self._stopped: dict[str, Any] | None = None
        # Set when a stale stop could not be removed at a fresh start.
        self._ignore_stop = False
        # The ``at`` of the last run and ship request taken, echoed in every
        # activity record so ``--wait`` can tell its own request was served.
        self._served: dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------

    def startup(self, *, fresh: bool | None = None) -> None:
        """Clear a stop left over from a previous container, keep one across a reload."""
        if fresh is None:
            fresh = not is_reexec()
        self.dir.mkdir(parents=True, exist_ok=True)
        marker = read_marker(self.dir / STOPPED)
        if marker is None:
            return
        if not fresh:
            # The previous process in this container was stopped; carry on
            # being stopped and say so once.
            self._stopped = marker
            logger.info("control.still_stopped", **who(marker))
            return
        try:
            (self.dir / STOPPED).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Left in place it would keep a fresh container idle, which is
            # exactly what a start must never do; ignore it for this process.
            logger.warning("control.stop_clear_failed", error=str(exc))
            self._ignore_stop = True
        logger.info("control.stop_cleared_on_start", **who(marker))

    # -- requests -----------------------------------------------------------

    def _stop_marker(self) -> dict[str, Any] | None:
        if self._ignore_stop:
            return None
        return read_marker(self.dir / STOPPED)

    def has_work(self) -> bool:
        """Whether something needs acting on before the wait runs out."""
        if (self.dir / RUN).exists() or (self.dir / SHIP).exists():
            return True
        # A start resumes straight away rather than at the end of whatever
        # wait was in progress, and a stop is acknowledged straight away.
        return (self._stopped is None) != (self._stop_marker() is None)

    def idle_state(self) -> str:
        """What the daemon is doing between cycles: ``stopped`` or ``waiting``."""
        return "stopped" if self._stop_marker() is not None else "waiting"

    def poll(self) -> Requests:
        """Consume pending one-off requests and report the stop state."""
        req = Requests(run=self._take(RUN), ship=self._take(SHIP))
        for kind, marker in (("run", req.run), ("ship", req.ship)):
            if marker is not None:
                self._served[kind] = marker.get("at")
        current = self._stop_marker()
        if current is not None and self._stopped is None:
            logger.info("control.stopped", **who(current))
        elif current is None and self._stopped is not None:
            req.resumed = True
            logger.info("control.started")
        self._stopped = current
        req.stopped = current
        return req

    def _take(self, name: str) -> dict[str, Any] | None:
        path = self.dir / name
        claimed = path.with_name(f".{name}.taken-{os.getpid()}")
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("control.request_unreadable", request=name, error=str(exc))
            return None
        marker = read_marker(claimed) or {}
        try:
            claimed.unlink()
        except OSError:
            pass
        return marker

    # -- activity -----------------------------------------------------------

    def activity(self, what: str, **fields: Any) -> None:
        """Record what the daemon is doing, for ``airlift status`` and ``--wait``."""
        payload = {"state": what, "since": time.time(), "pid": os.getpid(), **fields}
        if self._served:
            payload["served"] = dict(self._served)
        try:
            state.write_json_atomic(self.dir / ACTIVITY, payload)
        except OSError:
            # A breadcrumb must never fail the daemon.
            pass


def note(ctl: Control | None, what: str, **fields: Any) -> None:
    """:meth:`Control.activity`, for loops that may run without a Control."""
    if ctl is not None:
        ctl.activity(what, **fields)


def idle(ctl: Control | None) -> str:
    return ctl.idle_state() if ctl is not None else "waiting"


def who(marker: dict[str, Any], *, reason: bool = True) -> dict[str, Any]:
    """Log fields naming who left a marker and when."""
    out: dict[str, Any] = {
        "by": marker.get("by") or "unknown",
        "since": _ts(marker.get("at")),
    }
    if reason:
        out["reason"] = marker.get("reason") or "none given"
    return out


def _ts(epoch: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(epoch)))
    except (TypeError, ValueError):
        return "unknown"


def wait(
    seconds: float,
    *,
    current: Settings,
    watcher: reload.ConfigWatcher | None = None,
    control: Control | None = None,
) -> float:
    """The cycle loops' sleep. Returns the seconds left when woken early, else 0.

    Polls in :data:`reload.POLL_SECONDS` slices, checking the config file
    before each slice (as :meth:`reload.ConfigWatcher.wait` does) and control
    requests after it. Checking control only after a sleep means a request the
    daemon fails to consume costs one slice per look, never a busy loop.

    Time left is counted down per slice rather than read from a clock, so a
    long cycle does not eat into the next wait and tests can stub the sleep.
    """
    left = float(seconds)
    if left <= 0:
        return 0.0
    if watcher is None and control is None:
        time.sleep(left)
        return 0.0
    while left > 0:
        if watcher is not None:
            watcher.check(current)
        step = min(reload.POLL_SECONDS, left)
        time.sleep(step)
        left -= step
        if control is not None and left > 0 and control.has_work():
            return left
    return 0.0

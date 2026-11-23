"""Apply a changed config file without restarting the container.

Airlift shares a pod with Artifactory, and restarting the pod to pick up a
config change can take Artifactory down for as long as it takes to
reschedule. The ConfigMap is a directory mount, so the kubelet updates the
file in place while the sidecar runs; all that is missing is noticing.

The daemon checks the file between cycles, never inside one. When the content
changes and parses, :class:`ReloadRequested` unwinds the loop to ``main()``,
releasing the state lock and closing clients on the way, and :func:`reexec`
replaces the process with a fresh one. The PID stays the same, so Kubernetes
sees neither a container restart nor a moment of NotReady, and every setting
takes effect the same way it would at startup, including URLs, credentials
and the binarystore.

A change that does not parse is logged and ignored: the running settings stay
in force, so a typo in the ConfigMap never stops a working sidecar.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import time
from pathlib import Path
from typing import NoReturn

from . import config, log
from .config import Settings

logger = log.get("artifactory_airlift.reload")

# How often the file is checked while waiting between cycles. Reading a small
# file every few seconds costs nothing, and a short interval means a change
# lands well inside the kubelet's own propagation delay.
POLL_SECONDS = 5.0

# Set by reexec() so the replacement process can tell a reload from a
# container start. Deliberately not AIRLIFT_-prefixed, so the settings loader
# never sees it.
REEXEC_ENV = "_AIRLIFT_REEXECED"


class ReloadRequested(BaseException):
    """Unwinds the daemon to ``main()``, which replaces the process.

    A BaseException so the ``except Exception`` handlers around each cycle
    cannot swallow it, while ``finally`` blocks (the state lock, open clients)
    still run on the way out.
    """


def fingerprint(path: Path) -> str | None:
    """Hash of the file's content, or None if it cannot be read.

    Content rather than mtime: the kubelet updates a ConfigMap by swapping a
    symlink, and an mtime says nothing about whether the bytes differ.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def changed_fields(old: Settings, new: Settings) -> list[str]:
    """Names of the top-level settings that differ. Names only, never values,
    since several of them are credentials."""
    return [
        name
        for name in type(old).model_fields
        if getattr(old, name) != getattr(new, name)
    ]


class ConfigWatcher:
    """Tracks the config file and decides when a change warrants a reload."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.DEFAULT_PATH
        # Taken before main() loads the settings, so a change landing between
        # the two is seen as a change (at worst one reload that changes
        # nothing) rather than missed.
        self._seen = fingerprint(self.path)
        self._missing_reported = False

    def check(self, current: Settings | None) -> None:
        """Raise ReloadRequested if the file now holds different, valid settings.

        ``current`` is what the process is running with; None means it is
        parked without usable settings, so any valid file is worth a reload.
        Each version of the file is judged once, so a rejected edit is logged
        once rather than every poll.
        """
        fp = fingerprint(self.path)
        if fp is None:
            # A vanished file would otherwise load as all defaults and reload
            # into them. Keep what is running; a file that comes back is a
            # change like any other.
            if self._seen is not None and not self._missing_reported:
                logger.warning("config_file_missing", path=str(self.path))
                self._missing_reported = True
            return
        self._missing_reported = False
        if fp == self._seen:
            return
        self._seen = fp

        try:
            new = config.load(self.path)
        except Exception as exc:
            logger.warning(
                "config_reload_rejected", error=" ".join(str(exc).split())[:500]
            )
            return

        if current is None:
            logger.info("config_reload", changed="(was not running)")
            raise ReloadRequested
        changed = changed_fields(current, new)
        if not changed:
            # Comments, ordering, or a key that an env var overrides anyway.
            logger.info("config_reload_noop")
            return
        logger.info("config_reload", changed=", ".join(changed))
        raise ReloadRequested

    def wait(self, seconds: float, current: Settings | None) -> None:
        """Sleep for ``seconds``, checking the file every POLL_SECONDS."""
        slices = max(1, math.ceil(seconds / POLL_SECONDS))
        for i in range(slices):
            self.check(current)
            time.sleep(min(POLL_SECONDS, seconds - i * POLL_SECONDS))


def reexec() -> NoReturn:
    """Replace this process with a fresh airlift, keeping the PID.

    The environment is inherited unchanged, so env vars still outrank the
    file exactly as they did. File descriptors are close-on-exec by default
    in Python, so nothing leaks into the new image.

    The one addition is a marker telling the new process it is a reload and
    not a container start, which decides whether an operator's stop holds
    (see :mod:`artifactory_airlift.control`).
    """
    os.environ[REEXEC_ENV] = "1"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    argv = list(getattr(sys, "orig_argv", None) or [])
    if not argv:
        argv = [sys.executable, "-m", "artifactory_airlift"]
    os.execv(sys.executable, argv)

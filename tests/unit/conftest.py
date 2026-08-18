"""Isolate the unit tests from the ambient environment.

Settings read ``AIRLIFT_*`` environment variables, and those outrank the
values a test passes in as init kwargs (see ``Settings.settings_customise_sources``).
So a shell that has any of them exported, which is exactly the shell an
operator debugging a live sidecar is working in, silently redirects tests at
real state directories and a real Artifactory URL.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_airlift_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("AIRLIFT_"):
            monkeypatch.delenv(name, raising=False)

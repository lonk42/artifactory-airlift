"""Fixtures for end-to-end tests against the live artifactory-a / -b cluster.

These tests are off by default; they require a real cluster reachable
from the host running pytest. Enable with:

    E2E=1 pytest tests/e2e -q

Required env vars:
  ART_A_URL                  base URL of the source Artifactory, e.g. https://artifactory-a.example.com/artifactory
  ART_A_TOKEN                admin access token on instance A
  ART_B_URL                  base URL of the destination Artifactory, e.g. https://artifactory-b.example.com/artifactory
  ART_B_TOKEN                admin access token on instance B
  E2E_TEST_REPO              repo key to use on both sides (must exist on both); default "generic-local"
  E2E_KUBECTL                kubectl binary; default "kubectl"
  E2E_NS_A / E2E_NS_B        namespaces; default "artifactory-a" / "artifactory-b"
  E2E_POD_SELECTOR           pod selector; default "app=artifactory"
  E2E_CYCLE_SECONDS          airlift cycle seconds; default 60
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx
import pytest


def _env_required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        pytest.skip(f"E2E env var {name} is not set")
    return val


@dataclass(frozen=True)
class ArtClient:
    base_url: str
    token: str

    def http(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=60.0,
            verify=os.environ.get("E2E_VERIFY_TLS", "true").lower() == "true",
        )


def pytest_collection_modifyitems(config, items):  # noqa: D401
    if os.environ.get("E2E") == "1":
        return
    skip = pytest.mark.skip(reason="set E2E=1 to enable end-to-end tests")
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def art_a() -> ArtClient:
    return ArtClient(_env_required("ART_A_URL").rstrip("/"), _env_required("ART_A_TOKEN"))


@pytest.fixture(scope="session")
def art_b() -> ArtClient:
    return ArtClient(_env_required("ART_B_URL").rstrip("/"), _env_required("ART_B_TOKEN"))


@pytest.fixture(scope="session")
def test_repo() -> str:
    return os.environ.get("E2E_TEST_REPO", "generic-local")


@pytest.fixture(scope="session")
def cycle_seconds() -> int:
    return int(os.environ.get("E2E_CYCLE_SECONDS", "60"))


@pytest.fixture(scope="session")
def kube() -> dict[str, str]:
    return {
        "kubectl": os.environ.get("E2E_KUBECTL", "kubectl"),
        "ns_a": os.environ.get("E2E_NS_A", "artifactory-a"),
        "ns_b": os.environ.get("E2E_NS_B", "artifactory-b"),
        "selector": os.environ.get("E2E_POD_SELECTOR", "app=artifactory"),
    }


def wait_until(predicate, *, timeout: float, interval: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None

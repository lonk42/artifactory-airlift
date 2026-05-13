"""Fixtures for end-to-end tests against the live artifactory-a / -b cluster.

These tests are off by default; they require a real cluster reachable
from the host running pytest. Enable with:

    E2E=1 pytest tests/e2e -q

Required env vars (auth: supply either *_TOKEN or both *_USERNAME and *_PASSWORD):
  ART_A_URL                          base URL of the source Artifactory, e.g. https://artifactory-a.example.com/artifactory
  ART_A_TOKEN / ART_A_USERNAME+ART_A_PASSWORD
  ART_B_URL                          base URL of the destination Artifactory
  ART_B_TOKEN / ART_B_USERNAME+ART_B_PASSWORD

Optional env vars:
  E2E_TEST_REPO              legacy single-repo key used by test_roundtrip; default "generic-local"
  E2E_KUBECTL                kubectl binary; default "kubectl"
  E2E_NS_A / E2E_NS_B        namespaces; default "artifactory-a" / "artifactory-b"
  E2E_POD_SELECTOR           pod selector; default "app=artifactory"
  E2E_CYCLE_SECONDS          airlift cycle seconds; default 60

Note: provisioning admin endpoints (PUT /access/api/v1/projects, PUT /api/repositories)
require true admin auth. The legacy bearer-token endpoint produces tokens scoped
member-of-groups:*, which Artifactory rejects on those endpoints; basic auth
with an admin user (e.g. ART_A_USERNAME=admin ART_A_PASSWORD=Password1) is the
reliable path for the provisioning fixtures.
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


def _env_auth_or_skip(prefix: str) -> tuple[str, str, str]:
    """Return (token, username, password). Skips the test if none are configured."""
    token = os.environ.get(f"{prefix}_TOKEN", "")
    username = os.environ.get(f"{prefix}_USERNAME", "")
    password = os.environ.get(f"{prefix}_PASSWORD", "")
    if not token and not (username and password):
        pytest.skip(
            f"E2E auth not configured: set {prefix}_TOKEN or both "
            f"{prefix}_USERNAME and {prefix}_PASSWORD"
        )
    return token, username, password


@dataclass(frozen=True)
class ArtClient:
    base_url: str
    token: str = ""
    username: str = ""
    password: str = ""

    def http(self) -> httpx.Client:
        headers: dict[str, str] = {}
        auth: httpx.BasicAuth | None = None
        # Basic auth wins when both username and password are set; matches the
        # precedence used by src/artifactory_airlift/artifactory_client.py.
        if self.username and self.password:
            auth = httpx.BasicAuth(self.username, self.password)
        elif self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            auth=auth,
            timeout=60.0,
            verify=os.environ.get("E2E_VERIFY_TLS", "true").lower() == "true",
        )

    def ui_session(self) -> httpx.Client:
        """Return an httpx.Client authenticated against the JFrog UI bridge.

        The Access endpoints under /access/api/v1/ reject Artifactory basic auth
        and the legacy bearer tokens we can mint (audience: jfrt@*). The UI
        bridge at /ui/api/v1/ does accept admin basic auth via a login flow
        that sets ACCESSTOKEN cookies scoped to the frontend (jffe@*); those
        cookies pass JFrog's CSRF gate when paired with X-Requested-With.

        Requires username + password (the UI login endpoint does not take
        bearer tokens). Used for project create / read / delete; repo CRUD
        keeps using the regular http() basic-auth client.
        """
        if not (self.username and self.password):
            raise RuntimeError(
                "ui_session requires username + password (Access UI bridge "
                "does not accept bearer tokens)"
            )
        # The UI bridge sits at the same host as the artifactory base_url; strip
        # the /artifactory suffix to get the platform root.
        platform_root = self.base_url.rsplit("/artifactory", 1)[0] or self.base_url
        client = httpx.Client(
            base_url=platform_root,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=60.0,
            verify=os.environ.get("E2E_VERIFY_TLS", "true").lower() == "true",
        )
        r = client.post(
            "/ui/api/v1/ui/auth/login",
            json={"user": self.username, "password": self.password, "type": "login"},
        )
        if r.status_code != 200:
            client.close()
            raise RuntimeError(
                f"UI login failed: {r.status_code} {r.text[:200]}"
            )
        return client


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
    token, username, password = _env_auth_or_skip("ART_A")
    return ArtClient(
        _env_required("ART_A_URL").rstrip("/"),
        token=token,
        username=username,
        password=password,
    )


@pytest.fixture(scope="session")
def art_b() -> ArtClient:
    token, username, password = _env_auth_or_skip("ART_B")
    return ArtClient(
        _env_required("ART_B_URL").rstrip("/"),
        token=token,
        username=username,
        password=password,
    )


@pytest.fixture(scope="session")
def test_repo() -> str:
    # example-repo-local is the default repo that ships with Artifactory;
    # generic-local does not exist on a fresh cluster.
    return os.environ.get("E2E_TEST_REPO", "example-repo-local")


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


# The set of repos the e2e battery and the soak scripts expect to find on both
# sides. Project key is "airlift" (lowercase, <=8 chars). Repos assigned to a
# project must be prefixed with the project key, which all the project-scoped
# entries below satisfy.
PROJECT_KEY = "airlift"
PROJECT_DISPLAY_NAME = "Airlift e2e fixtures"

PROVISION_SPECS: tuple[tuple[str, str, str | None], ...] = (
    ("airlift-rpm-local", "rpm", PROJECT_KEY),
    ("airlift-deb-local", "debian", PROJECT_KEY),
    ("airlift-helm-local", "helm", PROJECT_KEY),
    ("airlift-pypi-local", "pypi", PROJECT_KEY),
    ("airlift-npm-local", "npm", PROJECT_KEY),
    ("airlift-npm-test-local", "npm", None),
)


@pytest.fixture(scope="session")
def provisioned_repos(art_a, art_b):
    from .provisioning import (
        ProjectsUnavailable,
        ProvisionedRepo,
        ensure_local_repo,
        ensure_project,
    )

    for client, label in ((art_a, "A"), (art_b, "B")):
        try:
            ensure_project(client, PROJECT_KEY, PROJECT_DISPLAY_NAME)
        except ProjectsUnavailable as exc:
            pytest.skip(f"projects unavailable on instance {label}: {exc}")

    repos = [
        ProvisionedRepo(key=key, package_type=ptype, project=proj)
        for key, ptype, proj in PROVISION_SPECS
    ]
    for repo in repos:
        for client in (art_a, art_b):
            ensure_local_repo(client, repo.key, repo.package_type, repo.project)
    return repos

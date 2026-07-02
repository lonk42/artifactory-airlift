"""Unit coverage for ArtifactoryClient construction (no network).

Focus is the TLS-verify resolution added for private/self-signed CAs:
``from_settings`` turns ``artifactory_ca_cert`` into the httpx ``verify``
value, and ``__init__`` passes it to both underlying httpx clients.
"""

from artifactory_airlift import artifactory_client as ac
from artifactory_airlift.artifactory_client import ArtifactoryClient
from artifactory_airlift.config import Settings


class _FakeClient:
    """Records the kwargs it was built with; stands in for httpx.Client."""

    instances: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeClient.instances.append(kwargs)

    def close(self) -> None:
        pass


def _patch_httpx(monkeypatch) -> None:
    _FakeClient.instances = []
    monkeypatch.setattr(ac.httpx, "Client", _FakeClient)


def test_from_settings_ca_cert_becomes_verify(monkeypatch) -> None:
    _patch_httpx(monkeypatch)
    s = Settings(artifactory_ca_cert="/etc/airlift/ca/ca.crt")
    ArtifactoryClient.from_settings(s)
    # Both the normal and long-poll clients must trust the CA bundle.
    assert len(_FakeClient.instances) == 2
    assert all(i["verify"] == "/etc/airlift/ca/ca.crt" for i in _FakeClient.instances)


def test_from_settings_no_ca_cert_verifies_with_default_store(monkeypatch) -> None:
    _patch_httpx(monkeypatch)
    s = Settings()  # artifactory_ca_cert defaults to ""
    ArtifactoryClient.from_settings(s)
    assert len(_FakeClient.instances) == 2
    assert all(i["verify"] is True for i in _FakeClient.instances)


def test_init_verify_defaults_true(monkeypatch) -> None:
    _patch_httpx(monkeypatch)
    ArtifactoryClient("http://localhost:8081/artifactory")
    assert all(i["verify"] is True for i in _FakeClient.instances)


def test_from_settings_passes_auth_through(monkeypatch) -> None:
    # Regression guard: the from_settings refactor must keep wiring auth.
    _patch_httpx(monkeypatch)
    s = Settings(artifactory_token="tok")
    ArtifactoryClient.from_settings(s)
    assert all(
        i["headers"].get("Authorization") == "Bearer tok"
        for i in _FakeClient.instances
    )

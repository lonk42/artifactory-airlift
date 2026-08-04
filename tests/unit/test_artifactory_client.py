"""Unit coverage for ArtifactoryClient construction and auth (no network).

Two things are covered here. TLS-verify resolution for private/self-signed
CAs: ``from_settings`` turns ``artifactory_ca_cert`` into the httpx
``verify`` value, and ``__init__`` passes it to both underlying httpx
clients. And credential resolution, including ``FileTokenAuth``, which must
re-read its token file on every request so an externally rotated token is
picked up without a restart.
"""

import httpx
import pytest

from artifactory_airlift import artifactory_client as ac
from artifactory_airlift.artifactory_client import ArtifactoryClient, FileTokenAuth
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


def _run_auth_flow(auth: FileTokenAuth) -> httpx.Request:
    """Drive one auth_flow round and hand back the authenticated request."""
    request = httpx.Request("GET", "http://localhost:8081/artifactory/api/system/ping")
    return next(auth.auth_flow(request))


def test_file_token_auth_reads_per_request(tmp_path) -> None:
    # The whole point of the feature: the client is built once and lives for
    # the process, so the token must be re-read on each request rather than
    # captured at construction.
    path = tmp_path / "token"
    path.write_text("first")
    auth = FileTokenAuth(path)
    assert _run_auth_flow(auth).headers["Authorization"] == "Bearer first"

    path.write_text("second")
    assert _run_auth_flow(auth).headers["Authorization"] == "Bearer second"


def test_file_token_auth_strips_whitespace(tmp_path) -> None:
    # Secret tooling routinely leaves a trailing newline.
    path = tmp_path / "token"
    path.write_text("  tok\n")
    assert _run_auth_flow(FileTokenAuth(path)).headers["Authorization"] == "Bearer tok"


def test_file_token_auth_empty_file_raises(tmp_path) -> None:
    path = tmp_path / "token"
    path.write_text("\n")
    with pytest.raises(RuntimeError, match="empty"):
        _run_auth_flow(FileTokenAuth(path))


def test_from_settings_token_file_becomes_auth(monkeypatch, tmp_path) -> None:
    _patch_httpx(monkeypatch)
    path = tmp_path / "token"
    path.write_text("tok")
    s = Settings(artifactory_token_file=str(path))
    ArtifactoryClient.from_settings(s)
    assert len(_FakeClient.instances) == 2
    for i in _FakeClient.instances:
        assert isinstance(i["auth"], FileTokenAuth)
        # No static header, or a stale token would ride along with it.
        assert "Authorization" not in i["headers"]


def test_token_file_beats_static_token(monkeypatch, tmp_path) -> None:
    _patch_httpx(monkeypatch)
    path = tmp_path / "token"
    path.write_text("from-file")
    s = Settings(artifactory_token="static", artifactory_token_file=str(path))
    ArtifactoryClient.from_settings(s)
    assert all(isinstance(i["auth"], FileTokenAuth) for i in _FakeClient.instances)
    assert all("Authorization" not in i["headers"] for i in _FakeClient.instances)


def test_basic_auth_beats_token_file(monkeypatch, tmp_path) -> None:
    _patch_httpx(monkeypatch)
    path = tmp_path / "token"
    path.write_text("from-file")
    s = Settings(
        artifactory_username="admin",
        artifactory_password="pw",
        artifactory_token_file=str(path),
    )
    ArtifactoryClient.from_settings(s)
    assert all(isinstance(i["auth"], httpx.BasicAuth) for i in _FakeClient.instances)

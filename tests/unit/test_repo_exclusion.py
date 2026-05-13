from __future__ import annotations

from artifactory_airlift import sender
from artifactory_airlift.config import Settings


class _FakeClient:
    def __init__(self, repos: list[dict] | Exception) -> None:
        self._repos = repos
        self.calls = 0

    def list_repositories(self) -> list[dict]:
        self.calls += 1
        if isinstance(self._repos, Exception):
            raise self._repos
        return self._repos


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def test_defaults_include_buildinfo_and_known_system_repos() -> None:
    s = _settings()
    assert "artifactory-build-info" in s.excluded_repos
    assert "jfrog-usage-logs" in s.excluded_repos
    assert "BuildInfo" in s.excluded_package_types


def test_env_override_parses_comma_separated(monkeypatch) -> None:
    monkeypatch.setenv("AIRLIFT_EXCLUDED_REPOS", "foo,bar,baz")
    monkeypatch.setenv("AIRLIFT_EXCLUDED_PACKAGE_TYPES", "BuildInfo,Generic")
    s = Settings()
    assert s.excluded_repos == ["foo", "bar", "baz"]
    assert s.excluded_package_types == ["BuildInfo", "Generic"]


def test_resolve_excludes_by_package_type() -> None:
    s = _settings(excluded_repos=["jfrog-usage-logs"])
    client = _FakeClient(
        [
            {"key": "example-repo-local", "packageType": "Generic"},
            {"key": "artifactory-build-info", "packageType": "BuildInfo"},
            {"key": "airlift-build-info", "packageType": "BuildInfo"},
            {"key": "team-foo-build-info", "packageType": "BuildInfo"},
            {"key": "airlift-rpm-local", "packageType": "RPM"},
        ]
    )
    excluded = sender._resolve_excluded_repos(client, s)
    assert "artifactory-build-info" in excluded
    assert "airlift-build-info" in excluded
    assert "team-foo-build-info" in excluded
    assert "jfrog-usage-logs" in excluded
    assert "example-repo-local" not in excluded
    assert "airlift-rpm-local" not in excluded


def test_resolve_falls_back_to_name_list_on_api_error() -> None:
    s = _settings(excluded_repos=["jfrog-usage-logs", "auto-trashcan"])
    client = _FakeClient(RuntimeError("boom"))
    excluded = sender._resolve_excluded_repos(client, s)
    assert excluded == {"jfrog-usage-logs", "auto-trashcan"}


def test_resolve_skips_api_call_when_type_list_empty() -> None:
    s = _settings(excluded_repos=["foo"], excluded_package_types=[])
    client = _FakeClient([{"key": "anything", "packageType": "BuildInfo"}])
    excluded = sender._resolve_excluded_repos(client, s)
    assert excluded == {"foo"}
    assert client.calls == 0

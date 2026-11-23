"""The Azure account name can be supplied when binarystore.xml encrypts it.

Artifactory rewrites <accountName> into its "<keyId>.<algorithm>.<ciphertext>"
envelope in its own copy of the file, shortly after boot, even for a store
that authenticates with a platform identity. Read literally, that envelope
became the first labels of the blob hostname, and the ciphertext label is
longer than DNS allows, so every read failed with "label empty or too long".
A daemon that parsed the file before Artifactory rewrote it kept working,
which is what made the failure look like a CLI bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from artifactory_airlift import binarystore
from artifactory_airlift.binarystore import _apply_account_override
from artifactory_airlift.binarystore.config import (
    AzureConfig,
    S3Config,
    UnsupportedBinarystore,
    parse,
)
from artifactory_airlift.config import Settings

ENCRYPTED = "f5c6bf.aesgcm256." + "Q" * 70

XML = """<config version="3">
    <chain template="azure-blob-storage-v2-direct"/>
    <provider id="azure-blob-storage-v2" type="azure-blob-storage-v2">
        <accountName>{account}</accountName>
        <container>artifactory</container>
        {endpoint}
        <useInstanceCredentials>true</useInstanceCredentials>
    </provider>
</config>
"""


def _xml(tmp_path: Path, account: str, endpoint: str = "") -> Path:
    path = tmp_path / "binarystore.xml"
    tag = f"<endpoint>{endpoint}</endpoint>" if endpoint else ""
    path.write_text(XML.format(account=account, endpoint=tag))
    return path


def _settings(xml: Path, account: str = "") -> Settings:
    return Settings(
        binarystore_config=xml,
        binarystore_provider="azure",
        binarystore_account=account,
        # Pins SharedKey signing so no platform identity is looked for.
        binarystore_account_key="a2V5",
    )


def test_encrypted_account_never_becomes_a_hostname(tmp_path: Path) -> None:
    cfg = parse(_xml(tmp_path, ENCRYPTED))
    assert isinstance(cfg, AzureConfig)
    assert cfg.account == ""
    assert cfg.endpoint_url == ""


def test_plaintext_account_is_unchanged(tmp_path: Path) -> None:
    cfg = parse(_xml(tmp_path, "acct"))
    assert cfg.account == "acct"
    assert cfg.endpoint_url == "https://acct.blob.core.windows.net"


def test_resolve_without_the_setting_names_it(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedBinarystore, match="binarystore_account"):
        binarystore.resolve(_settings(_xml(tmp_path, ENCRYPTED)))


def test_resolve_with_the_setting_addresses_the_account(tmp_path: Path) -> None:
    store = binarystore.resolve(_settings(_xml(tmp_path, ENCRYPTED), "acct"))
    try:
        assert store.location("ab" * 20).startswith(
            "https://acct.blob.core.windows.net/artifactory/"
        )
    finally:
        store.close()


def test_setting_overrides_a_plaintext_account(tmp_path: Path) -> None:
    store = binarystore.resolve(_settings(_xml(tmp_path, "old"), "new1"))
    try:
        assert store.location("ab" * 20).startswith("https://new1.blob.core.windows.net/")
    finally:
        store.close()


def test_explicit_endpoint_is_kept(tmp_path: Path) -> None:
    """A stated <endpoint> (Azurite, sovereign clouds) is not rewritten."""
    xml = _xml(tmp_path, ENCRYPTED, endpoint="https://blob.example.internal/acct")
    cfg = parse(xml)
    assert cfg.endpoint_url == "https://blob.example.internal/acct"
    out = _apply_account_override(cfg, "acct")
    assert out.endpoint_url == "https://blob.example.internal/acct"
    assert out.account == "acct"


@pytest.mark.parametrize("bad", [ENCRYPTED, "ab", "UPPER", "a" * 25, "has-dash"])
def test_setting_must_look_like_an_account_name(bad: str) -> None:
    cfg = AzureConfig(container="c", endpoint_url="")
    with pytest.raises(UnsupportedBinarystore, match="3 to 24"):
        _apply_account_override(cfg, bad)


def test_other_backends_ignore_the_setting() -> None:
    cfg = S3Config(bucket="b", endpoint_url="https://s3.example")
    assert _apply_account_override(cfg, "acct") is cfg


# -- doctor -------------------------------------------------------------------


class _Store:
    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self.result, self.exc, self.calls = result, exc, []

    def open(self, sha1: str):
        self.calls.append(sha1)
        if self.exc:
            raise self.exc
        return self.result


def _run(store) -> list[tuple]:
    from artifactory_airlift.cli.views import _doctor_store_read

    checks: list[tuple] = []
    _doctor_store_read(store, lambda *a, **k: checks.append(a))
    return checks


def test_doctor_read_passes_on_absent_blob() -> None:
    store = _Store(result=None)
    assert _run(store) == [("binarystore read", True, "store answered a read")]
    assert store.calls == ["0" * 40]


def test_doctor_read_fails_on_the_dns_error() -> None:
    """The exact failure doctor used to pass: resolution fine, reads broken."""
    exc = UnicodeError("encoding with 'idna' codec failed (UnicodeError: label too long)")
    [(name, ok, detail)] = _run(_Store(exc=exc))
    assert (name, ok) == ("binarystore read", False)
    assert "label too long" in detail


def test_doctor_read_tries_once(monkeypatch) -> None:
    """A retrying backend gets one attempt, not the daemon's backoff chain."""
    import httpx
    from tenacity import retry, stop_after_attempt

    attempts = []

    class Retrying:
        @retry(reraise=True, stop=stop_after_attempt(5))
        def open(self, sha1):
            attempts.append(sha1)
            raise httpx.ConnectError("refused")

    [(_, ok, _)] = _run(Retrying())
    assert ok is False
    assert len(attempts) == 1

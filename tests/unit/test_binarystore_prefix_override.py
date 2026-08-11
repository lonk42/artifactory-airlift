"""The binarystore key prefix can be overridden independently of the XML.

Artifactory keys blobs as ``<path>/<sha1[:2]>/<sha1>``, and when ``<path>`` is
omitted the default is the provider's, not a universal one: JFrog documents
"filestore" for s3-storage-v3 but "data" for azure-blob-storage-v2, and the v1
Azure provider has no ``<path>`` parameter at all. Airlift cannot tell which
default applies from the XML alone, and it must not be "fixed" by writing
``<path>`` into binarystore.xml, because Artifactory reads the same file and
would relocate its own filestore.

The failure mode this guards against is silent: a blob that is not where
airlift looked reads as 404, which is indistinguishable from one Artifactory
has not written yet, so entries defer forever instead of erroring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from artifactory_airlift.binarystore import _apply_prefix_override, sha1_key
from artifactory_airlift.binarystore.config import (
    AzureConfig,
    FilesystemConfig,
    S3Config,
)

SHA1 = "5f85c6df1e2a3b4c5d6e7f8091a2b3c4d5e6f708"


def _azure(prefix: str = "filestore") -> AzureConfig:
    return AzureConfig(
        container="artifactory",
        endpoint_url="https://acct.blob.core.windows.net",
        account="acct",
        prefix=prefix,
    )


def _s3(prefix: str = "filestore") -> S3Config:
    return S3Config(bucket="artifactory", endpoint_url="https://s3.example", prefix=prefix)


def test_empty_override_keeps_the_xml_prefix() -> None:
    cfg = _azure("artifactory/filestore")
    assert _apply_prefix_override(cfg, "").prefix == "artifactory/filestore"
    assert _apply_prefix_override(cfg, "   ").prefix == "artifactory/filestore"


def test_override_replaces_the_xml_prefix() -> None:
    """The Azure Blob v2 case: XML omits <path>, real layout is "data"."""
    cfg = _apply_prefix_override(_azure("filestore"), "data")
    assert cfg.prefix == "data"
    assert sha1_key(cfg.prefix, SHA1) == f"data/5f/{SHA1}"


def test_override_applies_to_s3_too() -> None:
    cfg = _apply_prefix_override(_s3("filestore"), "artifactory/filestore")
    assert cfg.prefix == "artifactory/filestore"


def test_slash_means_the_container_root() -> None:
    """"" already means "unset", so "/" is how an empty prefix is expressed."""
    cfg = _apply_prefix_override(_azure("filestore"), "/")
    assert cfg.prefix == ""
    assert sha1_key(cfg.prefix, SHA1) == f"5f/{SHA1}"


def test_surrounding_slashes_are_stripped() -> None:
    assert _apply_prefix_override(_azure(), "/data/").prefix == "data"


def test_filesystem_config_is_untouched() -> None:
    """A filesystem store has no key prefix; the override must not error."""
    cfg = FilesystemConfig(root=Path("/var/opt/jfrog/filestore"))
    assert _apply_prefix_override(cfg, "data") is cfg


def test_override_leaves_other_fields_alone() -> None:
    before = _azure("filestore")
    after = _apply_prefix_override(before, "data")
    assert after.container == before.container
    assert after.endpoint_url == before.endpoint_url
    assert after.account == before.account
    assert after.instance_credentials == before.instance_credentials


@pytest.mark.parametrize("prefix", ["data", "artifactory/filestore", ""])
def test_matching_override_is_a_noop(prefix: str) -> None:
    """Restating the prefix the XML already gave returns the same object."""
    cfg = _azure(prefix)
    assert _apply_prefix_override(cfg, prefix or "/") is cfg


AZURE_V2_NO_PATH = """<config version="3">
    <chain template="azure-blob-storage-v2-direct"/>
    <provider id="azure-blob-storage-v2" type="azure-blob-storage-v2">
        <accountName>acct</accountName>
        <container>artifactory</container>
        <useInstanceCredentials>true</useInstanceCredentials>
    </provider>
</config>
"""


def test_resolve_honours_the_setting(tmp_path: Path) -> None:
    """End to end: Settings -> resolve() -> the store addresses the new prefix.

    The XML states no <path>, so the parser yields airlift's default. Without
    the override this addresses artifactory/filestore/..., which is where the
    404s came from on a v2 Azure store whose real layout is data/....
    """
    from artifactory_airlift import binarystore
    from artifactory_airlift.config import Settings

    xml = tmp_path / "binarystore.xml"
    xml.write_text(AZURE_V2_NO_PATH)

    def _settings(prefix: str) -> Settings:
        return Settings(
            binarystore_config=xml,
            binarystore_provider="azure",
            binarystore_prefix=prefix,
            # Pins SharedKey signing so the test never reaches for a platform
            # identity that does not exist under pytest.
            binarystore_account_key="a2V5",
        )

    default = binarystore.resolve(_settings(""))
    try:
        assert "'filestore'" in default.describe()
    finally:
        default.close()

    overridden = binarystore.resolve(_settings("data"))
    try:
        assert "'data'" in overridden.describe()
    finally:
        overridden.close()

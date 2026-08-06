"""Parsing of Artifactory's binarystore.xml.

LIVE_S3_XML is copied verbatim from a running Artifactory 7.x instance backed
by an S3 store, so the parser is pinned against a real file rather than against
a reading of the JFrog docs. Several details here are not what the docs would
lead you to expect: the endpoint is a bare host with the scheme and port in
sibling elements, the flag is `enablePathStyleAccess`, the key prefix is a
two-segment path, and the chain is written out explicitly instead of using a
`template` attribute.
"""

from pathlib import Path

import pytest

from artifactory_airlift.binarystore import sha1_key
from artifactory_airlift.binarystore.config import (
    AzureConfig,
    FilesystemConfig,
    S3Config,
    UnsupportedBinarystore,
    parse,
)

LIVE_S3_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<config version="2">
    <chain>
        <provider type="cache-fs" id="cache-fs">
            <provider type="s3-storage-v3" id="s3-storage-v3"/>
        </provider>
    </chain>
    <provider type="cache-fs" id="cache-fs">
        <maxCacheSize>5000000000</maxCacheSize>
        <cacheProviderDir>cache</cacheProviderDir>
    </provider>
    <provider type="s3-storage-v3" id="s3-storage-v3">
        <bucketName>artifactory-a</bucketName>
        <enablePathStyleAccess>true</enablePathStyleAccess>
        <testConnection>false</testConnection>
        <useHttp>true</useHttp>
        <path>artifactory/filestore</path>
        <endpoint>minio.minio.svc.cluster.local</endpoint>
        <credential>a1b2c3.aesgcm256.EXAMPLEciphertextEXAMPLEciphertextEXAMPLEciphertextEXAMPLEcipher</credential>
        <port>9000</port>
        <identity>a1b2c3.aesgcm256.EXAMPLEidentityEXAMPLEidentityEXAMPLEidentity</identity>
        <region>us-east-1</region>
        <maxConnections>50</maxConnections>
    </provider>
</config>
"""


def _write(tmp_path: Path, xml: str) -> Path:
    p = tmp_path / "binarystore.xml"
    p.write_text(xml)
    return p


def test_live_s3_config(tmp_path: Path) -> None:
    cfg = parse(_write(tmp_path, LIVE_S3_XML))
    assert isinstance(cfg, S3Config)
    assert cfg.bucket == "artifactory-a"
    # Bare host + <port> + <useHttp> compose into the endpoint URL.
    assert cfg.endpoint_url == "http://minio.minio.svc.cluster.local:9000"
    assert cfg.prefix == "artifactory/filestore"
    assert cfg.region == "us-east-1"
    assert cfg.path_style is True


def test_live_s3_key_layout_matches_artifactory(tmp_path: Path) -> None:
    """The computed key must match where Artifactory actually stored the blob.

    Verified against the live instance: sha1 5f85c6df... was found in the
    bucket at artifactory/filestore/5f/5f85c6df...
    """
    cfg = parse(_write(tmp_path, LIVE_S3_XML))
    sha1 = "5f85c6dfc672334addea0fe63cc7132ca0498fc6"
    assert sha1_key(cfg.prefix, sha1) == f"artifactory/filestore/5f/{sha1}"


def test_template_form(tmp_path: Path) -> None:
    """The other way a chain can be declared: a template attribute."""
    xml = """<config version="2">
        <chain template="s3-storage-v3"/>
        <provider type="s3-storage-v3" id="s3-storage-v3">
            <bucketName>b</bucketName>
            <endpoint>https://s3.example.com</endpoint>
        </provider>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert isinstance(cfg, S3Config)
    assert cfg.bucket == "b"
    assert cfg.endpoint_url == "https://s3.example.com"
    # Absent <path> falls back to Artifactory's default prefix.
    assert cfg.prefix == "filestore"
    # Absent <useHttp> means https, and no port is invented.
    assert cfg.path_style is False


def test_endpoint_already_a_url_keeps_scheme(tmp_path: Path) -> None:
    xml = """<config version="2">
        <chain><provider type="s3-storage-v3" id="s3"/></chain>
        <provider type="s3-storage-v3" id="s3">
            <bucketName>b</bucketName>
            <endpoint>https://s3.example.com:8443/</endpoint>
            <useHttp>true</useHttp>
        </provider>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert cfg.endpoint_url == "https://s3.example.com:8443"


def test_azure_config(tmp_path: Path) -> None:
    xml = """<config version="2">
        <chain>
            <provider type="cache-fs" id="cache-fs">
                <provider type="azure-blob-storage" id="azure-blob-storage"/>
            </provider>
        </chain>
        <provider type="azure-blob-storage" id="azure-blob-storage">
            <accountName>acct</accountName>
            <accountKey>c2VjcmV0</accountKey>
            <containerName>artifactory</containerName>
            <path>filestore</path>
        </provider>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert isinstance(cfg, AzureConfig)
    assert cfg.container == "artifactory"
    assert cfg.account == "acct"
    # Endpoint is derived from the account when not stated.
    assert cfg.endpoint_url == "https://acct.blob.core.windows.net"


def test_azure_v2_template_chain(tmp_path: Path) -> None:
    """The v2 provider family is addressed the same way as the original.

    A template-only chain is the common shape here, and this one names the
    "-direct" variant while the settings block is declared under the plain
    type, so the settings have to be found by family rather than exact type.
    """
    xml = """<config version="2">
        <chain template="azure-blob-storage-v2-direct"/>
        <provider type="azure-blob-storage-v2" id="azure-blob-storage-v2">
            <accountName>acct</accountName>
            <accountKey>c2VjcmV0</accountKey>
            <containerName>artifactory</containerName>
            <endpoint>https://acct.blob.core.windows.net/</endpoint>
        </provider>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert isinstance(cfg, AzureConfig)
    assert cfg.container == "artifactory"
    assert cfg.account == "acct"
    assert cfg.account_key == "c2VjcmV0"
    assert cfg.endpoint_url == "https://acct.blob.core.windows.net"
    # Absent <path> falls back to Artifactory's default prefix.
    assert cfg.prefix == "filestore"


def test_azure_v2_explicit_chain(tmp_path: Path) -> None:
    xml = """<config version="2">
        <chain>
            <provider type="cache-fs" id="cache-fs">
                <provider type="cluster-azure-blob-storage-v2" id="azure"/>
            </provider>
        </chain>
        <provider type="cluster-azure-blob-storage-v2" id="azure">
            <accountName>acct</accountName>
            <containerName>artifactory</containerName>
            <path>artifactory/filestore</path>
        </provider>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert isinstance(cfg, AzureConfig)
    assert cfg.container == "artifactory"
    assert cfg.prefix == "artifactory/filestore"


def test_filesystem_chain(tmp_path: Path) -> None:
    xml = """<config version="2">
        <chain template="file-system"/>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert isinstance(cfg, FilesystemConfig)
    # No absolute fileStoreDir, so the caller's configured root is used.
    assert cfg.root is None


def test_filesystem_absolute_dir(tmp_path: Path) -> None:
    xml = """<config version="2">
        <chain><provider type="file-system" id="file-system"/></chain>
        <provider type="file-system" id="file-system">
            <fileStoreDir>/mnt/blobs</fileStoreDir>
        </provider>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert cfg.root == Path("/mnt/blobs")


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """Absent config is not an error; it means the pre-object-storage default."""
    assert parse(tmp_path / "nope.xml") is None


def test_unparseable_file_returns_none(tmp_path: Path) -> None:
    assert parse(_write(tmp_path, "<config><broken")) is None


def test_sharded_chain_is_refused(tmp_path: Path) -> None:
    """Guessing a key layout for a sharded chain would corrupt the destination."""
    xml = """<config version="2">
        <chain>
            <provider type="sharding-cluster" id="shard">
                <provider type="s3-storage-v3" id="s3a"/>
                <provider type="s3-storage-v3" id="s3b"/>
            </provider>
        </chain>
    </config>"""
    with pytest.raises(UnsupportedBinarystore, match="Sharded"):
        parse(_write(tmp_path, xml))


def test_unknown_template_is_refused(tmp_path: Path) -> None:
    xml = '<config version="2"><chain template="full-db"/></config>'
    with pytest.raises(UnsupportedBinarystore, match="full-db"):
        parse(_write(tmp_path, xml))


def test_s3_without_bucket_is_refused(tmp_path: Path) -> None:
    xml = """<config version="2">
        <chain><provider type="s3-storage-v3" id="s3"/></chain>
        <provider type="s3-storage-v3" id="s3"><endpoint>x</endpoint></provider>
    </config>"""
    with pytest.raises(UnsupportedBinarystore, match="bucketName"):
        parse(_write(tmp_path, xml))


def test_plaintext_credentials_are_read(tmp_path: Path) -> None:
    """The Helm-rendered copy still has real credentials; use them.

    Artifactory encrypts them in place only once it owns the config, so the
    chart's Secret copy is a complete configuration source on its own.
    """
    xml = """<config version="2">
        <chain><provider type="s3-storage-v3" id="s3"/></chain>
        <provider type="s3-storage-v3" id="s3">
            <bucketName>b</bucketName>
            <endpoint>minio.example</endpoint>
            <identity>minioadmin</identity>
            <credential>supersecret</credential>
        </provider>
    </config>"""
    cfg = parse(_write(tmp_path, xml))
    assert cfg.access_key == "minioadmin"
    assert cfg.secret_key == "supersecret"


def test_encrypted_credentials_are_treated_as_absent(tmp_path: Path) -> None:
    """The on-disk copy is an opaque envelope; it must not reach the signer."""
    cfg = parse(_write(tmp_path, LIVE_S3_XML))
    assert cfg.access_key == ""
    assert cfg.secret_key == ""

"""Request shapes for the object-storage backends.

Uses httpx's MockTransport so the exact URLs, keys and call sequences are
asserted without a network. Behavioural correctness against real servers was
established separately against MinIO and Azurite.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from artifactory_airlift.binarystore import resolve
from artifactory_airlift.binarystore.azure import AzureBlobStore
from artifactory_airlift.binarystore.config import (
    AzureConfig,
    S3Config,
    UnsupportedBinarystore,
)
from artifactory_airlift.binarystore.s3 import S3BlobStore
from artifactory_airlift.config import Settings

from .test_binarystore_config import LIVE_AZURE_V2_XML

S3_CFG = S3Config(
    bucket="artifactory-a",
    endpoint_url="http://minio.example:9000",
    prefix="artifactory/filestore",
    region="us-east-1",
    path_style=True,
)
SHA1 = "5f85c6dfc672334addea0fe63cc7132ca0498fc6"
KEY = f"artifactory/filestore/5f/{SHA1}"


def _s3(handler, **kwargs) -> S3BlobStore:
    store = S3BlobStore(S3_CFG, access_key="ak", secret_key="sk", **kwargs)
    store._http = httpx.Client(transport=httpx.MockTransport(handler), auth=store._http.auth)
    return store


def _azure(handler, **kwargs) -> AzureBlobStore:
    cfg = AzureConfig(
        container="artifactory",
        endpoint_url="https://acct.blob.core.windows.net",
        account="acct",
        prefix="filestore",
    )
    store = AzureBlobStore(cfg, account_key="c2VjcmV0MDAwMDAwMDAwMDAwMDAwMA==", **kwargs)
    store._http = httpx.Client(transport=httpx.MockTransport(handler), auth=store._http.auth)
    return store


def test_s3_path_style_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    _s3(handler).exists(SHA1)
    assert seen == [f"http://minio.example:9000/artifactory-a/{KEY}"]


def test_s3_virtual_host_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404)

    store = S3BlobStore(
        S3Config(
            bucket="bkt",
            endpoint_url="https://s3.example.com",
            prefix="filestore",
            path_style=False,
        ),
        access_key="ak",
        secret_key="sk",
    )
    store._http = httpx.Client(
        transport=httpx.MockTransport(handler), auth=store._http.auth
    )
    store.exists(SHA1)
    assert seen == [f"https://bkt.s3.example.com/filestore/5f/{SHA1}"]


def test_s3_open_missing_returns_none() -> None:
    store = _s3(lambda request: httpx.Response(404))
    assert store.open(SHA1) is None


def test_s3_open_streams_body_and_size() -> None:
    store = _s3(lambda request: httpx.Response(200, content=b"hello world"))
    opened = store.open(SHA1)
    assert opened is not None
    reader, size = opened
    assert size == 11
    assert reader.read() == b"hello world"
    reader.close()


def test_s3_write_skips_existing_blob(tmp_path: Path) -> None:
    """Content addressing makes writes idempotent; an existing key is a no-op."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, headers={"content-length": "5"})

    src = tmp_path / "b.bin"
    src.write_bytes(b"hello")
    assert _s3(handler).write(src, SHA1) is False
    assert calls == ["HEAD"]  # no upload attempted


def test_s3_write_single_put(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(200)

    src = tmp_path / "b.bin"
    src.write_bytes(b"hello")
    assert _s3(handler).write(src, SHA1) is True
    assert [m for m, _ in calls] == ["HEAD", "PUT"]


def test_s3_multipart_sequence(tmp_path: Path) -> None:
    """Above the threshold: create, one part per block, then complete."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url.params)))
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "POST" and "uploads" in str(request.url.params):
            return httpx.Response(
                200, text="<Init><UploadId>UP1</UploadId></Init>"
            )
        if request.method == "PUT":
            return httpx.Response(200, headers={"etag": '"abc"'})
        return httpx.Response(200, text="<CompleteMultipartUploadResult/>")

    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 25)
    store = _s3(handler, multipart_threshold=10, part_bytes=10)
    assert store.write(src, SHA1) is True

    methods = [m for m, _ in calls]
    assert methods == ["HEAD", "POST", "PUT", "PUT", "PUT", "POST"]  # 25 bytes -> 3 parts


def test_s3_multipart_aborts_on_failure(tmp_path: Path, monkeypatch) -> None:
    """A failed upload must not leave billable orphan parts behind."""
    # The part upload retries with exponential backoff; skip the waiting so
    # this test costs milliseconds rather than seconds.
    monkeypatch.setattr(S3BlobStore._upload_part.retry, "wait", wait_none())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "POST" and "uploads" in str(request.url.params):
            return httpx.Response(200, text="<Init><UploadId>UP1</UploadId></Init>")
        if request.method == "PUT":
            return httpx.Response(500)
        return httpx.Response(200)

    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 25)
    store = _s3(handler, multipart_threshold=10, part_bytes=10)
    with pytest.raises(httpx.HTTPStatusError):
        store.write(src, SHA1)
    assert "DELETE" in calls  # AbortMultipartUpload issued


def test_azure_blob_url_and_type_header(tmp_path: Path) -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.method, str(request.url), request.headers.get("x-ms-blob-type", ""))
        )
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(201)

    src = tmp_path / "b.bin"
    src.write_bytes(b"hello")
    assert _azure(handler).write(src, SHA1) is True

    put = [s for s in seen if s[0] == "PUT"][0]
    assert put[1] == f"https://acct.blob.core.windows.net/artifactory/filestore/5f/{SHA1}"
    assert put[2] == "BlockBlob"


def test_azure_staged_blocks_then_commit(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.params.get("comp", "")))
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(201)

    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 25)
    store = _azure(handler, multipart_threshold=10, part_bytes=10)
    assert store.write(src, SHA1) is True

    assert [c for _, c in calls] == ["", "block", "block", "block", "blocklist"]


def test_instance_credentials_error_names_the_setting(tmp_path: Path) -> None:
    """An identity-authenticated store fails with an actionable message.

    There is no credential in the XML to fall back to, by design, so the
    generic "encrypted or absent" wording would send the operator hunting for
    a key that never existed.
    """
    xml = tmp_path / "binarystore.xml"
    xml.write_text(LIVE_AZURE_V2_XML)
    settings = Settings(binarystore_config=str(xml))

    with pytest.raises(UnsupportedBinarystore, match="binarystore_account_key"):
        resolve(settings)


def test_configured_account_key_satisfies_instance_credentials(tmp_path: Path) -> None:
    xml = tmp_path / "binarystore.xml"
    xml.write_text(LIVE_AZURE_V2_XML)
    settings = Settings(
        binarystore_config=str(xml),
        binarystore_account_key="c2VjcmV0MDAwMDAwMDAwMDAwMDAwMA==",
    )

    store = resolve(settings)
    assert store.kind == "azure"
    store.close()

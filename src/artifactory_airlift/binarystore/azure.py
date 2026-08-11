"""Azure Blob Storage blob store, signed with the SharedKey scheme.

Mirrors the S3 backend: same BlobStore interface, same retry policy, signing
implemented on httpx rather than via an SDK. Large blobs upload as staged
blocks, which is Azure's equivalent of S3 multipart.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
from pathlib import Path
from typing import IO
from urllib.parse import quote

import httpx
from tenacity import retry

from .. import log
from ._retry import RETRY
from ._stream import RangeReader, ResponseReader
from .config import AzureConfig

logger = log.get("binarystore.azure")

# Pinned so the signing contract is stable; SharedKey string-to-sign layout is
# tied to the service version in use.
_API_VERSION = "2021-08-06"

# Block size for staged uploads. Azure allows up to 50000 blocks per blob.
_BLOCK_BYTES = 64 * 1024**2

# Headers that participate in the SharedKey string-to-sign, in the exact order
# the service expects. Order is part of the protocol, not a style choice.
_SIGNED_HEADERS = (
    "content-encoding",
    "content-language",
    "content-length",
    "content-md5",
    "content-type",
    "date",
    "if-modified-since",
    "if-match",
    "if-none-match",
    "if-unmodified-since",
    "range",
)


class SharedKeyAuth(httpx.Auth):
    """Sign each request with the Azure SharedKey scheme at send time."""

    def __init__(self, account: str, account_key: str) -> None:
        self.account = account
        try:
            self._key = base64.b64decode(account_key)
        except Exception as exc:  # noqa: BLE001 - surfaced as a config error
            raise ValueError("azure account key is not valid base64") from exc

    def auth_flow(self, request: httpx.Request):
        request.headers["x-ms-date"] = dt.datetime.now(dt.timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        request.headers.setdefault("x-ms-version", _API_VERSION)

        parts = [request.method]
        for name in _SIGNED_HEADERS:
            value = request.headers.get(name, "")
            # Content-Length is signed as an empty string when zero, and the
            # Date slot stays empty because x-ms-date carries the timestamp.
            if name == "content-length" and value in ("0", ""):
                value = ""
            if name == "date":
                value = ""
            parts.append(value)

        canonical_headers = "".join(
            f"{name}:{request.headers[name].strip()}\n"
            for name in sorted(k.lower() for k in request.headers if k.lower().startswith("x-ms-"))
        )
        canonical_resource = f"/{self.account}{request.url.path}"
        for key, value in sorted(request.url.params.multi_items()):
            canonical_resource += f"\n{key}:{value}"

        string_to_sign = "\n".join(parts) + "\n" + canonical_headers + canonical_resource
        signature = base64.b64encode(
            hmac.new(self._key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        ).decode()
        request.headers["Authorization"] = f"SharedKey {self.account}:{signature}"
        yield request


class BearerAuth(httpx.Auth):
    """Authenticate as a platform-assigned identity, not with an account key.

    The token is fetched per request from the shared credential, which caches
    it until close to expiry. Doing the lookup here rather than at
    construction is what lets a long-running sidecar survive token rotation
    without a restart, the same reasoning as FileTokenAuth in
    artifactory_client.
    """

    def __init__(self, credential) -> None:
        self._credential = credential

    def auth_flow(self, request: httpx.Request):
        request.headers["x-ms-date"] = dt.datetime.now(dt.timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        request.headers.setdefault("x-ms-version", _API_VERSION)
        request.headers["Authorization"] = f"Bearer {self._credential.token()}"
        yield request


def _block_id(number: int) -> str:
    """Base64 block id. Every id for one blob must be the same length."""
    return base64.b64encode(f"{number:08d}".encode()).decode()


class AzureBlobStore:
    kind = "azure"

    def __init__(
        self,
        cfg: AzureConfig,
        *,
        account_key: str = "",
        credential=None,
        verify: bool | str = True,
        multipart_threshold: int = 256 * 1024**2,
        part_bytes: int = _BLOCK_BYTES,
        timeout: float = 60.0,
    ) -> None:
        if not account_key and credential is None:
            raise ValueError("azure blob store needs an account key or a credential")
        self._cfg = cfg
        self._threshold = multipart_threshold
        self._part_bytes = part_bytes
        # An account key wins when both are available, so an operator can
        # always pin behaviour by setting one.
        self._credential = None if account_key else credential
        auth = (
            SharedKeyAuth(cfg.account, account_key)
            if account_key
            else BearerAuth(credential)
        )
        self._http = httpx.Client(auth=auth, timeout=timeout, verify=verify)

    # -- addressing ---------------------------------------------------------

    def _key(self, sha1: str) -> str:
        from . import sha1_key

        return sha1_key(self._cfg.prefix, sha1)

    def _url(self, sha1: str) -> str:
        return f"{self._cfg.endpoint_url}/{self._cfg.container}/{quote(self._key(sha1))}"

    def describe(self) -> str:
        auth = (
            f"as {self._credential.describe()}"
            if self._credential is not None
            else "with an account key"
        )
        return (
            f"azure container {self._cfg.container} at {self._cfg.endpoint_url} "
            f"(prefix {self._cfg.prefix!r}) {auth}"
        )

    # -- BlobStore ----------------------------------------------------------

    @retry(**RETRY)
    def _head(self, url: str) -> httpx.Response:
        r = self._http.head(url)
        if r.status_code not in (200, 404):
            r.raise_for_status()
        return r

    def exists(self, sha1: str) -> bool:
        return self._head(self._url(sha1)).status_code == 200

    @retry(**RETRY)
    def open(self, sha1: str) -> tuple[IO[bytes], int] | None:
        request = self._http.build_request("GET", self._url(sha1))
        response = self._http.send(request, stream=True)
        if response.status_code == 404:
            response.close()
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            response.close()
            raise
        size = int(response.headers.get("content-length", 0))
        return ResponseReader(response), size

    def write(self, src: Path, sha1: str) -> bool:
        url = self._url(sha1)
        if self._head(url).status_code == 200:
            return False
        size = src.stat().st_size
        if size >= self._threshold:
            self._write_blocks(src, url, size, sha1)
        else:
            self._write_single(src, url, size)
        return True

    @retry(**RETRY)
    def _write_single(self, src: Path, url: str, size: int) -> None:
        with src.open("rb") as fh:
            r = self._http.put(
                url,
                content=fh,
                headers={
                    "Content-Length": str(size),
                    "x-ms-blob-type": "BlockBlob",
                },
            )
        r.raise_for_status()

    def _write_blocks(self, src: Path, url: str, size: int, sha1: str) -> None:
        logger.info(
            "binarystore.multipart_started",
            sha1=sha1,
            size=size,
            part_bytes=self._part_bytes,
        )
        ids: list[str] = []
        with src.open("rb") as fh:
            number = 1
            remaining = size
            while remaining > 0:
                length = min(self._part_bytes, remaining)
                block_id = _block_id(number)
                self._put_block(fh, fh.tell(), length, url, block_id)
                ids.append(block_id)
                remaining -= length
                number += 1
        # Uncommitted blocks expire on their own, so a failure before this
        # point needs no explicit abort.
        self._commit_blocks(url, ids)
        logger.info("binarystore.multipart_done", sha1=sha1, parts=len(ids))

    @retry(**RETRY)
    def _put_block(
        self, fh: IO[bytes], offset: int, length: int, url: str, block_id: str
    ) -> None:
        fh.seek(offset)
        r = self._http.put(
            url,
            params={"comp": "block", "blockid": block_id},
            content=RangeReader(fh, length),
            headers={"Content-Length": str(length)},
        )
        r.raise_for_status()

    @retry(**RETRY)
    def _commit_blocks(self, url: str, ids: list[str]) -> None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?><BlockList>'
            + "".join(f"<Latest>{i}</Latest>" for i in ids)
            + "</BlockList>"
        ).encode()
        r = self._http.put(
            url,
            params={"comp": "blocklist"},
            content=body,
            headers={
                "Content-Length": str(len(body)),
                "Content-Type": "application/xml",
            },
        )
        r.raise_for_status()

    def probe(self) -> None:
        key = (
            f"{self._cfg.prefix.strip('/')}/.airlift-probe"
            if self._cfg.prefix
            else ".airlift-probe"
        )
        url = f"{self._cfg.endpoint_url}/{self._cfg.container}/{quote(key)}"
        r = self._http.put(
            url,
            content=b"ok",
            headers={"Content-Length": "2", "x-ms-blob-type": "BlockBlob"},
        )
        r.raise_for_status()
        self._http.delete(url)

    def close(self) -> None:
        self._http.close()
        if self._credential is not None:
            self._credential.close()

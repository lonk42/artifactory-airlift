"""S3-compatible blob store, signed with AWS Signature Version 4.

Covers anything speaking the S3 API: Ceph RADOS Gateway, MinIO, AWS S3. The
signing is implemented directly on httpx rather than pulling in a cloud SDK,
which keeps the image small and reuses the retry and TLS handling the rest of
airlift already has.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from pathlib import Path
from typing import IO
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
from tenacity import retry

from .. import log
from ._stream import RangeReader, ResponseReader
from .config import S3Config
from ._retry import RETRY

logger = log.get("binarystore.s3")

_ALGORITHM = "AWS4-HMAC-SHA256"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# Signing a multi-gigabyte upload would mean hashing it a second time purely
# to satisfy the signature. S3 and its clones accept UNSIGNED-PAYLOAD for
# exactly this reason; integrity is still covered because airlift verifies
# blobs by sha1 end to end.
_UNSIGNED = "UNSIGNED-PAYLOAD"

# Part size for multipart uploads. S3 requires every part except the last to
# be at least 5 MiB, and caps a single upload at 10000 parts.
_PART_BYTES = 64 * 1024**2


class SigV4Auth(httpx.Auth):
    """Sign each request with SigV4 at send time.

    Implemented as an httpx.Auth, mirroring FileTokenAuth in the Artifactory
    client, so signing happens inside the request path. That matters for
    retries: a request replayed by tenacity is re-signed with a fresh
    timestamp instead of reusing a signature that may have aged out.
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        *,
        region: str,
        service: str = "s3",
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.service = service

    def _signing_key(self, datestamp: str) -> bytes:
        key = f"AWS4{self.secret_key}".encode()
        for part in (datestamp, self.region, self.service, "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        return key

    def auth_flow(self, request: httpx.Request):
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        # A body that is not a plain in-memory buffer cannot be hashed without
        # consuming the stream, so uploads declare an unsigned payload.
        payload_hash = _EMPTY_SHA256 if request.method in ("GET", "HEAD", "DELETE") else _UNSIGNED
        if request.method == "POST" and request.content:
            payload_hash = hashlib.sha256(request.content).hexdigest()

        request.headers["x-amz-date"] = amz_date
        request.headers["x-amz-content-sha256"] = payload_hash
        request.headers["host"] = request.url.netloc.decode("ascii")

        canonical_uri = quote(request.url.path, safe="/~")
        canonical_qs = "&".join(
            f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
            for k, v in sorted(request.url.params.multi_items())
        )

        signed = ["host", "x-amz-content-sha256", "x-amz-date"]
        canonical_headers = "".join(
            f"{name}:{request.headers[name].strip()}\n" for name in signed
        )
        signed_headers = ";".join(signed)

        canonical_request = "\n".join(
            [
                request.method,
                canonical_uri,
                canonical_qs,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{datestamp}/{self.region}/{self.service}/aws4_request"
        string_to_sign = "\n".join(
            [
                _ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._signing_key(datestamp), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        request.headers["Authorization"] = (
            f"{_ALGORITHM} Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        yield request


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class S3BlobStore:
    kind = "s3"

    def __init__(
        self,
        cfg: S3Config,
        *,
        access_key: str,
        secret_key: str,
        verify: bool | str = True,
        multipart_threshold: int = 256 * 1024**2,
        part_bytes: int = _PART_BYTES,
        timeout: float = 60.0,
    ) -> None:
        self._cfg = cfg
        self._threshold = multipart_threshold
        self._part_bytes = part_bytes
        self._http = httpx.Client(
            auth=SigV4Auth(access_key, secret_key, region=cfg.region),
            timeout=timeout,
            verify=verify,
        )

    # -- addressing ---------------------------------------------------------

    def _bucket_url(self) -> str:
        if self._cfg.path_style:
            return f"{self._cfg.endpoint_url}/{self._cfg.bucket}"
        scheme, _, host = self._cfg.endpoint_url.partition("://")
        return f"{scheme}://{self._cfg.bucket}.{host}"

    def _key(self, sha1: str) -> str:
        from . import sha1_key

        return sha1_key(self._cfg.prefix, sha1)

    def _url(self, sha1: str) -> str:
        return f"{self._bucket_url()}/{self._key(sha1)}"

    def describe(self) -> str:
        return (
            f"s3 bucket {self._cfg.bucket} at {self._cfg.endpoint_url} "
            f"(prefix {self._cfg.prefix!r})"
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
            self._write_multipart(src, url, size, sha1)
        else:
            self._write_single(src, url, size)
        return True

    @retry(**RETRY)
    def _write_single(self, src: Path, url: str, size: int) -> None:
        # Reopened per attempt so a retry restarts from byte zero rather than
        # resuming a partially consumed handle.
        with src.open("rb") as fh:
            r = self._http.put(
                url, content=fh, headers={"Content-Length": str(size)}
            )
        r.raise_for_status()

    def _write_multipart(self, src: Path, url: str, size: int, sha1: str) -> None:
        upload_id = self._create_upload(url)
        logger.info(
            "binarystore.multipart_started",
            sha1=sha1,
            size=size,
            part_bytes=self._part_bytes,
        )
        try:
            parts: list[tuple[int, str]] = []
            with src.open("rb") as fh:
                number = 1
                remaining = size
                while remaining > 0:
                    length = min(self._part_bytes, remaining)
                    offset = fh.tell()
                    etag = self._upload_part(fh, offset, length, url, upload_id, number)
                    parts.append((number, etag))
                    remaining -= length
                    number += 1
            self._complete_upload(url, upload_id, parts)
        except Exception:
            # Without an abort the incomplete parts linger and are billed.
            self._abort_upload(url, upload_id)
            raise
        logger.info("binarystore.multipart_done", sha1=sha1, parts=len(parts))

    @retry(**RETRY)
    def _create_upload(self, url: str) -> str:
        r = self._http.post(url, params={"uploads": ""})
        r.raise_for_status()
        root = ElementTree.fromstring(r.text)
        for child in root:
            if _strip_ns(child.tag) == "UploadId" and child.text:
                return child.text.strip()
        raise RuntimeError(f"no UploadId in multipart response for {url}")

    @retry(**RETRY)
    def _upload_part(
        self, fh: IO[bytes], offset: int, length: int, url: str, upload_id: str, number: int
    ) -> str:
        # Seek explicitly so a retried attempt re-sends the same window.
        fh.seek(offset)
        r = self._http.put(
            url,
            params={"partNumber": str(number), "uploadId": upload_id},
            content=RangeReader(fh, length),
            headers={"Content-Length": str(length)},
        )
        r.raise_for_status()
        etag = r.headers.get("etag", "").strip()
        if not etag:
            raise RuntimeError(f"part {number} of {url} returned no ETag")
        return etag

    @retry(**RETRY)
    def _complete_upload(self, url: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
        body = ["<CompleteMultipartUpload>"]
        for number, etag in parts:
            body.append(
                f"<Part><PartNumber>{number}</PartNumber><ETag>{etag}</ETag></Part>"
            )
        body.append("</CompleteMultipartUpload>")
        r = self._http.post(
            url,
            params={"uploadId": upload_id},
            content="".join(body).encode(),
            headers={"Content-Type": "application/xml"},
        )
        r.raise_for_status()
        # S3 can report a failure inside a 200 response, so the body is checked
        # rather than trusted.
        if "<Error" in r.text:
            raise RuntimeError(f"multipart completion failed for {url}: {r.text[:200]}")

    def _abort_upload(self, url: str, upload_id: str) -> None:
        try:
            self._http.delete(url, params={"uploadId": upload_id})
        except httpx.HTTPError as exc:
            logger.warning("binarystore.multipart_abort_failed", error=str(exc))

    def probe(self) -> None:
        """Verify credentials and write access with a real round trip.

        Mirrors the filesystem probe: create an object, then remove it. A
        read-only check would let a misconfigured receiver start and fail on
        every cycle instead of at boot.
        """
        key = f"{self._cfg.prefix.strip('/')}/.airlift-probe" if self._cfg.prefix else ".airlift-probe"
        url = f"{self._bucket_url()}/{key}"
        r = self._http.put(url, content=b"ok", headers={"Content-Length": "2"})
        r.raise_for_status()
        self._http.delete(url)

    def close(self) -> None:
        self._http.close()

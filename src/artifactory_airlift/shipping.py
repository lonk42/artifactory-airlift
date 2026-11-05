"""Ship finished archives from the sender's spool to an Azure Blob container.

Optional, sender only, and off unless ``shipping.enabled`` is true. It is
for deployments where something other than airlift collects archives from an
object store rather than from the spool PVC. The spool itself is unchanged:
archives are built there exactly as before, and this runs afterwards.

Per archive, in lexical order so a chunked cycle goes up chunk by chunk:

1. hash it (MD5 for Azure's own integrity checks, SHA-256 as metadata for the
   collecting transport),
2. upload it as ``<shipping.azure.prefix>/<archive name>``, skipping the upload
   when an identical blob is already there,
3. move the local copy to ``spool/.shipped/``.

Step 3 is what reopens the sender's "one delta in flight" gate, which only
looks at finalised archives in the spool root. Nothing here ever waits on the
collecting transport, so a container that is not being emptied grows without
bound; that is the operator's side of the contract.

A blob only becomes visible in Azure once its upload commits (Put Blob, or
Put Block List for a staged upload), so a collector never sees half an
archive. A crash between upload and move re-runs the upload on restart; the
existence check turns that into a skip.

Authentication is the platform identity only (workload identity, or instance
metadata), via the same token code the Azure binarystore uses.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from tenacity import retry

from . import log, state
from .binarystore._retry import RETRY
from .binarystore._stream import RangeReader
from .binarystore.azure import BearerAuth, _block_id
from .config import Settings

logger = log.get("artifactory.shipping")

# Block size for staged uploads; Azure allows 50000 blocks per blob, so this
# caps a single archive at about 3 TiB, far past any sane max_archive_bytes.
_BLOCK_BYTES = 64 * 1024**2

_HASH_READ_BYTES = 4 * 1024**2

_LEDGER_KEEP_ROWS = 5000
_LEDGER_TRIM_SLACK = 1000


def shipped_dir(settings: Settings) -> Path:
    return settings.spool_dir / ".shipped"


def ledger_path(settings: Settings) -> Path:
    return settings.state_dir / "shipped.jsonl"


@dataclass(frozen=True, slots=True)
class _Digest:
    size: int
    md5: bytes
    sha256: str
    block_md5s: tuple[bytes, ...]

    @property
    def md5_b64(self) -> str:
        return base64.b64encode(self.md5).decode()


def _digest(path: Path, block_bytes: int) -> _Digest:
    """Hash the file once: whole-file MD5 and SHA-256, plus per-block MD5s.

    The per-block MD5s let Azure verify each staged block on arrival, so a
    corrupted block fails its own PUT and is retried, rather than surfacing
    only after the whole archive has been committed.
    """
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    blocks: list[bytes] = []
    size = 0
    with path.open("rb") as fh:
        while True:
            block = hashlib.md5()
            taken = 0
            while taken < block_bytes:
                data = fh.read(min(_HASH_READ_BYTES, block_bytes - taken))
                if not data:
                    break
                md5.update(data)
                sha256.update(data)
                block.update(data)
                taken += len(data)
            if not taken:
                break
            blocks.append(block.digest())
            size += taken
    return _Digest(size, md5.digest(), sha256.hexdigest(), tuple(blocks))


class AzureArchiveTarget:
    """Write archives, by name, into one Azure Blob container."""

    kind = "azure"

    def __init__(
        self,
        *,
        endpoint_url: str,
        container: str,
        prefix: str,
        credential,
        source_instance: str = "",
        single_put_max: int = 256 * 1024**2,
        block_bytes: int = _BLOCK_BYTES,
        timeout: float = 300.0,
    ) -> None:
        self._endpoint = endpoint_url.rstrip("/")
        self._container = container
        self._prefix = prefix.strip("/")
        self._credential = credential
        self._source = source_instance
        self._single_put_max = single_put_max
        self.block_bytes = block_bytes
        self._http = httpx.Client(auth=BearerAuth(credential), timeout=timeout)

    def key(self, name: str) -> str:
        return f"{self._prefix}/{name}" if self._prefix else name

    def url(self, name: str) -> str:
        return f"{self._endpoint}/{self._container}/{quote(self.key(name))}"

    def describe(self) -> str:
        where = f"{self._endpoint}/{self._container}"
        if self._prefix:
            where += f"/{self._prefix}"
        return f"azure {where} as {self._credential.describe()}"

    @retry(**RETRY)
    def _head(self, url: str) -> httpx.Response:
        r = self._http.head(url)
        if r.status_code not in (200, 404):
            r.raise_for_status()
        return r

    def already_present(self, name: str, digest: _Digest) -> bool:
        """True when a blob of this name with identical content is in place.

        Compared on size and stored Content-MD5, both of which this module
        always sets. A same-named blob that differs is overwritten: archive
        names carry the cycle id, so that can only be a damaged earlier
        upload, never somebody else's data.
        """
        r = self._head(self.url(name))
        if r.status_code != 200:
            return False
        return (
            r.headers.get("content-length") == str(digest.size)
            and r.headers.get("content-md5") == digest.md5_b64
        )

    def upload(self, path: Path, digest: _Digest) -> None:
        url = self.url(path.name)
        metadata = {"x-ms-meta-sha256": digest.sha256}
        if self._source:
            metadata["x-ms-meta-source_instance"] = self._source
        if digest.size <= self._single_put_max:
            self._put_single(path, url, digest, metadata)
        else:
            self._put_blocks(path, url, digest, metadata)

    @retry(**RETRY)
    def _put_single(
        self, path: Path, url: str, digest: _Digest, metadata: dict[str, str]
    ) -> None:
        with path.open("rb") as fh:
            r = self._http.put(
                url,
                content=fh,
                headers={
                    "Content-Length": str(digest.size),
                    # Verified by the service against the body, and stored as
                    # the blob's Content-MD5 property for the collector.
                    "Content-MD5": digest.md5_b64,
                    "Content-Type": "application/zstd",
                    "x-ms-blob-type": "BlockBlob",
                    **metadata,
                },
            )
        r.raise_for_status()

    def _put_blocks(
        self, path: Path, url: str, digest: _Digest, metadata: dict[str, str]
    ) -> None:
        ids: list[str] = []
        with path.open("rb") as fh:
            offset = 0
            for number, block_md5 in enumerate(digest.block_md5s, 1):
                length = min(self.block_bytes, digest.size - offset)
                block_id = _block_id(number)
                self._put_block(fh, offset, length, url, block_id, block_md5)
                ids.append(block_id)
                offset += length
        # Uncommitted blocks expire by themselves, so a failure before the
        # commit leaves nothing visible behind.
        self._commit(url, ids, digest, metadata)

    @retry(**RETRY)
    def _put_block(
        self,
        fh,
        offset: int,
        length: int,
        url: str,
        block_id: str,
        block_md5: bytes,
    ) -> None:
        fh.seek(offset)
        r = self._http.put(
            url,
            params={"comp": "block", "blockid": block_id},
            content=RangeReader(fh, length),
            headers={
                "Content-Length": str(length),
                "Content-MD5": base64.b64encode(block_md5).decode(),
            },
        )
        r.raise_for_status()

    @retry(**RETRY)
    def _commit(
        self, url: str, ids: list[str], digest: _Digest, metadata: dict[str, str]
    ) -> None:
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
                # Not verified by the service on a commit, only stored; every
                # block was verified on its own PUT above.
                "x-ms-blob-content-md5": digest.md5_b64,
                "x-ms-blob-content-type": "application/zstd",
                **metadata,
            },
        )
        r.raise_for_status()

    # -- doctor checks: one attempt each, no retry chain ----------------------

    def check_container(self) -> str:
        """Read the container's properties. Raises with the Azure error code.

        Proves the identity, the network path and that the container exists,
        and needs only read access.
        """
        r = self._http.get(
            f"{self._endpoint}/{self._container}", params={"restype": "container"}
        )
        _raise_with_code(r)
        return f"container {self._container} reachable as {self._credential.describe()}"

    def check_write(self) -> str:
        """Stage one block under a probe name, without committing it.

        Put Block needs the same write permission as an upload, but an
        uncommitted block is not a blob: it is not listed, a collector cannot
        fetch it, and Azure discards it after seven days. A committed probe
        object could be picked up by the transport this container feeds.
        """
        name = f".airlift-ship-probe-{int(time.time())}"
        r = self._http.put(
            self.url(name),
            params={"comp": "block", "blockid": _block_id(1)},
            content=b"ok",
            headers={"Content-Length": "2"},
        )
        _raise_with_code(r)
        return "write permission confirmed with an uncommitted block (not listed, expires in 7 days)"

    def close(self) -> None:
        self._http.close()
        self._credential.close()


_ERROR_HINTS = {
    "AuthorizationPermissionMismatch": (
        "the identity lacks a data-plane role; grant Storage Blob Data "
        "Contributor on the container"
    ),
    "AuthorizationFailure": (
        "request refused, often a storage firewall or network rule blocking this pod"
    ),
    "ContainerNotFound": "the container does not exist; airlift does not create it",
    "InvalidAuthenticationInfo": "the token was rejected; check the identity's tenant",
}


def _raise_with_code(r: httpx.Response) -> None:
    if r.is_success:
        return
    code = r.headers.get("x-ms-error-code", "")
    hint = _ERROR_HINTS.get(code)
    detail = f"HTTP {r.status_code} {code}".rstrip()
    raise RuntimeError(f"{detail}: {hint}" if hint else detail)


def endpoint_url(settings: Settings) -> str:
    azure = settings.shipping.azure
    if azure.endpoint:
        return azure.endpoint.rstrip("/")
    return f"https://{azure.account}.blob.core.windows.net"


def describe_target(settings: Settings) -> str:
    """The configured destination as a URL, without contacting it."""
    azure = settings.shipping.azure
    where = f"{endpoint_url(settings)}/{azure.container}"
    prefix = azure.prefix.strip("/")
    return f"{where}/{prefix}" if prefix else where


def build_target(settings: Settings) -> AzureArchiveTarget:
    """Construct the configured target. Raises when no identity is available.

    ``shipping.method`` has one value today; a second method branches here.
    """
    from .binarystore.azure_identity import detect

    credential = detect()
    return AzureArchiveTarget(
        endpoint_url=endpoint_url(settings),
        container=settings.shipping.azure.container,
        prefix=settings.shipping.azure.prefix,
        credential=credential,
        source_instance=settings.instance_name,
        single_put_max=settings.binarystore_multipart_threshold,
    )


def pending(settings: Settings) -> list[Path]:
    """Finalised archives in the spool root, in upload order."""
    return sorted(settings.spool_dir.glob("*.tar.zst"))


class Shipper:
    """Drains the spool into the target, one call per sender loop tick.

    Never raises: like the binarystore, an unusable target means archives wait
    in the spool (which also holds the sender's gate shut) rather than a dead
    process taking Artifactory's pod with it.
    """

    def __init__(self, settings: Settings, *, target=None) -> None:
        self._settings = settings
        self._target = target
        self._attempt = 0

    def _acquire(self):
        if self._target is not None:
            return self._target
        self._attempt += 1
        try:
            self._target = build_target(self._settings)
        except Exception as exc:
            logger.error(
                "shipping.target_unavailable", error=str(exc), attempt=self._attempt
            )
            return None
        logger.info(
            "shipping.target_selected",
            detail=self._target.describe(),
            attempts=self._attempt,
        )
        self._attempt = 0
        return self._target

    def run_once(self) -> int:
        """Ship everything pending. Returns how many archives were shipped."""
        try:
            _prune_shipped(shipped_dir(self._settings), self._settings.shipping.keep_hours)
        except OSError as exc:
            logger.warning("shipping.prune_failed", error=str(exc))

        archives = pending(self._settings)
        if not archives:
            return 0
        target = self._acquire()
        if target is None:
            return 0

        shipped = 0
        for path in archives:
            if not self._ship_one(target, path):
                # Stop at the first failure so chunks never go up out of
                # order; the rest wait for the next tick.
                break
            shipped += 1
        return shipped

    def _ship_one(self, target, path: Path) -> bool:
        started = time.time()
        row: dict = {"archive": path.name}
        try:
            digest = _digest(path, target.block_bytes)
            row.update(bytes=digest.size, sha256=digest.sha256)
            if target.already_present(path.name, digest):
                row["status"] = "already-present"
            else:
                target.upload(path, digest)
                row["status"] = "ok"
            row["url"] = target.url(path.name)
        except Exception as exc:
            logger.error("shipping.upload_failed", archive=path.name, error=str(exc))
            self._record(row | {"status": "failed", "error": str(exc)}, started)
            return False

        try:
            _retire(path, shipped_dir(self._settings), self._settings.shipping.keep_hours)
        except OSError as exc:
            # Uploaded but still in the spool root. Next tick finds the blob
            # already present and only retries this step.
            logger.error("shipping.retire_failed", archive=path.name, error=str(exc))
            return False
        self._record(row, started)
        logger.info(
            "shipping.archive_shipped",
            archive=path.name,
            size_bytes=digest.size,
            size_human=log.human_bytes(digest.size),
            url=row["url"],
            skipped=row["status"] == "already-present",
            duration_ms=int((time.time() - started) * 1000),
        )
        return True

    def _record(self, row: dict, started: float) -> None:
        row = {"at": int(time.time()), "duration_ms": int((time.time() - started) * 1000)} | row
        if "error" in row:
            row["error"] = " ".join(str(row["error"]).split())[:300]
        path = ledger_path(self._settings)
        try:
            state.append_jsonl(path, row)
            state.trim_jsonl(path, keep=_LEDGER_KEEP_ROWS, slack=_LEDGER_TRIM_SLACK)
        except OSError as exc:
            logger.warning("shipping.ledger_write_failed", error=str(exc))

    def close(self) -> None:
        if self._target is not None:
            self._target.close()
            self._target = None


def _retire(path: Path, dest_dir: Path, keep_hours: int) -> None:
    """Take a shipped archive out of the spool root.

    Moved rather than deleted when ``keep_hours`` is set, so a recent archive
    can still be inspected or re-shipped by hand. The mtime is bumped so
    retention counts from shipping, not from when the archive was built.
    """
    if keep_hours <= 0:
        path.unlink(missing_ok=True)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    os.replace(path, dest)
    os.utime(dest)


def _prune_shipped(dest_dir: Path, keep_hours: int) -> None:
    if not dest_dir.is_dir():
        return
    cutoff = time.time() - keep_hours * 3600
    for p in dest_dir.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            continue

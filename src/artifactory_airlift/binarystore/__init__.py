"""Pluggable access to Artifactory's binarystore.

Airlift moves blobs by sha1, and every binary provider Artifactory ships keys
them the same way: ``<prefix>/<sha1[:2]>/<sha1>``. That makes the storage
backend a narrow seam. :class:`BlobStore` is that seam, with one implementation
per provider family, so the sender's archive builder and the receiver's blob
writer stay backend-agnostic.

The backend in use is detected per side from Artifactory's own
``binarystore.xml`` (see :mod:`.config`), which is why a sender on one object
store and a receiver on another need no special handling.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol, runtime_checkable

from .. import filestore, log
from .config import (
    AzureConfig,
    BinarystoreConfig,
    FilesystemConfig,
    S3Config,
    UnsupportedBinarystore,
    parse,
)

if TYPE_CHECKING:
    from ..config import Settings

logger = log.get("binarystore")

__all__ = [
    "BlobStore",
    "FilesystemBlobStore",
    "UnsupportedBinarystore",
    "resolve",
]


@runtime_checkable
class BlobStore(Protocol):
    """Content-addressed blob access, keyed by sha1."""

    def probe(self) -> None:
        """Verify the store is reachable and writable. Raises on failure."""

    def open(self, sha1: str) -> tuple[IO[bytes], int] | None:
        """Open a blob for reading.

        Returns a binary reader and the blob's size, or None when the blob is
        not present. The caller closes the reader.
        """

    def write(self, src: Path, sha1: str) -> bool:
        """Store a blob. Returns True if newly written, False if already present."""

    def close(self) -> None:
        """Release any underlying connections."""


class FilesystemBlobStore:
    """The original on-disk binarystore, wrapping :mod:`..filestore`.

    Kept as a thin adapter so the existing write semantics (atomic rename,
    best-effort chown, idempotent skip) stay exactly as they were.
    """

    kind = "filesystem"

    def __init__(self, root: Path, *, uid: int, gid: int) -> None:
        self.root = root
        self.uid = uid
        self.gid = gid

    def probe(self) -> None:
        filestore.probe(self.root, self.uid, self.gid)

    def open(self, sha1: str) -> tuple[IO[bytes], int] | None:
        path = filestore.blob_path(self.root, sha1)
        try:
            size = path.stat().st_size
            return path.open("rb"), size
        except OSError:
            return None

    def write(self, src: Path, sha1: str) -> bool:
        return filestore.write_blob(src, self.root, sha1, uid=self.uid, gid=self.gid)

    def close(self) -> None:
        return None

    def describe(self) -> str:
        return f"filesystem at {self.root}"


def _credential(configured: str, from_xml: str, setting: str, kind: str) -> str:
    """Resolve one credential, preferring explicit configuration.

    Configuration wins so an operator can always override, matching how env
    vars outrank the mounted config file elsewhere. The XML is the fallback,
    which pays off when the file is the Helm-rendered copy: that one still has
    plaintext credentials, so mounting it configures airlift completely.
    """
    if configured:
        return configured
    if from_xml:
        return from_xml
    raise UnsupportedBinarystore(
        f"binarystore.xml describes a {kind} backend but {setting} is not set, "
        "and the XML's own credentials are encrypted or absent. Supply the "
        "credential via configuration, or point binarystore_config at a copy "
        "of binarystore.xml that still holds plaintext credentials."
    )


def _build(cfg: BinarystoreConfig, settings: "Settings") -> BlobStore:
    if isinstance(cfg, S3Config):
        from .s3 import S3BlobStore

        return S3BlobStore(
            cfg,
            access_key=_credential(
                settings.binarystore_access_key,
                cfg.access_key,
                "binarystore_access_key",
                "S3",
            ),
            secret_key=_credential(
                settings.binarystore_secret_key,
                cfg.secret_key,
                "binarystore_secret_key",
                "S3",
            ),
            verify=settings.binarystore_ca_cert or True,
            multipart_threshold=settings.binarystore_multipart_threshold,
        )

    if isinstance(cfg, AzureConfig):
        from .azure import AzureBlobStore

        return AzureBlobStore(
            cfg,
            account_key=_credential(
                settings.binarystore_account_key,
                cfg.account_key,
                "binarystore_account_key",
                "Azure",
            ),
            verify=settings.binarystore_ca_cert or True,
            multipart_threshold=settings.binarystore_multipart_threshold,
        )

    root = cfg.root or settings.filestore_root
    return FilesystemBlobStore(
        root, uid=settings.artifactory_uid, gid=settings.artifactory_gid
    )


def resolve(settings: "Settings") -> BlobStore:
    """Build the blob store for this side, detecting the backend from the XML.

    ``binarystore_provider`` overrides detection: ``filesystem`` forces the
    legacy on-disk path (an escape hatch if the XML cannot be parsed), while
    ``s3`` and ``azure`` assert that the detected backend is the expected one
    and fail loudly otherwise. That assertion matters because the failure mode
    of guessing wrong is silent: blobs would be written where Artifactory never
    looks for them.
    """
    forced = settings.binarystore_provider.strip().lower()

    if forced == "filesystem":
        store = FilesystemBlobStore(
            settings.filestore_root,
            uid=settings.artifactory_uid,
            gid=settings.artifactory_gid,
        )
        logger.info("binarystore.selected", backend=store.kind, detected="forced")
        return store

    cfg = parse(Path(settings.binarystore_config))
    if cfg is None:
        # No usable XML. Every deployment before object-storage support
        # behaved this way, so this is the compatible default rather than an
        # error.
        cfg = FilesystemConfig()

    if forced in ("s3", "azure") and cfg.kind != forced:
        raise UnsupportedBinarystore(
            f"binarystore_provider is {forced!r} but binarystore.xml describes a "
            f"{cfg.kind!r} backend"
        )

    store = _build(cfg, settings)
    logger.info(
        "binarystore.selected",
        backend=cfg.kind,
        detected="auto" if forced == "auto" else "asserted",
        detail=store.describe(),
    )
    return store


def sha1_key(prefix: str, sha1: str) -> str:
    """Object key for a blob: ``<prefix>/<sha1[:2]>/<sha1>``.

    The same two-character fan-out Artifactory uses on disk. Verified against a
    live S3-backed instance, where sha1 5f85c6df... is stored under
    ``artifactory/filestore/5f/5f85c6df...``.
    """
    if len(sha1) < 2:
        raise ValueError(f"sha1 too short: {sha1!r}")
    key = f"{sha1[:2]}/{sha1}"
    return f"{prefix.strip('/')}/{key}" if prefix else key

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import log

logger = log.get("artifactory.filestore")


def blob_path(root: Path, sha1: str) -> Path:
    if len(sha1) < 2:
        raise ValueError(f"sha1 too short: {sha1!r}")
    return root / sha1[:2] / sha1


def write_blob(
    src: Path,
    root: Path,
    sha1: str,
    *,
    uid: int,
    gid: int,
    mode: int = 0o640,
) -> bool:
    """Place a blob into the filestore.

    Returns True if the blob was newly written, False if it was already
    present (content-addressed → idempotent).
    """
    dest = blob_path(root, sha1)
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(f".tmp-{os.getpid()}")
    shutil.copyfile(src, tmp)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    try:
        os.chown(tmp, uid, gid)
    except PermissionError:
        # When running as the artifactory user already, chown to self is a no-op
        # and chown to other uids will fail. The Helm spec runs us as 1030:1030
        # so this should match the destination; log and continue.
        logger.debug("filestore.chown_skipped", path=str(tmp))
    os.replace(tmp, dest)
    return True


def probe(root: Path, uid: int, gid: int) -> None:
    """Verify we can write into the filestore as the artifactory user."""
    root.mkdir(parents=True, exist_ok=True)
    probe_dir = root / ".airlift-probe"
    probe_dir.mkdir(exist_ok=True)
    probe = probe_dir / f"probe-{os.getpid()}"
    probe.write_bytes(b"ok")
    probe.unlink()
    probe_dir.rmdir()

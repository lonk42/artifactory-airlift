"""Test helper for building a filesystem-backed BlobStore."""

import os
from pathlib import Path

from artifactory_airlift.binarystore import FilesystemBlobStore


def fs_store(root: Path) -> FilesystemBlobStore:
    """Filesystem store rooted at ``root``.

    uid/gid are pinned to the current process so the chown in write_blob is a
    no-op rather than a permission error under an unprivileged test runner.
    """
    return FilesystemBlobStore(root, uid=os.getuid(), gid=os.getgid())

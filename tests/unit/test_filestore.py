import os
from pathlib import Path

from artifactory_airlift import filestore


def test_blob_path_layout(tmp_path: Path) -> None:
    sha1 = "abcd" + "0" * 36
    p = filestore.blob_path(tmp_path, sha1)
    assert p == tmp_path / "ab" / sha1


def test_write_blob_creates_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "filestore"
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello world")
    sha1 = "0123456789abcdef" + "0" * 24
    uid, gid = os.getuid(), os.getgid()

    created = filestore.write_blob(src, root, sha1, uid=uid, gid=gid)
    assert created is True
    dest = root / sha1[:2] / sha1
    assert dest.read_bytes() == b"hello world"

    created2 = filestore.write_blob(src, root, sha1, uid=uid, gid=gid)
    assert created2 is False  # already present


def test_probe(tmp_path: Path) -> None:
    filestore.probe(tmp_path / "fs", uid=os.getuid(), gid=os.getgid())

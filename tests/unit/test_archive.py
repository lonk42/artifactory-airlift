from pathlib import Path

from artifactory_airlift import archive
from artifactory_airlift.export_unpacker import ArtifactEntry


def _make_export(tmp_path: Path, repo: str, artifact: str, sha1: str) -> Path:
    """Build a minimal system-export tree under tmp_path/export."""
    root = tmp_path / "export"
    repo_dir = root / "repositories" / repo
    repo_dir.mkdir(parents=True)
    (repo_dir / artifact).write_bytes(b"")  # zero-byte placeholder
    meta_dir = repo_dir / f"{artifact}.artifactory-metadata"
    meta_dir.mkdir()
    (meta_dir / "fileinfo").write_text(
        f"<fileinfo><sha1>{sha1}</sha1><size>11</size></fileinfo>"
    )
    return root


def test_build_and_extract_roundtrip(tmp_path: Path) -> None:
    sha1 = "0123456789abcdef" + "0" * 24
    export_root = _make_export(tmp_path, "r1", "blob.bin", sha1)

    filestore_root = tmp_path / "filestore"
    blob = filestore_root / sha1[:2] / sha1
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"hello world")

    spool = tmp_path / "spool"
    cycle_id = archive.new_cycle_id()
    entry = ArtifactEntry(repo_key="r1", repo_path="blob.bin", sha1=sha1, size=11)

    archive_path = archive.build(
        spool_dir=spool,
        cycle_id=cycle_id,
        prev_cycle_id=None,
        source_instance="art-a",
        export_root=export_root,
        entries=[entry],
        filestore_root=filestore_root,
    )
    assert archive_path.is_file()
    assert archive_path.name == f"{cycle_id}.tar.zst"

    manifest = archive.read_manifest(archive_path)
    assert manifest.cycle_id == cycle_id
    assert manifest.prev_cycle_id is None
    assert manifest.repos == ["r1"]
    assert manifest.blob_count == 1
    assert manifest.total_bytes == len(b"hello world")

    dest = tmp_path / "extracted"
    extracted_manifest = archive.extract(archive_path, dest)
    assert extracted_manifest.cycle_id == cycle_id
    assert (dest / archive.BLOBS_PREFIX / sha1[:2] / sha1).read_bytes() == b"hello world"
    assert (
        dest / archive.METADATA_PREFIX / "repositories" / "r1" / "blob.bin"
    ).is_file()


def test_partial_archive_invisible_to_glob(tmp_path: Path) -> None:
    """Receiver scans *.tar.zst; partial files must not match."""
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "111.tar.zst.partial").write_text("nope")
    (spool / "222.tar.zst").write_text("ok")
    matched = sorted(p.name for p in spool.glob("*.tar.zst"))
    assert matched == ["222.tar.zst"]

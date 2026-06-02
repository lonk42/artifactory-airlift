from pathlib import Path

from artifactory_airlift import export_unpacker


def _meta(meta_dir: Path, sha1: str, size: int) -> None:
    meta_dir.mkdir(parents=True)
    (meta_dir / "fileinfo").write_text(
        f"<fileinfo><sha1>{sha1}</sha1><size>{size}</size></fileinfo>"
    )


def test_iter_artifacts_nested_paths(tmp_path: Path) -> None:
    root = tmp_path / "export"
    a = root / "repositories" / "r1" / "sub" / "blob.bin.artifactory-metadata"
    _meta(a, "a" * 40, 100)
    b = root / "repositories" / "r2" / "top.bin.artifactory-metadata"
    _meta(b, "b" * 40, 200)

    entries = sorted(export_unpacker.iter_artifacts(root), key=lambda e: e.sha1)
    assert [(e.repo_key, e.repo_path, e.sha1, e.size) for e in entries] == [
        ("r1", "sub/blob.bin", "a" * 40, 100),
        ("r2", "top.bin", "b" * 40, 200),
    ]


def test_iter_artifacts_real_artifactory_file_xml(tmp_path: Path) -> None:
    """The shape Artifactory 7.x system-export actually writes."""
    root = tmp_path / "export"
    meta = root / "repositories" / "r1" / "blob.bin.artifactory-metadata"
    meta.mkdir(parents=True)
    (meta / "artifactory-file.xml").write_text(
        '<artifactory-file>'
        '  <size>12</size>'
        '  <additionalInfo>'
        '    <checksumsInfo>'
        '      <checksums>'
        '        <checksum><type>sha1</type><actual>' + "a" * 40 + '</actual></checksum>'
        '        <checksum><type>md5</type><actual>' + "b" * 32 + '</actual></checksum>'
        '      </checksums>'
        '    </checksumsInfo>'
        '  </additionalInfo>'
        '</artifactory-file>'
    )
    # A folder descriptor in the same repo should not produce an entry.
    (root / "repositories" / "r1.artifactory-metadata").mkdir(parents=True)
    (root / "repositories" / "r1.artifactory-metadata" / "artifactory-folder.xml").write_text(
        "<artifactory-folder><name>r1</name></artifactory-folder>"
    )

    entries = list(export_unpacker.iter_artifacts(root))
    assert [(e.repo_key, e.repo_path, e.sha1, e.size) for e in entries] == [
        ("r1", "blob.bin", "a" * 40, 12),
    ]


def test_iter_artifacts_excluded_repos_skipped(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _meta(
        root / "repositories" / "real-repo" / "blob.bin.artifactory-metadata",
        "a" * 40,
        100,
    )
    _meta(
        root
        / "repositories"
        / "artifactory-build-info"
        / "build1.json.artifactory-metadata",
        "b" * 40,
        50,
    )
    _meta(
        root / "repositories" / "jfrog-usage-logs" / "log.txt.artifactory-metadata",
        "c" * 40,
        10,
    )

    excluded = {"artifactory-build-info", "jfrog-usage-logs"}
    entries = list(export_unpacker.iter_artifacts(root, excluded_repos=excluded))
    assert [e.repo_key for e in entries] == ["real-repo"]

    # write_snapshot must forward the filter end-to-end.
    snap = tmp_path / "snap.jsonl"
    count = export_unpacker.write_snapshot(root, snap, excluded_repos=excluded)
    assert count == 1
    assert "real-repo" in snap.read_text()
    assert "artifactory-build-info" not in snap.read_text()


def test_iter_artifacts_included_repos_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _meta(root / "repositories" / "keep-a" / "x.bin.artifactory-metadata", "a" * 40, 1)
    _meta(root / "repositories" / "keep-b" / "y.bin.artifactory-metadata", "b" * 40, 1)
    _meta(root / "repositories" / "drop-me" / "z.bin.artifactory-metadata", "c" * 40, 1)

    included = {"keep-a", "keep-b"}
    entries = list(export_unpacker.iter_artifacts(root, included_repos=included))
    assert sorted(e.repo_key for e in entries) == ["keep-a", "keep-b"]

    # An empty/None allowlist means no filtering (every repo eligible).
    all_entries = list(export_unpacker.iter_artifacts(root, included_repos=set()))
    assert sorted(e.repo_key for e in all_entries) == ["drop-me", "keep-a", "keep-b"]

    # write_snapshot must forward the filter end-to-end.
    snap = tmp_path / "snap.jsonl"
    count = export_unpacker.write_snapshot(root, snap, included_repos=included)
    assert count == 2
    assert "drop-me" not in snap.read_text()


def test_iter_artifacts_allowlist_and_exclusion_combine(tmp_path: Path) -> None:
    # A repo in both lists is dropped: allowlist narrows, exclusion still wins.
    root = tmp_path / "export"
    _meta(root / "repositories" / "keep" / "x.bin.artifactory-metadata", "a" * 40, 1)
    _meta(root / "repositories" / "both" / "y.bin.artifactory-metadata", "b" * 40, 1)

    entries = list(
        export_unpacker.iter_artifacts(
            root, included_repos={"keep", "both"}, excluded_repos={"both"}
        )
    )
    assert [e.repo_key for e in entries] == ["keep"]


def test_write_snapshot_sorted_by_sha1(tmp_path: Path) -> None:
    root = tmp_path / "export"
    _meta(root / "repositories" / "r" / "z.bin.artifactory-metadata", "b" * 40, 1)
    _meta(root / "repositories" / "r" / "a.bin.artifactory-metadata", "a" * 40, 1)

    snap = tmp_path / "snap.jsonl"
    count = export_unpacker.write_snapshot(root, snap)
    assert count == 2
    lines = snap.read_text().splitlines()
    # sha1 "aaa..." comes before "bbb..."
    assert "a" * 40 in lines[0]
    assert "b" * 40 in lines[1]

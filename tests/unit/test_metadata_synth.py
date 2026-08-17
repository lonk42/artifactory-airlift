"""Synthesis of the per-artifact metadata tree.

Airlift no longer asks Artifactory for a system export; it writes the same
XML itself for the changed artifacts only. Artifactory parses what comes out,
so shape errors here surface on the destination as a failed import or, worse,
as an artifact that imports with the wrong dates or checksums.
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from artifactory_airlift import metadata_synth
from artifactory_airlift.export_unpacker import ArtifactEntry


def _row(**overrides) -> dict:
    row = {
        "repo": "airlift-helm-local",
        "path": "charts",
        "name": "demo-1.0.0.tgz",
        "size": 4096,
        "created": "2026-05-13T01:34:15.924Z",
        "modified": "2026-05-13T01:34:16.100Z",
        "updated": "2026-05-13T01:34:16.200Z",
        "created_by": "admin",
        "modified_by": "admin",
        "actual_sha1": "aa" * 20,
        "actual_md5": "bb" * 16,
        "sha256": "cc" * 32,
    }
    row.update(overrides)
    return row


# --- _epoch_ms -----------------------------------------------------------


def test_epoch_ms_parses_an_aql_timestamp() -> None:
    assert metadata_synth._epoch_ms("2026-05-13T01:34:15.924Z") == 1778636055924


def test_epoch_ms_keeps_millisecond_resolution() -> None:
    assert metadata_synth._epoch_ms("1970-01-01T00:00:01.500Z") == 1500


def test_epoch_ms_falls_back_to_zero_when_absent() -> None:
    """Zero means "unknown" to Artifactory, which substitutes the import
    time. A worse mirror, but never a failed one, so this must not raise."""
    assert metadata_synth._epoch_ms(None) == 0
    assert metadata_synth._epoch_ms("") == 0


def test_epoch_ms_falls_back_to_zero_on_an_unparseable_value() -> None:
    for value in ("2026-05-13", "not a timestamp", "2026-05-13T01:34:15Z"):
        assert metadata_synth._epoch_ms(value) == 0


# --- file_xml ------------------------------------------------------------


def test_file_xml_shape() -> None:
    xml = ET.fromstring(metadata_synth.file_xml(_row(), "charts/demo-1.0.0.tgz"))
    assert xml.tag == "artifactory-file"
    assert xml.findtext("repoPath/repoKey") == "airlift-helm-local"
    # The path element carries the filename too, as a real export writes it.
    assert xml.findtext("repoPath/path") == "charts/demo-1.0.0.tgz"
    assert xml.findtext("repoPath/folder") == "false"
    assert xml.findtext("name") == "demo-1.0.0.tgz"
    assert xml.findtext("created") == "1778636055924"
    assert xml.findtext("lastModified") == "1778636056100"
    assert xml.findtext("size") == "4096"
    assert xml.findtext("additionalInfo/createdBy") == "admin"
    assert xml.findtext("additionalInfo/modifiedBy") == "admin"
    assert xml.findtext("additionalInfo/lastUpdated") == "1778636056200"


def _checksums(row: dict, repo_path: str = "charts/demo-1.0.0.tgz") -> dict:
    xml = ET.fromstring(metadata_synth.file_xml(row, repo_path))
    return {
        c.findtext("type"): (c.findtext("actual"), c.findtext("original"))
        for c in xml.findall("additionalInfo/checksumsInfo/checksums/checksum")
    }


def test_file_xml_carries_all_three_checksums() -> None:
    got = _checksums(_row())
    assert {k: v[0] for k, v in got.items()} == {
        "sha1": "aa" * 20,
        "md5": "bb" * 16,
        "sha256": "cc" * 32,
    }


def test_sha256_always_carries_an_original_equal_to_actual() -> None:
    """Matches what a real export writes; AQL has no original_sha256 field."""
    sha256 = _checksums(_row())["sha256"]
    assert sha256 == ("cc" * 32, "cc" * 32)


def test_sha1_and_md5_omit_original_when_the_client_declared_none() -> None:
    """Emitting one unconditionally shows up on the destination as an
    originalChecksums block the source does not have, which is a visible
    divergence in a mirror."""
    got = _checksums(_row())
    assert got["sha1"][1] is None
    assert got["md5"][1] is None


def test_sha1_and_md5_carry_a_declared_original_when_there_is_one() -> None:
    got = _checksums(_row(original_sha1="dd" * 20, original_md5="ee" * 16))
    assert got["sha1"] == ("aa" * 20, "dd" * 20)
    assert got["md5"] == ("bb" * 16, "ee" * 16)


def test_file_xml_omits_a_checksum_the_source_does_not_have() -> None:
    xml = ET.fromstring(
        metadata_synth.file_xml(_row(sha256=None), "charts/demo-1.0.0.tgz")
    )
    types = {
        c.findtext("type")
        for c in xml.findall("additionalInfo/checksumsInfo/checksums/checksum")
    }
    assert types == {"sha1", "md5"}


def test_file_xml_escapes_markup_in_values() -> None:
    xml = metadata_synth.file_xml(
        _row(created_by="a<b>&c"), "charts/x&y.tgz"
    )
    assert "a<b>&c" not in xml
    parsed = ET.fromstring(xml)
    assert parsed.findtext("additionalInfo/createdBy") == "a<b>&c"
    assert parsed.findtext("repoPath/path") == "charts/x&y.tgz"


def test_file_xml_tolerates_a_row_with_nothing_in_it() -> None:
    xml = ET.fromstring(metadata_synth.file_xml({}, "a.bin"))
    assert xml.findtext("size") == "0"
    assert xml.findtext("created") == "0"


# --- properties_xml ------------------------------------------------------


def test_properties_xml_renders_each_key_as_an_element() -> None:
    row = _row(
        properties=[
            {"key": "docker.manifest", "value": "1.0.0"},
            {"key": "sha256", "value": "cc" * 32},
        ]
    )
    xml = ET.fromstring(metadata_synth.properties_xml(row))
    assert xml.tag == "properties"
    assert [c.tag for c in xml] == ["docker.manifest", "sha256"]
    assert xml.findtext("sha256") == "cc" * 32


def test_properties_xml_repeats_a_multi_valued_property() -> None:
    """AQL returns one row per key/value pair, so a multi-valued property
    arrives as repeated keys and must be emitted as repeated elements."""
    row = _row(
        properties=[
            {"key": "docker.label", "value": "one"},
            {"key": "docker.label", "value": "two"},
        ]
    )
    xml = ET.fromstring(metadata_synth.properties_xml(row))
    assert [c.text for c in xml.findall("docker.label")] == ["one", "two"]


def test_properties_xml_is_none_without_properties() -> None:
    assert metadata_synth.properties_xml(_row()) is None
    assert metadata_synth.properties_xml(_row(properties=[])) is None


def test_properties_xml_drops_a_key_that_is_not_a_legal_element_name() -> None:
    """A key Artifactory cannot render as an element would produce a tree it
    then refuses to parse, failing the whole repository's import."""
    row = _row(
        properties=[
            {"key": "ok.key-1", "value": "v"},
            {"key": "1leading-digit", "value": "v"},
            {"key": "has space", "value": "v"},
            {"key": "", "value": "v"},
        ]
    )
    xml = ET.fromstring(metadata_synth.properties_xml(row))
    assert [c.tag for c in xml] == ["ok.key-1"]


def test_properties_xml_is_none_when_every_key_was_dropped() -> None:
    assert metadata_synth.properties_xml(_row(properties=[{"key": "!", "value": "v"}])) is None


def test_properties_xml_escapes_markup_in_values() -> None:
    row = _row(properties=[{"key": "note", "value": "<a> & <b>"}])
    xml = metadata_synth.properties_xml(row)
    assert "<a>" not in xml.replace("<note>", "")
    assert ET.fromstring(xml).findtext("note") == "<a> & <b>"


# --- build_tree ----------------------------------------------------------


def test_build_tree_writes_the_export_layout(tmp_path: Path) -> None:
    entry = ArtifactEntry("airlift-helm-local", "charts/demo-1.0.0.tgz", "aa" * 20, 4096)
    row = _row(properties=[{"key": "chart.name", "value": "demo"}])
    written, unresolved = metadata_synth.build_tree(
        tmp_path, [entry], {(entry.repo_key, entry.repo_path): row}
    )
    assert (written, unresolved) == (1, [])
    meta_dir = (
        tmp_path
        / "repositories"
        / "airlift-helm-local"
        / "charts"
        / "demo-1.0.0.tgz.artifactory-metadata"
    )
    assert sorted(p.name for p in meta_dir.iterdir()) == [
        "artifactory-file.xml",
        "properties.xml",
    ]
    parsed = ET.fromstring((meta_dir / "artifactory-file.xml").read_text())
    assert parsed.findtext("repoPath/path") == "charts/demo-1.0.0.tgz"


def test_build_tree_emits_only_the_repositories_subtree(tmp_path: Path) -> None:
    """etc/, artifactory.config.xml and licenses/ exist for /api/import/system,
    which airlift does not use."""
    entry = ArtifactEntry("r", "a.bin", "aa", 1)
    metadata_synth.build_tree(tmp_path, [entry], {("r", "a.bin"): _row()})
    assert [p.name for p in tmp_path.iterdir()] == ["repositories"]


def test_build_tree_omits_properties_xml_when_there_are_none(tmp_path: Path) -> None:
    entry = ArtifactEntry("r", "a.bin", "aa", 1)
    metadata_synth.build_tree(tmp_path, [entry], {("r", "a.bin"): _row()})
    meta_dir = tmp_path / "repositories" / "r" / "a.bin.artifactory-metadata"
    assert [p.name for p in meta_dir.iterdir()] == ["artifactory-file.xml"]


def test_build_tree_reports_entries_whose_metadata_is_missing(tmp_path: Path) -> None:
    """A synthesised record with no checksum would ask the receiver to import
    an artifact it cannot link to a blob, so the entry is reported, not guessed."""
    have = ArtifactEntry("r", "a.bin", "aa", 1)
    gone = ArtifactEntry("r", "b.bin", "bb", 1)
    written, unresolved = metadata_synth.build_tree(
        tmp_path, [have, gone], {("r", "a.bin"): _row()}
    )
    assert written == 1
    assert unresolved == [gone]
    assert not (tmp_path / "repositories" / "r" / "b.bin.artifactory-metadata").exists()


def test_build_tree_keys_metadata_on_repo_and_path(tmp_path: Path) -> None:
    """The same path in two repositories must not resolve to one another."""
    a = ArtifactEntry("repo-a", "x.bin", "aa", 1)
    b = ArtifactEntry("repo-b", "x.bin", "bb", 1)
    written, unresolved = metadata_synth.build_tree(
        tmp_path,
        [a, b],
        {("repo-a", "x.bin"): _row(repo="repo-a", actual_sha1="aa")},
    )
    assert (written, unresolved) == (1, [b])


def test_build_tree_creates_the_root_for_an_empty_delta(tmp_path: Path) -> None:
    """A removals-only cycle still ships a tree; the import call needs one."""
    written, unresolved = metadata_synth.build_tree(tmp_path, [], {})
    assert (written, unresolved) == (0, [])
    assert (tmp_path / "repositories").is_dir()

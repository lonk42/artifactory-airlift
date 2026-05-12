from pathlib import Path

from artifactory_airlift import diff
from artifactory_airlift.export_unpacker import ArtifactEntry


def _write(path: Path, entries: list[ArtifactEntry]) -> None:
    entries_sorted = sorted(entries, key=lambda e: e.sha1)
    path.write_text("\n".join(e.to_json() for e in entries_sorted) + "\n")


def _e(sha1: str, repo: str, p: str, size: int = 1) -> ArtifactEntry:
    return ArtifactEntry(repo_key=repo, repo_path=p, sha1=sha1, size=size)


def test_added_empty_previous(tmp_path: Path) -> None:
    cur = tmp_path / "cur.jsonl"
    _write(cur, [_e("a" * 40, "r1", "x"), _e("b" * 40, "r1", "y")])
    result = list(diff.added(None, cur))
    assert {(e.sha1, e.repo_key, e.repo_path) for e in result} == {
        ("a" * 40, "r1", "x"),
        ("b" * 40, "r1", "y"),
    }


def test_added_no_changes(tmp_path: Path) -> None:
    prev = tmp_path / "p.jsonl"
    cur = tmp_path / "c.jsonl"
    entries = [_e("a" * 40, "r1", "x"), _e("b" * 40, "r1", "y")]
    _write(prev, entries)
    _write(cur, entries)
    assert list(diff.added(prev, cur)) == []


def test_added_new_blob(tmp_path: Path) -> None:
    prev = tmp_path / "p.jsonl"
    cur = tmp_path / "c.jsonl"
    _write(prev, [_e("a" * 40, "r1", "x")])
    _write(cur, [_e("a" * 40, "r1", "x"), _e("b" * 40, "r1", "y")])
    result = list(diff.added(prev, cur))
    assert [(e.sha1, e.repo_key, e.repo_path) for e in result] == [
        ("b" * 40, "r1", "y")
    ]


def test_added_same_sha1_new_repo_path(tmp_path: Path) -> None:
    prev = tmp_path / "p.jsonl"
    cur = tmp_path / "c.jsonl"
    sha = "a" * 40
    _write(prev, [_e(sha, "r1", "x")])
    _write(cur, [_e(sha, "r1", "x"), _e(sha, "r2", "y")])
    result = list(diff.added(prev, cur))
    assert [(e.sha1, e.repo_key, e.repo_path) for e in result] == [(sha, "r2", "y")]


def test_added_removed_blob_not_reported(tmp_path: Path) -> None:
    prev = tmp_path / "p.jsonl"
    cur = tmp_path / "c.jsonl"
    _write(prev, [_e("a" * 40, "r1", "x"), _e("b" * 40, "r1", "y")])
    _write(cur, [_e("b" * 40, "r1", "y")])
    # one-way add-only sync; removals are not propagated
    assert list(diff.added(prev, cur)) == []

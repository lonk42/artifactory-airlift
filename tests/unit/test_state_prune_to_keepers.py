from pathlib import Path

from artifactory_airlift import state


def test_prune_to_keepers_removes_non_keepers(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    c = tmp_path / "c.jsonl"
    for p in (a, b, c):
        p.write_text("")

    state.prune_to_keepers(tmp_path, {a, b}, pattern="*.jsonl")

    assert a.exists()
    assert b.exists()
    assert not c.exists()


def test_prune_to_keepers_pattern_filters(tmp_path: Path) -> None:
    snap = tmp_path / "snap.jsonl"
    note = tmp_path / "note.txt"
    snap.write_text("")
    note.write_text("")

    state.prune_to_keepers(tmp_path, set(), pattern="*.jsonl")

    assert not snap.exists()
    assert note.exists()


def test_prune_to_keepers_empty_dir_noop(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert state.prune_to_keepers(empty, set(), pattern="*.jsonl") == []


def test_prune_to_keepers_missing_dir_noop(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert state.prune_to_keepers(missing, set(), pattern="*.jsonl") == []

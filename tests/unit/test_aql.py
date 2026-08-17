"""AQL enumeration.

The tests that matter most here are the ones around ``_include()``. AQL
collapses adjacent duplicate rows over the projected field set, silently, so
a projection missing the natural key returns fewer rows than the source holds
and the sender reads the shortfall as deletions. The guard is structural (the
key is always emitted and callers may only append), and these tests pin that
it stays structural.
"""
from __future__ import annotations

import json
from pathlib import Path

from artifactory_airlift import aql
from artifactory_airlift.export_unpacker import ArtifactEntry


class _FakeClient:
    """Records every AQL query it is handed and replays canned rows.

    ``rows`` may be a single list (returned for every query) or a list of
    lists consumed in order, for tests that issue more than one query.
    """

    def __init__(self, rows) -> None:
        self.queries: list[str] = []
        self._rows = rows

    def aql(self, query: str) -> list[dict]:
        self.queries.append(query)
        if self._rows and isinstance(self._rows[0], list):
            return self._rows[len(self.queries) - 1]
        return self._rows


def _row(repo: str, path: str, name: str, sha1: str, size: int = 1) -> dict:
    return {
        "repo": repo,
        "path": path,
        "name": name,
        "actual_sha1": sha1,
        "size": size,
    }


# --- _include: the natural-key guard -------------------------------------


def test_include_always_emits_the_natural_key() -> None:
    assert aql._include() == '.include("repo","path","name")'


def test_include_appends_extra_fields_after_the_key() -> None:
    assert (
        aql._include("actual_sha1", "size")
        == '.include("repo","path","name","actual_sha1","size")'
    )


def test_include_never_drops_the_key_when_a_caller_repeats_it() -> None:
    """A caller naming a key field must not be able to reorder or remove it."""
    rendered = aql._include("name", "size", "repo")
    assert rendered == '.include("repo","path","name","size")'
    for field in aql._ITEM_KEY:
        assert f'"{field}"' in rendered


def test_snapshot_query_projects_the_key() -> None:
    """The query the enumeration actually issues, not just the helper."""
    client = _FakeClient([])
    list(aql.iter_artifacts(client))
    assert len(client.queries) == 1
    for field in aql._ITEM_KEY:
        assert f'"{field}"' in client.queries[0]


def test_metadata_query_projects_the_key() -> None:
    client = _FakeClient([])
    aql.fetch_metadata(client, [ArtifactEntry("r", "a/b.txt", "aa", 1)])
    assert len(client.queries) == 1
    for field in aql._ITEM_KEY:
        assert f'"{field}"' in client.queries[0]


# --- _repo_path ----------------------------------------------------------


def test_repo_path_joins_directory_and_name() -> None:
    assert aql._repo_path({"path": "a/b", "name": "c.txt"}) == "a/b/c.txt"


def test_repo_path_treats_dot_as_repository_root() -> None:
    """AQL reports a root-level artifact as path ".", not as an empty string."""
    assert aql._repo_path({"path": ".", "name": "c.txt"}) == "c.txt"


def test_repo_path_treats_a_missing_path_as_root() -> None:
    assert aql._repo_path({"name": "c.txt"}) == "c.txt"
    assert aql._repo_path({"path": None, "name": "c.txt"}) == "c.txt"


def test_repo_path_handles_a_deep_path() -> None:
    assert (
        aql._repo_path({"path": "a/b/c/d", "name": "e.tgz"}) == "a/b/c/d/e.tgz"
    )


# --- _criteria -----------------------------------------------------------


def test_criteria_restricts_to_files() -> None:
    assert json.loads(aql._criteria()) == {"type": "file"}


def test_criteria_pushes_the_allowlist_into_the_query() -> None:
    crit = json.loads(aql._criteria(included_repos={"b-repo", "a-repo"}))
    assert crit == {
        "type": "file",
        "$or": [{"repo": {"$eq": "a-repo"}}, {"repo": {"$eq": "b-repo"}}],
    }


def test_criteria_never_emits_a_set_operator() -> None:
    """AQL has neither $in nor $nin; both are rejected with a parse error.
    Membership is an $or of $eq, and its negation an $and of $ne."""
    rendered = aql._criteria(
        included_repos={"a", "b"}, excluded_repos={"c", "d"}
    )
    assert "$in" not in rendered
    assert "$nin" not in rendered


def test_criteria_pushes_the_denylist_into_the_query() -> None:
    crit = json.loads(
        aql._criteria(excluded_repos={"jfrog-usage-logs", "artifactory-build-info"})
    )
    assert crit == {
        "type": "file",
        "$and": [
            {"repo": {"$ne": "artifactory-build-info"}},
            {"repo": {"$ne": "jfrog-usage-logs"}},
        ],
    }


def test_denylist_uses_and_so_every_exclusion_survives_the_parse() -> None:
    """A JSON object cannot carry `repo` twice, so sibling $ne clauses would
    silently collapse to whichever one won the parse. Each exclusion must
    therefore appear as its own element of $and."""
    excluded = {f"repo-{i}" for i in range(5)}
    crit = json.loads(aql._criteria(excluded_repos=excluded))
    assert len(crit["$and"]) == 5
    assert {c["repo"]["$ne"] for c in crit["$and"]} == excluded


def test_criteria_carries_both_filters_together() -> None:
    crit = json.loads(
        aql._criteria(included_repos={"keep-me"}, excluded_repos={"drop-me"})
    )
    assert crit == {
        "type": "file",
        "$or": [{"repo": {"$eq": "keep-me"}}],
        "$and": [{"repo": {"$ne": "drop-me"}}],
    }


def test_criteria_omits_empty_filters() -> None:
    crit = json.loads(aql._criteria(included_repos=set(), excluded_repos=set()))
    assert crit == {"type": "file"}


# --- iter_artifacts ------------------------------------------------------


def test_iter_artifacts_maps_rows_to_entries() -> None:
    client = _FakeClient([_row("repo-a", "x/y", "f.bin", "AABBCC", 42)])
    (entry,) = list(aql.iter_artifacts(client))
    assert entry == ArtifactEntry(
        repo_key="repo-a", repo_path="x/y/f.bin", sha1="aabbcc", size=42
    )


def test_iter_artifacts_asks_the_server_to_apply_the_denylist() -> None:
    client = _FakeClient([_row("keep", ".", "a.bin", "aa")])
    list(aql.iter_artifacts(client, excluded_repos={"drop"}))
    assert '{"$ne":"drop"}' in client.queries[0]


def test_iter_artifacts_asks_the_server_to_apply_the_allowlist() -> None:
    client = _FakeClient([_row("keep", ".", "a.bin", "aa")])
    list(aql.iter_artifacts(client, included_repos={"keep"}))
    assert '{"$eq":"keep"}' in client.queries[0]


def test_iter_artifacts_still_drops_a_denied_repo_the_server_returned() -> None:
    """The query is the mechanism, but a clause the server declines to apply
    comes back as a healthy 200 with extra rows. The client-side gate is what
    stops those being mirrored."""
    client = _FakeClient(
        [
            _row("keep", ".", "a.bin", "aa"),
            _row("drop", ".", "b.bin", "bb"),
        ]
    )
    entries = list(aql.iter_artifacts(client, excluded_repos={"drop"}))
    assert [e.repo_key for e in entries] == ["keep"]


def test_iter_artifacts_still_drops_a_repo_outside_the_allowlist() -> None:
    client = _FakeClient(
        [
            _row("keep", ".", "a.bin", "aa"),
            _row("other", ".", "b.bin", "bb"),
        ]
    )
    entries = list(aql.iter_artifacts(client, included_repos={"keep"}))
    assert [e.repo_key for e in entries] == ["keep"]


def test_the_allowlist_gate_runs_before_the_denylist_gate() -> None:
    """Listing a system repo in the allowlist must not force it through."""
    client = _FakeClient([_row("sys", ".", "a.bin", "aa")])
    entries = list(
        aql.iter_artifacts(client, included_repos={"sys"}, excluded_repos={"sys"})
    )
    assert entries == []


def test_iter_artifacts_skips_rows_without_a_checksum() -> None:
    """An artifact Artifactory has not checksummed yet cannot be shipped,
    but it must not abort the cycle either."""
    client = _FakeClient(
        [
            _row("r", ".", "a.bin", "aa"),
            {"repo": "r", "path": ".", "name": "b.bin", "size": 1},
            {"repo": "r", "path": ".", "name": "c.bin", "actual_sha1": "", "size": 1},
        ]
    )
    entries = list(aql.iter_artifacts(client))
    assert [e.repo_path for e in entries] == ["a.bin"]


def test_iter_artifacts_defaults_a_missing_size_to_zero() -> None:
    client = _FakeClient([{"repo": "r", "path": ".", "name": "a", "actual_sha1": "aa"}])
    (entry,) = list(aql.iter_artifacts(client))
    assert entry.size == 0


# --- write_snapshot ------------------------------------------------------


def test_write_snapshot_sorts_by_sha1_and_returns_the_count(tmp_path: Path) -> None:
    client = _FakeClient(
        [
            _row("r", ".", "c.bin", "cc", 3),
            _row("r", ".", "a.bin", "aa", 1),
            _row("r", ".", "b.bin", "bb", 2),
        ]
    )
    dest = tmp_path / "snapshots" / "cycle.jsonl"
    count = aql.write_snapshot(client, dest)
    assert count == 3
    lines = dest.read_text().splitlines()
    assert [json.loads(line)["sha1"] for line in lines] == ["aa", "bb", "cc"]


def test_write_snapshot_matches_the_export_derived_format(tmp_path: Path) -> None:
    """Baselines written by either enumeration path must stay comparable."""
    client = _FakeClient([_row("r", "x", "a.bin", "aa", 7)])
    dest = tmp_path / "cycle.jsonl"
    aql.write_snapshot(client, dest)
    line = dest.read_text().splitlines()[0]
    assert json.loads(line) == {
        "repo": "r",
        "path": "x/a.bin",
        "sha1": "aa",
        "size": 7,
    }
    assert ArtifactEntry.from_json(line).repo_path == "x/a.bin"


def test_write_snapshot_leaves_no_temp_file(tmp_path: Path) -> None:
    client = _FakeClient([_row("r", ".", "a.bin", "aa")])
    dest = tmp_path / "cycle.jsonl"
    aql.write_snapshot(client, dest)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cycle.jsonl"]


# --- fetch_metadata ------------------------------------------------------


def test_fetch_metadata_queries_once_per_repository() -> None:
    client = _FakeClient(
        [
            [_row("r1", "a", "x.bin", "aa")],
            [_row("r2", ".", "y.bin", "bb")],
        ]
    )
    out = aql.fetch_metadata(
        client,
        [
            ArtifactEntry("r1", "a/x.bin", "aa", 1),
            ArtifactEntry("r2", "y.bin", "bb", 1),
        ],
    )
    assert len(client.queries) == 2
    assert set(out) == {("r1", "a/x.bin"), ("r2", "y.bin")}


def test_fetch_metadata_splits_the_path_back_into_path_and_name() -> None:
    client = _FakeClient([[]])
    aql.fetch_metadata(
        client,
        [ArtifactEntry("r", "a/b/c.bin", "aa", 1), ArtifactEntry("r", "top.bin", "bb", 1)],
    )
    crit = json.loads(client.queries[0].split("items.find(")[1].split(").include")[0])
    assert crit["repo"] == "r"
    assert crit["type"] == "file"
    assert crit["$or"] == [
        {"path": "a/b", "name": "c.bin"},
        {"path": ".", "name": "top.bin"},
    ]


def test_fetch_metadata_requests_properties() -> None:
    client = _FakeClient([[]])
    aql.fetch_metadata(client, [ArtifactEntry("r", "a.bin", "aa", 1)])
    assert '"property.key"' in client.queries[0]
    assert '"property.value"' in client.queries[0]


def test_fetch_metadata_issues_no_query_for_an_empty_delta() -> None:
    client = _FakeClient([])
    assert aql.fetch_metadata(client, []) == {}
    assert client.queries == []

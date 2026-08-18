"""The operator CLI.

Commands are exercised through their view functions rather than through
argparse, because the views are the contract: they return the dictionaries
that both the text renderer and a future UI consume.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from artifactory_airlift import aql
from artifactory_airlift.cli import actions, app, common, render, views
from artifactory_airlift.cli.common import CLIError
from artifactory_airlift.config import Settings


def _settings(tmp_path: Path, mode: str = "sender", **overrides) -> Settings:
    state = tmp_path / "state"
    spool = tmp_path / "spool"
    for p in (state, spool, state / "snapshots", state / "exports", spool / ".done"):
        p.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        mode=mode,
        instance_name="art-a",
        state_dir=state,
        spool_dir=spool,
        filestore_root=tmp_path / "filestore",
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


# -- time parsing -----------------------------------------------------------


def test_relative_windows_read_backwards() -> None:
    now = 1_700_000_000.0
    assert common.parse_when("7d", now=now) == now - 7 * 86400
    assert common.parse_when("30m", now=now) == now - 1800


def test_naive_timestamps_are_utc() -> None:
    # Airlift buckets snapshots in UTC, so a bare timestamp has to mean UTC
    # here too, or a history filter would silently shift by the pod's offset.
    expected = datetime(2026, 8, 18, tzinfo=timezone.utc).timestamp()
    assert common.parse_when("2026-08-18T00:00:00") == expected
    assert common.parse_when("2026-08-18T00:00:00Z") == expected


def test_an_unreadable_time_says_what_it_accepts() -> None:
    with pytest.raises(CLIError) as exc:
        common.parse_when("last tuesday")
    assert "ISO 8601" in str(exc.value)


# -- config -----------------------------------------------------------------


def test_env_masking_the_config_file_is_reported(tmp_path: Path, monkeypatch) -> None:
    """The trap this command exists for.

    Env vars outrank the mounted ConfigMap, and the chart renders every key
    into that ConfigMap whether or not the operator set it. "The file says X"
    and "airlift is using X" are therefore different statements, and the
    difference has to be visible.
    """
    monkeypatch.setenv("AIRLIFT_INCLUDED_REPOS", "one,two")
    settings = _settings(tmp_path, included_repos=["one", "two"])
    data = views.effective_config(settings, {"included_repos": [], "cycle_seconds": 300})

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["included_repos"]["origin"] == "env"
    assert fields["included_repos"]["masked_file_value"] == []
    assert fields["cycle_seconds"]["origin"] == "file"
    assert fields["mode"]["origin"] == "default"


def test_credentials_are_never_printed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, artifactory_token="secret-token-value")
    data = views.effective_config(settings, {})
    fields = {f["name"]: f for f in data["fields"]}
    assert "secret-token-value" not in json.dumps(data)
    assert fields["artifactory_token"]["value"] == "<set, 18 chars>"
    # A setting that only looks sensitive stays readable: the prefix is the
    # first thing to check when every blob read 404s.
    assert fields["binarystore_prefix"]["value"] == ""


# -- history ----------------------------------------------------------------


def _sender_rows(now: int) -> list[dict]:
    return [
        {"status": "ok", "at": now - 300, "cycle_id": "c1", "added": 3,
         "repos_added": {"repo-a": 3}, "archive_bytes": 2048},
        {"status": "no-changes", "at": now - 240, "cycle_id": "c2"},
        {"status": "skipped-pending", "at": now - 180, "note": "1 archive(s)"},
        {"status": "brake-refused", "at": now - 120, "cycle_id": "c3", "removed": 9,
         "note": "9 of 12 (75.0%)"},
        {"status": "ok", "at": now - 60, "cycle_id": "c4", "added": 1,
         "repos_added": {"repo-b": 1}, "deferred_blobs": 2},
    ]


def test_cycles_that_did_nothing_are_hidden_by_default(tmp_path: Path) -> None:
    now = int(time.time())
    settings = _settings(tmp_path)
    _write_ledger(common.sender_ledger_path(settings), _sender_rows(now))

    quiet_hidden = views.cycles(settings)
    assert [c["cycle_id"] for c in quiet_hidden["cycles"]] == ["c1", "c3", "c4"]

    everything = views.cycles(settings, include_quiet=True)
    assert everything["matched"] == 5


def test_cycles_filter_by_time_status_and_repo(tmp_path: Path) -> None:
    now = int(time.time())
    settings = _settings(tmp_path)
    _write_ledger(common.sender_ledger_path(settings), _sender_rows(now))

    assert views.cycles(settings, since=now - 90)["matched"] == 1
    assert views.cycles(settings, status_filter="brake-refused")["matched"] == 1
    assert views.cycles(settings, repo="repo-b")["matched"] == 1
    assert views.cycles(settings, repo="repo-missing")["matched"] == 0


def test_deferrals_surface_in_the_history_note(tmp_path: Path) -> None:
    now = int(time.time())
    settings = _settings(tmp_path)
    _write_ledger(common.sender_ledger_path(settings), _sender_rows(now))

    last = views.cycles(settings)["cycles"][-1]
    assert "2 blob(s) deferred" in last["note"]


def test_receiver_rows_normalise_into_the_same_shape(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="receiver")
    _write_ledger(
        common.receiver_ledger_path(settings),
        [
            {"cycle_id": "c1", "status": "ok", "blob_count": 4, "total_bytes": 900,
             "repos": ["repo-a"], "deleted_count": 1, "processed_at": 1700000000},
            {"cycle_id": "c2", "status": "partial", "blob_count": 0, "repos": [],
             "failures": ["500 : boom"], "processed_at": 1700000100},
        ],
    )

    rows = views.cycles(settings)["cycles"]
    assert rows[0]["added"] == 4 and rows[0]["removed"] == 1
    assert rows[1]["note"] == "1 import failure(s)"


def test_a_truncated_ledger_line_does_not_hide_the_rest(tmp_path: Path) -> None:
    """A SIGKILL mid-append leaves a partial final line; history still reads."""
    settings = _settings(tmp_path)
    path = common.sender_ledger_path(settings)
    path.write_text(
        json.dumps({"status": "ok", "at": 1700000000, "cycle_id": "c1", "added": 1})
        + "\n"
        + '{"status": "ok", "cycle'
    )
    assert views.cycles(settings)["matched"] == 1


# -- snapshots and diff -----------------------------------------------------


def _write_snapshot(settings: Settings, cycle_id: str, count: int, start: int = 0) -> None:
    lines = []
    for i in range(start, start + count):
        lines.append(
            json.dumps(
                {"repo": "repo-a", "path": f"f{i}.bin", "sha1": f"{i:040x}", "size": 10},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    (common.snapshots_dir(settings) / f"{cycle_id}.jsonl").write_text(
        "\n".join(lines) + "\n"
    )


def test_diff_reports_what_the_brake_would_do(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_delete_fraction=0.05)
    _write_snapshot(settings, "1700000000-a", 100)
    _write_snapshot(settings, "1700000100-b", 10)

    data = views.snapshot_diff(settings, "prev", "latest")
    assert data["removed"] == 90
    assert data["delete_fraction"] == 0.9
    assert data["would_trip_brake"] is True

    # Widening never trips it: additions are not braked.
    _write_snapshot(settings, "1700000200-c", 200)
    assert views.snapshot_diff(settings, "1700000100-b", "latest")["would_trip_brake"] is False


def test_snapshots_marks_the_baseline_the_cursor_points_at(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshot(settings, "1700000000-a", 3)
    _write_snapshot(settings, "1700000100-b", 4)
    common.cursor_path(settings).write_text(json.dumps({"last_cycle_id": "1700000000-a"}))

    rows = views.snapshots(settings)["snapshots"]
    assert [r["is_baseline"] for r in rows] == [True, False]


def test_a_missing_snapshot_names_where_it_looked(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(CLIError) as exc:
        views.snapshot_diff(settings, "nope", "latest")
    assert "nope" in str(exc.value)


# -- aql guard --------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def aql(self, query: str) -> list[dict]:
        self.queries.append(query)
        return [{"repo": "repo-a", "path": ".", "name": "f.bin"}]

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def test_a_projection_without_the_item_key_is_refused(tmp_path: Path, monkeypatch) -> None:
    """AQL collapses adjacent duplicate rows over the projected fields.

    It does so silently, with a healthy 200, so a diagnostic query that omits
    the key can under-report and read as proof of a problem that does not
    exist. Verified live: on a 140-file repository, a projection of
    (actual_sha1, size) returned 137 rows and the same query with the key
    returned 140.
    """
    settings = _settings(tmp_path)
    client = _FakeClient()
    monkeypatch.setattr(
        "artifactory_airlift.cli.views.ArtifactoryClient.from_settings",
        lambda _s: client,
    )

    with pytest.raises(CLIError) as exc:
        views.run_aql(settings, 'items.find({}).include("actual_sha1","size")')
    assert "path" in str(exc.value) and "name" in str(exc.value)
    assert client.queries == []

    forced = views.run_aql(
        settings, 'items.find({}).include("actual_sha1","size")', force=True
    )
    assert forced["warnings"]
    assert len(client.queries) == 1

    clean = views.run_aql(
        settings, 'items.find({}).include("repo","path","name","size")'
    )
    assert clean["warnings"] == []


def test_a_query_with_no_projection_is_fine(tmp_path: Path, monkeypatch) -> None:
    # Without include() AQL returns the default field set, which carries the
    # key, so there is nothing to guard against.
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        "artifactory_airlift.cli.views.ArtifactoryClient.from_settings",
        lambda _s: _FakeClient(),
    )
    assert views.run_aql(settings, 'items.find({"repo":"repo-a"})')["rows"] == 1


# -- blobs and archives -----------------------------------------------------


def test_blob_rejects_something_that_is_not_a_sha1(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(CLIError):
        views.blob(settings, "nonsense")


def test_archive_refs_resolve_by_write_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    older = settings.spool_dir / "1700000000-a.tar.zst"
    newer = common.done_dir(settings) / "1700000100-b.tar.zst"
    older.write_bytes(b"x")
    newer.write_bytes(b"y")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))

    assert views.find_archive(settings, "latest") == newer
    assert views.find_archive(settings, "prev") == older
    # A bare cycle id finds its archive wherever it currently sits.
    assert views.find_archive(settings, "1700000100-b") == newer


# -- rendering --------------------------------------------------------------


def test_numeric_columns_right_align() -> None:
    out = render.table(["repo", "size"], [["a", "1.2MiB"], ["bb", "10B"]])
    lines = out.splitlines()
    assert lines[0].split()[-1] == "SIZE"
    # Right-aligned means the values end at the same column.
    assert [len(line.rstrip()) for line in lines[1:]] == [len(lines[1]), len(lines[2])]


def test_mode_specific_commands_say_which_side_they_need(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="receiver")
    with pytest.raises(CLIError) as exc:
        common.require_mode(settings, "sender", "snapshots")
    assert "sender" in str(exc.value) and "receiver" in str(exc.value)


def test_parser_covers_every_documented_command() -> None:
    parser = app.build_parser()
    known = set(parser._subparsers._group_actions[0].choices)
    assert {
        "status", "config", "doctor", "repos", "cycles", "show",
        "snapshots", "diff", "archives", "archive", "blob", "aql",
    } <= known


# -- mutating commands ------------------------------------------------------


def test_time_windows_become_and_clauses(tmp_path: Path) -> None:
    """A range cannot be sibling keys, so it is built as an $and of two.

    Verified against Artifactory 7.146.10: both bounds constrain when written
    this way, and a JSON object cannot carry the same field twice, so the
    obvious {"modified": {"$gt": a}, "modified": {"$lt": b}} silently keeps
    only one of them.
    """
    clauses = actions._time_criteria(
        time_field="modified", since=1700000000, until=1700086400
    )
    assert clauses == [
        {"modified": {"$gt": "2023-11-14T22:13:20.000Z"}},
        {"modified": {"$lt": "2023-11-15T22:13:20.000Z"}},
    ]

    criteria = json.loads(aql._criteria(excluded_repos={"sys-repo"}, extra=clauses))
    # Exclusions and the window share the one "$and" list.
    assert criteria["$and"] == [{"repo": {"$ne": "sys-repo"}}] + clauses


def test_an_unknown_time_field_is_refused() -> None:
    with pytest.raises(CLIError):
        actions._time_criteria(time_field="downloaded", since=1, until=None)


def test_export_selection_from_a_snapshot_pair(tmp_path: Path) -> None:
    """Gap recovery: rebuild a lost cycle's payload from retained baselines.

    Reads snapshots only, never the source, so it still works after the
    source has moved on.
    """
    settings = _settings(tmp_path)
    _write_snapshot(settings, "1700000000-a", 3)
    _write_snapshot(settings, "1700000100-b", 5)

    entries, how = actions.select_entries(
        settings, None, from_snapshot="1700000000-a", to_snapshot="1700000100-b"
    )
    assert len(entries) == 2
    assert how["selector"] == "snapshot-delta"

    whole, how = actions.select_entries(settings, None, from_snapshot="1700000100-b")
    assert len(whole) == 5
    assert how["selector"] == "snapshot"


def test_export_without_a_selector_says_what_to_pass(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    class _Empty:
        def aql(self, query: str) -> list[dict]:
            return []

    with pytest.raises(CLIError) as exc:
        actions.select_entries(settings, _Empty())
    assert "--since" in str(exc.value)


def test_cursor_set_refuses_a_snapshot_that_is_not_retained(tmp_path: Path) -> None:
    """A cursor pointing at a missing snapshot is treated as a cold start.

    That is the opposite of a deliberate rewind, so it is refused rather than
    written.
    """
    settings = _settings(tmp_path)
    with pytest.raises(CLIError) as exc:
        actions.cursor_set(settings, "1700000000-gone")
    assert "no snapshot" in str(exc.value)

    _write_snapshot(settings, "1700000000-a", 7)
    data = actions.cursor_set(settings, "1700000000-a")
    assert data["now"] == "1700000000-a" and data["entries"] == 7
    assert common.read_cursor(settings)["last_cycle_id"] == "1700000000-a"


def test_cursor_clear_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    common.cursor_path(settings).write_text(json.dumps({"last_cycle_id": "c1"}))

    assert actions.cursor_clear(settings) == {
        "cleared": True,
        "was": "c1",
        "path": str(common.cursor_path(settings)),
    }
    assert actions.cursor_clear(settings)["cleared"] is False


def test_forget_drops_only_the_named_cycle(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="receiver")
    _write_ledger(
        common.receiver_ledger_path(settings),
        [
            {"cycle_id": "c1", "status": "ok"},
            {"cycle_id": "c2-c001", "parent_cycle_id": "c2", "status": "blob-staged"},
            {"cycle_id": "c2-c002", "parent_cycle_id": "c2", "status": "ok"},
        ],
    )

    data = actions.ledger_forget(settings, "c2")
    assert data["rows_dropped"] == 2
    remaining = common.read_ledger(common.receiver_ledger_path(settings))
    assert [r["cycle_id"] for r in remaining] == ["c1"]

    with pytest.raises(CLIError):
        actions.ledger_forget(settings, "c2")


def test_replay_requeues_the_archive_and_forgets_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="receiver")
    archive = common.done_dir(settings) / "c1.tar.zst"
    archive.write_bytes(b"x")
    _write_ledger(common.receiver_ledger_path(settings), [{"cycle_id": "c1", "status": "ok"}])

    data = actions.replay(settings, "c1")
    assert data["archives"] == ["c1.tar.zst"]
    assert (settings.spool_dir / "c1.tar.zst").is_file()
    assert not archive.exists()
    assert common.read_ledger(common.receiver_ledger_path(settings)) == []


def test_replay_refuses_when_the_archive_is_already_queued(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="receiver")
    (common.done_dir(settings) / "c1.tar.zst").write_bytes(b"x")
    (settings.spool_dir / "c1.tar.zst").write_bytes(b"x")

    with pytest.raises(CLIError) as exc:
        actions.replay(settings, "c1")
    assert "already" in str(exc.value)


def test_a_second_command_cannot_mutate_concurrently(tmp_path: Path) -> None:
    """The daemon holds its own lock for its whole life, so this one is only
    between CLI invocations. That is what it can honestly promise."""
    settings = _settings(tmp_path)
    with actions.cli_lock(settings):
        with pytest.raises(CLIError) as exc:
            with actions.cli_lock(settings):
                pass
    assert "another airlift command" in str(exc.value)

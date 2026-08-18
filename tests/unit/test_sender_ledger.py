"""The sender's cycle ledger.

The receiver has ``processed.jsonl``; the sender had only a cursor, which
records the last cycle that *succeeded* and nothing else. A cycle the brake
refused, a cycle skipped because the transport was behind, or blobs deferred
because the store did not hold them yet all lived in the log and nowhere
else, so history was unanswerable from state once the log had rotated.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from artifactory_airlift import sender
from artifactory_airlift.config import Settings

from ._store import fs_store


class _FakeClient:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def ping(self) -> bool:
        return True

    def list_repositories(self) -> list[dict]:
        return []

    def aql(self, query: str) -> list[dict]:
        # Both the enumeration and the per-repository metadata query are
        # answered from the same rows: they carry every field either one
        # projects, so metadata resolves and the cycle ships real entries.
        return self._rows


def _settings(tmp_path: Path, **overrides) -> Settings:
    state = tmp_path / "state"
    spool = tmp_path / "spool"
    for p in (state, spool):
        p.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        mode="sender",
        instance_name="art-a",
        state_dir=state,
        spool_dir=spool,
        filestore_root=tmp_path / "filestore",
        spool_min_free_bytes=0,
        excluded_package_types=[],
        artifactory_uid=os.getuid(),
        artifactory_gid=os.getgid(),
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _rows(n: int) -> list[dict]:
    return [
        {
            "repo": "airlift-npm-local",
            "path": ".",
            "name": f"a{i:04d}.bin",
            "actual_sha1": f"{i:040x}",
            "size": 1,
        }
        for i in range(n)
    ]


def _seed_blobs(settings: Settings, rows: list[dict]) -> None:
    """Put a blob in the store for every row, so nothing defers."""
    store = fs_store(settings.filestore_root)
    payload = settings.state_dir / "blob-src"
    payload.write_bytes(b"x")
    for row in rows:
        store.write(payload, row["actual_sha1"])


def _clear_spool(settings: Settings) -> None:
    """Drain the spool the way the transport would, ungating the next cycle."""
    for archive in settings.spool_dir.glob("*.tar.zst"):
        archive.unlink()


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run(settings: Settings, client, ledger: Path) -> None:
    snaps = settings.state_dir / "snapshots"
    exports = settings.state_dir / "exports"
    for p in (snaps, exports):
        p.mkdir(parents=True, exist_ok=True)
    sender._cycle(
        settings,
        client=client,
        store=fs_store(settings.filestore_root),
        snapshots_dir=snaps,
        exports_dir=exports,
        cursor_path=settings.state_dir / "cursor.json",
        ledger_path=ledger,
    )


def test_pending_archives_are_recorded_as_a_skipped_cycle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.spool_dir / "1700000000-abcdef12.tar.zst").write_bytes(b"x")
    ledger = settings.state_dir / "cycles.jsonl"

    _run(settings, _FakeClient(_rows(3)), ledger)

    rows = _read(ledger)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped-pending"
    assert "1 archive(s) still in spool" in rows[0]["note"]


def test_a_refused_cycle_records_why(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_delete_fraction=0.05)
    ledger = settings.state_dir / "cycles.jsonl"

    # Cold start establishes the baseline, then the source loses most of it.
    _seed_blobs(settings, _rows(100))
    _run(settings, _FakeClient(_rows(100)), ledger)
    _clear_spool(settings)
    _run(settings, _FakeClient(_rows(10)), ledger)

    rows = _read(ledger)
    assert [r["status"] for r in rows] == ["ok", "brake-refused"]
    refused = rows[-1]
    assert refused["removed"] == 90
    assert "max_delete_fraction" in refused["note"]
    # A refusal must not look like progress: the cursor still points at the
    # baseline the refused diff was measured against.
    cursor = json.loads((settings.state_dir / "cursor.json").read_text())
    assert cursor["last_cycle_id"] == rows[0]["cycle_id"]


def test_an_unchanged_source_records_a_quiet_cycle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ledger = settings.state_dir / "cycles.jsonl"
    client = _FakeClient(_rows(4))
    _seed_blobs(settings, _rows(4))

    _run(settings, client, ledger)
    _clear_spool(settings)
    _run(settings, client, ledger)

    rows = _read(ledger)
    assert rows[0]["status"] == "ok"
    assert rows[0]["added"] == 4
    assert rows[1]["status"] == "no-changes"
    # Nothing happened, so nothing about the change is recorded.
    assert "added" not in rows[1]


def test_ledger_is_optional_so_the_cycle_never_depends_on_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _run(settings, _FakeClient(_rows(2)), tmp_path / "state" / "cycles.jsonl")
    assert (settings.state_dir / "cycles.jsonl").exists()

    # No ledger path at all is the pre-0.17 call shape and must still work.
    _clear_spool(settings)
    snaps = settings.state_dir / "snapshots"
    sender._cycle(
        settings,
        client=_FakeClient(_rows(3)),
        store=fs_store(settings.filestore_root),
        snapshots_dir=snaps,
        exports_dir=settings.state_dir / "exports",
        cursor_path=settings.state_dir / "cursor.json",
    )
    assert len(list(settings.spool_dir.glob("*.tar.zst"))) == 1


def test_trim_keeps_the_newest_rows(tmp_path: Path) -> None:
    ledger = tmp_path / "cycles.jsonl"
    total = sender._LEDGER_KEEP_ROWS + sender._LEDGER_TRIM_SLACK + 5
    ledger.write_text(
        "".join(json.dumps({"status": "ok", "n": i}) + "\n" for i in range(total))
    )

    sender._trim_ledger(ledger)

    rows = _read(ledger)
    assert len(rows) == sender._LEDGER_KEEP_ROWS
    assert rows[-1]["n"] == total - 1
    assert rows[0]["n"] == total - sender._LEDGER_KEEP_ROWS


def test_trim_leaves_a_file_within_the_slack_alone(tmp_path: Path) -> None:
    ledger = tmp_path / "cycles.jsonl"
    rows = sender._LEDGER_KEEP_ROWS + 10
    ledger.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(rows)))

    sender._trim_ledger(ledger)

    assert len(_read(ledger)) == rows


def test_error_text_is_flattened_into_one_line(tmp_path: Path) -> None:
    """httpx renders a failed status over two lines, with a URL on the second.

    A ledger row is one line per cycle and gets read as a table, so the text
    is collapsed on the way in. The full message was already logged.
    """
    ledger = tmp_path / "cycles.jsonl"
    sender._record_cycle(
        ledger,
        status="failed",
        note="Server error '503 ' for url 'http://localhost:8081/x'\nFor more "
        "information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503",
    )

    row = _read(ledger)[0]
    assert "\n" not in row["note"]
    assert row["note"].startswith("Server error '503 ' for url")


def test_a_very_long_message_is_truncated(tmp_path: Path) -> None:
    ledger = tmp_path / "cycles.jsonl"
    sender._record_cycle(ledger, status="failed", note="x" * 5000)

    note = _read(ledger)[0]["note"]
    assert len(note) == sender._LEDGER_FIELD_CHARS
    assert note.endswith("…")

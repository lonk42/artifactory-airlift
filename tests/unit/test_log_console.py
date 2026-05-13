from __future__ import annotations

from artifactory_airlift import log


def _render(event: str, level: str = "info", **fields) -> str:
    """Drive the console renderer directly with a fake event dict."""
    event_dict = {
        "timestamp": "2026-05-13T01:30:12.024369Z",
        "level": level,
        "event": event,
        **fields,
    }
    return log._console_renderer(None, level, event_dict)


def test_known_event_renders_with_template():
    line = _render(
        "sender.diff_computed",
        cycle_id="1778636042-363b0382",
        prev_cycle_id="1778635981-12a915b4",
        added=5,
        removed=2,
    )
    assert line.startswith("2026-05-13 01:30:12 INFO  sender   cycle=1778636042 ")
    assert "INFO  sender  " in line
    assert "5 added, 2 removed" in line
    assert "prev=1778635981-12a915b4" in line
    # Grammar / capitalisation: messages start with a capital letter.
    assert " Diff vs prev=" in line


def test_receiver_cycle_done_has_tail_for_extras():
    line = _render(
        "receiver.cycle_done",
        cycle_id="1778636042-363b0382",
        status="partial",
        moved_to="/var/airlift/spool/.done/1778636042-363b0382.tar.zst",
    )
    assert "status=partial" in line
    # moved_to is not in the template so it lands in the tail.
    assert "moved_to=/var/airlift/spool/.done/1778636042-363b0382.tar.zst" in line


def test_blobs_written_template():
    line = _render(
        "receiver.blobs_written",
        cycle_id="1778636042-363b0382",
        written=4,
        skipped=0,
    )
    assert "Blobs: 4 written, 0 skipped" in line
    assert "(already in filestore)" in line
    assert "receiver" in line


def test_no_cycle_id_omits_tag():
    line = _render("startup", mode="sender", instance="a", cycle_seconds=30)
    assert "cycle=" not in line
    assert "Mode=sender" in line
    assert "Starting up" in line


def test_unknown_event_falls_back_to_name_plus_tail():
    line = _render("never.heard.of.this", repo="r", count=12)
    assert "never.heard.of.this" in line
    # All non-reserved fields tail-rendered.
    assert "repo=r" in line
    assert "count=12" in line


def test_values_with_spaces_are_quoted():
    line = _render("never.heard.of.this", note="has a space")
    assert 'note="has a space"' in line


def test_archive_finalized_includes_size_and_counts():
    line = _render(
        "sender.archive_finalized",
        cycle_id="1778636042-363b0382",
        path="/var/airlift/spool/1778636042.tar.zst",
        size_bytes=1234567,
        size_human="1.2MiB",
        blob_count=42,
        repo_count=3,
        repos=["a", "b", "c"],
    )
    assert "Archive ready" in line
    assert "1.2MiB" in line
    assert "42 blob(s)" in line
    assert "3 repo(s)" in line


def test_per_repo_changes_summary_in_template():
    summary = "airlift-rpm-local=+3, airlift-npm-local=-1"
    line = _render(
        "sender.per_repo_changes",
        cycle_id="1778636042-363b0382",
        added={"airlift-rpm-local": 3},
        removed={"airlift-npm-local": 1},
        summary=summary,
    )
    assert summary in line
    assert "Per-repo changes:" in line
    # The structured added/removed dicts are kept for JSON consumers but
    # suppressed from the console tail because the human summary already
    # carries that data.
    assert "added=" not in line
    assert "removed=" not in line


def test_import_failure_per_line_render():
    line = _render(
        "receiver.import_failure",
        level="warning",
        cycle_id="1778636042-363b0382",
        index=2,
        total=7,
        code="500",
        detail="No directory for repository jfrog-usage-logs",
    )
    assert "Import failure 2/7" in line
    assert "HTTP 500" in line
    assert "No directory for repository jfrog-usage-logs" in line
    assert "WARN " in line


def test_human_bytes_formats():
    assert log.human_bytes(0) == "0B"
    assert log.human_bytes(900) == "900B"
    assert log.human_bytes(1024) == "1.0KiB"
    assert log.human_bytes(1024 * 1024 + 100) == "1.0MiB"
    assert log.human_bytes(5 * 1024 * 1024 * 1024) == "5.0GiB"


def test_fmt_counter_dict_sorts_by_count_desc():
    out = log._fmt_counter_dict({"alpha": 1, "beta": 5, "gamma": 5})
    # Tied counts fall back to alphabetical, so beta before gamma; both ahead of alpha.
    assert out == "beta=5, gamma=5, alpha=1"


def test_dict_value_in_tail_is_rendered_as_counter():
    line = _render("never.heard.of.this", per_repo={"r1": 3, "r2": 5})
    # The "r2=5, r1=3" rendering means we tripped the counter dict path.
    assert "per_repo=r2=5, r1=3" in line


def test_format_keys_extracts_template_field_roots():
    assert log._format_keys("a {x} b {y}") == {"x", "y"}
    assert log._format_keys("{foo.bar} {baz[0]}") == {"foo", "baz"}
    assert log._format_keys("no fields here") == set()

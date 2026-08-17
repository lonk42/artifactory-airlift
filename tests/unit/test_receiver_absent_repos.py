"""Per-repo import notices for repositories a cycle did not ship.

``/api/import/repositories`` walks every repository the destination holds and
reports a failure line for each one with no directory in the tree. Under the
old full-export tree that was rare; under an O(delta) synthesised tree it is
every untouched repository, on every cycle. Those lines have to be dropped or
``status=partial`` stops meaning anything.

Observed verbatim on the live pair, which is where these strings come from.
"""
from __future__ import annotations

from artifactory_airlift.receiver import _is_absent_repo_notice

_MISSING_DIR = (
    "500 : No directory for repository {key} found at "
    "/var/airlift/state/import/1786964365-011fdc57/metadata/repositories"
)
_NO_MATCH = "500 : The directory {key} does not match any repository key."


def test_absent_repo_is_dropped_when_the_cycle_did_not_ship_it() -> None:
    assert _is_absent_repo_notice(
        _MISSING_DIR.format(key="airlift-npm-local"), {"example-repo-local"}
    )


def test_the_other_wording_is_dropped_too() -> None:
    assert _is_absent_repo_notice(
        _NO_MATCH.format(key="jfrog-usage-logs"), {"example-repo-local"}
    )


def test_a_shipped_repo_still_counts_as_a_failure() -> None:
    """A missing directory for a repo whose directory IS in the tree means
    the archive or the extraction is wrong, which is what status=partial is
    for."""
    assert not _is_absent_repo_notice(
        _MISSING_DIR.format(key="example-repo-local"), {"example-repo-local"}
    )


def test_a_removals_only_cycle_ships_no_directory_for_its_repo() -> None:
    """A deletion carries no metadata, so the repo it names has no directory
    in the tree and Artifactory reports it as missing. Observed on the live
    pair: this was the last WARN left after the first fix."""
    assert _is_absent_repo_notice(_MISSING_DIR.format(key="example-repo-local"), set())


def test_an_unrelated_failure_is_never_dropped() -> None:
    line = (
        "500 : Import error: from: /var/airlift/state/import/x/metadata/"
        "repositories/airlift-helm-local/index.yaml to airlift-helm-local:"
        "index.yaml reason: Could not import file"
    )
    assert not _is_absent_repo_notice(line, {"airlift-helm-local"})
    assert not _is_absent_repo_notice(line, set())


def test_an_empty_shipped_set_still_drops_absent_repos() -> None:
    """A removals-only cycle ships no repos at all, so every repo on the
    destination reports one of these."""
    assert _is_absent_repo_notice(_MISSING_DIR.format(key="anything"), set())

"""Artifact enumeration via AQL, replacing the system-export walk.

Why this exists
---------------
The sender used to enumerate by asking Artifactory for a full system export
(``POST /api/export/system``) and walking the resulting tree. That writes one
directory and two small XML files per artifact, so it costs time proportional
to the size of the whole instance, every cycle, regardless of how little
changed. Measured on a real instance it runs at about 2.5 ms per node: roughly
three minutes for 78,000 artifacts, and linear beyond that.

AQL returns the same four fields the walk extracted (repo, path, sha1, size)
from the database instead, at about 12 microseconds per node plus a fixed
~180 ms. Same data, two orders of magnitude cheaper, and it does not grow into
the cycle interval.

The dedup trap
--------------
AQL collapses **adjacent** duplicate rows over the projected field set, in
whatever order the result set arrives. It is ``uniq`` semantics, not
``SELECT DISTINCT``. Measured against a repository holding 140 files:

    .include("actual_sha1", "size")                ->  43 rows
    .include("name", "actual_sha1", "size")        ->  53 rows
    .include("repo", "path", "name", ...)          -> 140 rows

and on a whole instance holding 347 files across 9 repositories:

    .include("repo")                               -> 252 rows
    .include("repo").sort({"$asc": ["repo"]})      ->   9 rows

There is no error and no warning; the response is a healthy 200 with
well-formed JSON. A short enumeration would be read as mass deletion and
mirrored to the destination, so this is a correctness hazard, not a
performance one.

The defence is structural rather than advisory. ``_ITEM_KEY`` (repo, path,
name) is a natural key for an item, so a row can never equal its neighbour
once those three are projected, and ``_include()`` always emits them.
Callers may only *add* fields; there is deliberately no way to ask for a
projection that omits the key.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from . import log
from .export_unpacker import ArtifactEntry

if TYPE_CHECKING:
    from .artifactory_client import ArtifactoryClient

logger = log.get("artifactory.aql")

# Natural key for an item. Always projected, never optional.
_ITEM_KEY = ("repo", "path", "name")

# Fields the snapshot needs on top of the key.
_SNAPSHOT_FIELDS = ("actual_sha1", "size")

# Everything artifactory-file.xml carries, for metadata synthesis.
_METADATA_FIELDS = (
    "size",
    "created",
    "modified",
    "updated",
    "created_by",
    "modified_by",
    "actual_sha1",
    "actual_md5",
    "sha256",
    # Absent unless the deploying client declared a checksum. Projected so
    # the synthesised XML can omit <original> exactly where a real export
    # does; see metadata_synth._checksums_xml.
    "original_sha1",
    "original_md5",
)


def _include(*extra: str) -> str:
    """Render an ``.include(...)`` clause that always carries the item key.

    Deduplicating the field list preserves caller order while keeping the
    key first, so the emitted query is stable and easy to eyeball in a log.
    """
    fields: list[str] = list(_ITEM_KEY)
    for f in extra:
        if f not in fields:
            fields.append(f)
    rendered = ",".join(f'"{f}"' for f in fields)
    return f".include({rendered})"


def _criteria(
    *,
    included_repos: set[str] | None = None,
    excluded_repos: set[str] | None = None,
) -> str:
    """Build the ``items.find`` criteria object.

    Both filters are pushed into the query, so the database does the work and
    a filtered sync genuinely reads less. This is strictly more than the
    export path could do: ``/api/export/system`` has no repository selector
    at all (every body field we tried was accepted and ignored), so it always
    walked the whole instance.

    **AQL has no set operators.** Neither ``$in`` nor ``$nin`` exists; both
    are rejected outright with a parse error naming the operator, which is at
    least a loud failure rather than a quiet one. A set membership test is
    written as an explicit ``$or`` of ``$eq``, and its negation as an ``$and``
    of ``$ne``.

    Both also *have* to be written that way even for the two-element case,
    because AQL takes a criteria object and a JSON object cannot carry the
    same key twice: ``{"repo": {"$ne": "a"}, "repo": {"$ne": "b"}}`` collapses
    to whichever clause survives the parse. A single top-level ``$ne`` does
    work, but special-casing one repository buys nothing.

    Why this is worth pushing to the server rather than filtering rows we
    already hold: the excluded repositories are not small. ``jfrog-usage-logs``
    alone was 167 of 442 rows on the dev instance and grows by a record per
    cycle; on a production instance ``artifactory-build-info`` holds one JSON
    per build and can dwarf the real content.
    """
    crit: dict[str, Any] = {"type": "file"}
    if included_repos:
        crit["$or"] = [{"repo": {"$eq": r}} for r in sorted(included_repos)]
    if excluded_repos:
        crit["$and"] = [{"repo": {"$ne": r}} for r in sorted(excluded_repos)]
    return json.dumps(crit, sort_keys=True, separators=(",", ":"))


def _repo_path(row: dict[str, Any]) -> str:
    """Join AQL's split path back into airlift's single repo-relative path.

    AQL reports a root-level artifact as path ``"."``; everything else is a
    directory prefix without the filename.
    """
    path = row.get("path") or "."
    name = row.get("name") or ""
    return name if path == "." else f"{path}/{name}"


def iter_artifacts(
    client: "ArtifactoryClient",
    *,
    excluded_repos: set[str] | None = None,
    included_repos: set[str] | None = None,
) -> Iterator[ArtifactEntry]:
    """Yield one entry per artifact on the source.

    Mirrors ``export_unpacker.iter_artifacts`` so the sender can swap one for
    the other. Both filters are applied by the query (see ``_criteria``); the
    equivalent checks here are a safety net, not the mechanism.

    Keeping them is deliberate. A criteria clause that the server declines to
    apply is not an error: the response is a healthy 200 with more rows in it
    than were asked for, which is the same failure shape as the dedup trap
    this module exists to defend against. So a row that reaches these gates
    means the query did not do what it was told, and that is worth saying out
    loud rather than silently mirroring a repository the operator excluded.
    The gates keep their original order, allowlist then denylist, so listing
    a system repo in the allowlist still does not force it through.

    Rows without a sha1 are skipped with a warning rather than aborting the
    cycle. An artifact whose checksum Artifactory has not computed yet cannot
    be shipped (the receiver links blobs by sha1), but it is not a reason to
    stall the whole mirror; the next cycle picks it up.
    """
    excluded = excluded_repos or set()
    included = included_repos or set()
    query = _criteria(included_repos=included_repos, excluded_repos=excluded_repos)
    rows = client.aql(f"items.find({query}){_include(*_SNAPSHOT_FIELDS)}")

    logger.info("aql.enumerated", rows=len(rows))

    missing_sha1 = 0
    unfiltered: Counter[str] = Counter()
    for row in rows:
        repo_key = row.get("repo") or ""
        if included and repo_key not in included:
            unfiltered[repo_key] += 1
            continue
        if repo_key in excluded:
            unfiltered[repo_key] += 1
            continue
        sha1 = row.get("actual_sha1")
        if not sha1:
            missing_sha1 += 1
            continue
        yield ArtifactEntry(
            repo_key=repo_key,
            repo_path=_repo_path(row),
            sha1=str(sha1).lower(),
            size=int(row.get("size") or 0),
        )
    if missing_sha1:
        logger.warning("aql.rows_without_sha1", count=missing_sha1)
    if unfiltered:
        # Not a correctness failure (the rows were dropped) but it means the
        # repository filter is running client-side after all, so the cycle is
        # paying to fetch rows it discards.
        logger.warning(
            "aql.filter_not_applied",
            count=sum(unfiltered.values()),
            repos=sorted(unfiltered),
            summary=log._fmt_counter_dict(unfiltered),
        )


def write_snapshot(
    client: "ArtifactoryClient",
    snapshot_path: Path,
    *,
    excluded_repos: set[str] | None = None,
    included_repos: set[str] | None = None,
) -> int:
    """Write a sorted JSONL snapshot from AQL. Returns the entry count.

    Byte-for-byte the same format as the export-derived snapshot, so
    baselines written by either path stay comparable across an upgrade.
    """
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    entries = list(
        iter_artifacts(
            client,
            excluded_repos=excluded_repos,
            included_repos=included_repos,
        )
    )
    entries.sort(key=lambda e: e.sha1)
    tmp = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(e.to_json() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, snapshot_path)
    return len(entries)


def fetch_metadata(
    client: "ArtifactoryClient",
    entries: Iterable[ArtifactEntry],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Fetch full per-artifact metadata for ``entries``, keyed by (repo, path).

    Queried per repository rather than per artifact: one request for a
    repository's whole changed set is far cheaper than one request each, and
    the delta is small enough that returning the repository's full row set
    and filtering locally would waste bandwidth on a large repository.

    Properties are requested alongside, because a Docker repository will not
    function on the destination without its ``sha256`` property.
    """
    wanted: dict[str, set[str]] = {}
    for e in entries:
        wanted.setdefault(e.repo_key, set()).add(e.repo_path)
    if not wanted:
        return {}

    out: dict[tuple[str, str], dict[str, Any]] = {}
    fields = _include(*_METADATA_FIELDS, "property.key", "property.value")
    for repo_key, paths in sorted(wanted.items()):
        clauses = []
        for rp in sorted(paths):
            head, _, tail = rp.rpartition("/")
            clauses.append({"path": head or ".", "name": tail})
        crit = json.dumps(
            {"repo": repo_key, "type": "file", "$or": clauses},
            sort_keys=True,
            separators=(",", ":"),
        )
        rows = client.aql(f"items.find({crit}){fields}")
        for row in rows:
            out[(row.get("repo") or "", _repo_path(row))] = row
        logger.debug(
            "aql.metadata_fetched", repo=repo_key, wanted=len(paths), got=len(rows)
        )
    return out

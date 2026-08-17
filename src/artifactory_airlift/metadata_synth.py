"""Write the per-artifact metadata tree that Artifactory used to write for us.

The receiver registers artifacts by handing Artifactory a directory tree and
calling ``POST /api/import/repositories``. That tree used to come from a full
system export on the source, which is what made every cycle cost time
proportional to the whole instance.

Nothing about the import requires Artifactory to have produced the tree. It is
a directory of small XML files in a documented shape, and Artifactory accepts
one we wrote ourselves. Verified end to end against a live pair: an artifact
was deleted from the destination, a tree synthesised here from AQL rows alone
was imported, and the artifact came back with matching bytes, matching sha1,
all five of its properties, and its original creation timestamp rather than
the import time.

So the sender synthesises this tree for the changed artifacts only, and the
export disappears from the cycle entirely.

Layout produced (mirroring a real export, minus everything unused)::

    repositories/<repoKey>/<path...>/<file>.artifactory-metadata/
        artifactory-file.xml
        properties.xml          (only when the artifact has properties)

Only ``repositories/`` is emitted. A real export also writes ``etc/``,
``artifactory.config.xml`` and ``licenses/``; those exist for
``/api/import/system``, which airlift does not use.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape

from . import log
from .export_unpacker import ArtifactEntry

logger = log.get("artifactory.metadata_synth")

_METADATA_DIR_SUFFIX = ".artifactory-metadata"

# Artifactory renders property keys as XML element names. Anything that is not
# a legal name would produce a tree it then refuses to parse, so such keys are
# dropped rather than risking the whole repository's import.
_XML_NAME_RE = re.compile(r"^[A-Za-z_][\w.\-]*$")

# AQL timestamps are ISO-8601 with milliseconds and a trailing Z; the XML wants
# epoch milliseconds.
_ISO = "%Y-%m-%dT%H:%M:%S.%f%z"


def _epoch_ms(value: str | None) -> int:
    """Convert an AQL ISO-8601 timestamp to epoch milliseconds.

    Returns 0 when absent or unparseable. Artifactory treats 0 as "unknown"
    and substitutes the import time, which is a worse mirror but not a
    broken one, so this never raises.
    """
    if not value:
        return 0
    try:
        # Python's %z wants +0000 rather than a bare Z.
        dt = _dt.datetime.strptime(value.replace("Z", "+0000"), _ISO)
        return int(dt.timestamp() * 1000)
    except ValueError:
        logger.debug("synth.bad_timestamp", value=value)
        return 0


def _checksums_xml(row: dict[str, Any]) -> str:
    """Render the checksumsInfo block.

    ``<original>`` is what a client declared on deploy, and it is not always
    present. Comparing a real export against this output on a live pair:
    sha256 always carries an ``<original>`` equal to ``<actual>``, while sha1
    and md5 carry one only when the deploying client actually declared them
    (AQL then reports ``original_sha1`` / ``original_md5``, and omits the
    field otherwise).

    Emitting one unconditionally is importable but not faithful: it surfaces
    on the destination as an ``originalChecksums`` block the source does not
    have, which is a visible divergence in a mirror.
    """
    triples = (
        ("sha1", row.get("actual_sha1"), row.get("original_sha1")),
        ("md5", row.get("actual_md5"), row.get("original_md5")),
        # sha256 has no original_* counterpart in AQL; Artifactory records
        # the actual value as the original for it, so mirror that.
        ("sha256", row.get("sha256"), row.get("sha256")),
    )
    out = []
    for kind, actual, original in triples:
        if not actual:
            continue
        parts = [f"<checksum><type>{kind}</type>"]
        if original:
            parts.append(f"<original>{escape(str(original))}</original>")
        parts.append(f"<actual>{escape(str(actual))}</actual></checksum>")
        out.append("".join(parts))
    return "".join(out)


def file_xml(row: dict[str, Any], repo_path: str) -> str:
    """Render ``artifactory-file.xml`` for one artifact.

    ``repoPath/path`` carries the full repo-relative path *including* the
    filename, which is how a real export writes it.
    """
    return (
        "<artifactory-file>"
        "<repoPath>"
        f"<repoKey>{escape(str(row.get('repo', '')))}</repoKey>"
        f"<path>{escape(repo_path)}</path>"
        "<folder>false</folder>"
        "</repoPath>"
        f"<name>{escape(str(row.get('name', '')))}</name>"
        f"<created>{_epoch_ms(row.get('created'))}</created>"
        f"<lastModified>{_epoch_ms(row.get('modified'))}</lastModified>"
        f"<size>{int(row.get('size') or 0)}</size>"
        "<additionalInfo>"
        f"<createdBy>{escape(str(row.get('created_by') or ''))}</createdBy>"
        f"<lastUpdated>{_epoch_ms(row.get('updated'))}</lastUpdated>"
        f"<modifiedBy>{escape(str(row.get('modified_by') or ''))}</modifiedBy>"
        f"<checksumsInfo><checksums>{_checksums_xml(row)}</checksums></checksumsInfo>"
        "</additionalInfo>"
        "</artifactory-file>"
    )


def properties_xml(row: dict[str, Any]) -> str | None:
    """Render ``properties.xml``, or None when the artifact has no properties.

    Each property becomes an element named after its key. AQL returns one
    row per key/value pair, so a multi-valued property arrives as repeated
    keys and is emitted as repeated elements, which is how the export
    writes it too.
    """
    props = row.get("properties") or []
    parts = []
    for p in props:
        key = str(p.get("key", ""))
        if not _XML_NAME_RE.match(key):
            logger.warning(
                "synth.property_key_skipped", key=key, repo=row.get("repo")
            )
            continue
        parts.append(f"<{key}>{escape(str(p.get('value', '')))}</{key}>")
    if not parts:
        return None
    return "<properties>" + "".join(parts) + "</properties>"


def build_tree(
    dest: Path,
    entries: Iterable[ArtifactEntry],
    metadata: dict[tuple[str, str], dict[str, Any]],
) -> tuple[int, list[ArtifactEntry]]:
    """Write ``dest/repositories/...`` for ``entries``.

    ``metadata`` is keyed by ``(repo_key, repo_path)`` as returned by
    ``aql.fetch_metadata``. Returns ``(written, unresolved)``; an entry whose
    metadata is missing is reported rather than guessed at, because a
    synthesised record with no checksum would ask the receiver to import an
    artifact it cannot link to a blob.
    """
    repos_root = dest / "repositories"
    repos_root.mkdir(parents=True, exist_ok=True)

    written = 0
    unresolved: list[ArtifactEntry] = []
    for entry in entries:
        row = metadata.get((entry.repo_key, entry.repo_path))
        if row is None:
            unresolved.append(entry)
            continue
        meta_dir = repos_root / entry.repo_key / (entry.repo_path + _METADATA_DIR_SUFFIX)
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "artifactory-file.xml").write_text(
            file_xml(row, entry.repo_path), encoding="utf-8"
        )
        px = properties_xml(row)
        if px is not None:
            (meta_dir / "properties.xml").write_text(px, encoding="utf-8")
        written += 1

    logger.info(
        "synth.tree_written",
        path=str(dest),
        written=written,
        unresolved=len(unresolved),
    )
    return written, unresolved

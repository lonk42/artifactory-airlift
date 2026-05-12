from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from . import log

logger = log.get("artifactory.export_unpacker")

# Per-artifact metadata in a system export lives at:
#   repositories/<repoKey>/<path...>/<filename>.artifactory-metadata/properties.xml
# and the artifact itself (zero-byte under excludeContent=true) is alongside the
# .artifactory-metadata sibling directory.
_METADATA_DIR_SUFFIX = ".artifactory-metadata"
_FILEINFO_RE = re.compile(r"<sha1>([0-9a-f]+)</sha1>", re.IGNORECASE)
_SIZE_RE = re.compile(r"<size>(\d+)</size>")


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    repo_key: str
    repo_path: str
    sha1: str
    size: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "repo": self.repo_key,
                "path": self.repo_path,
                "sha1": self.sha1,
                "size": self.size,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> "ArtifactEntry":
        d = json.loads(line)
        return cls(
            repo_key=d["repo"],
            repo_path=d["path"],
            sha1=d["sha1"],
            size=int(d["size"]),
        )


def _parse_fileinfo(xml_bytes: bytes) -> tuple[str | None, int | None]:
    """Pull sha1 + size out of an Artifactory artifact metadata XML.

    The system-export shape Artifactory writes today (7.x) is
    ``artifactory-file.xml``:

        <artifactory-file>
          <size>NNN</size>
          <additionalInfo>
            <checksumsInfo>
              <checksums>
                <checksum>
                  <type>sha1</type>
                  <actual>HEX</actual>
                </checksum>
                ...
              </checksums>
            </checksumsInfo>
          </additionalInfo>
        </artifactory-file>

    Older shapes that embedded ``<sha1>HEX</sha1>`` directly are still
    handled via a regex fallback. Returns ``(sha1, size)``; either may
    be None if absent.
    """
    sha1: str | None = None
    size: int | None = None
    try:
        root = ET.fromstring(xml_bytes)
        # Walk every <checksum><type>sha1</type><actual>...</actual></checksum>.
        for checksum in root.iter():
            tag = checksum.tag.split("}")[-1].lower()
            if tag != "checksum":
                continue
            ctype = None
            cactual = None
            for child in checksum:
                ctag = child.tag.split("}")[-1].lower()
                if ctag == "type" and child.text:
                    ctype = child.text.strip().lower()
                elif ctag == "actual" and child.text:
                    cactual = child.text.strip().lower()
            if ctype == "sha1" and cactual:
                sha1 = cactual
                break
        # Direct <size> if present.
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag == "size" and elem.text:
                try:
                    size = int(elem.text.strip())
                    break
                except ValueError:
                    pass
        # Legacy shape with bare <sha1> tag.
        if sha1 is None:
            for elem in root.iter():
                tag = elem.tag.split("}")[-1].lower()
                if tag == "sha1" and elem.text:
                    sha1 = elem.text.strip().lower()
                    break
    except ET.ParseError:
        pass
    if sha1 is None:
        m = _FILEINFO_RE.search(xml_bytes.decode("utf-8", errors="ignore"))
        if m:
            sha1 = m.group(1).lower()
    if size is None:
        m = _SIZE_RE.search(xml_bytes.decode("utf-8", errors="ignore"))
        if m:
            size = int(m.group(1))
    return sha1, size


def iter_artifacts(export_root: Path) -> Iterator[ArtifactEntry]:
    """Walk a system-export directory tree and yield one entry per artifact.

    Looks for `<artifact>.artifactory-metadata/` directories and reads the
    sibling `fileinfo` XML (or `properties.xml` which also embeds the
    checksum on some Artifactory versions).
    """
    repos_root = export_root / "repositories"
    if not repos_root.is_dir():
        logger.warning("export.no_repositories_dir", path=str(repos_root))
        return

    for repo_dir in sorted(p for p in repos_root.iterdir() if p.is_dir()):
        repo_key = repo_dir.name
        for dirpath, dirnames, _filenames in os.walk(repo_dir):
            for d in dirnames:
                if not d.endswith(_METADATA_DIR_SUFFIX):
                    continue
                meta_dir = Path(dirpath) / d
                artifact_name = d[: -len(_METADATA_DIR_SUFFIX)]
                # repo_path = relative path under the repo, including filename
                rel = Path(dirpath).relative_to(repo_dir)
                repo_path = (
                    artifact_name if str(rel) == "." else f"{rel.as_posix()}/{artifact_name}"
                )
                sha1, size = _read_meta(meta_dir)
                if sha1 is None:
                    continue
                yield ArtifactEntry(
                    repo_key=repo_key,
                    repo_path=repo_path,
                    sha1=sha1,
                    size=size or 0,
                )


def _read_meta(meta_dir: Path) -> tuple[str | None, int | None]:
    """Read the sha1/size out of one .artifactory-metadata directory.

    The interesting files describe individual artifacts and look like
    ``artifactory-file.xml`` (containing a sha1). Folder descriptors
    (``artifactory-folder.xml``) carry no sha1 and are skipped.
    """
    file_candidates = ["artifactory-file.xml", "fileinfo", "fileinfo.xml", "properties.xml"]
    for name in file_candidates:
        f = meta_dir / name
        if f.is_file():
            sha1, size = _parse_fileinfo(f.read_bytes())
            if sha1 is not None:
                return sha1, size
    # Fallback: scan any xml in the dir, skipping known folder descriptors.
    for f in meta_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.name in ("artifactory-folder.xml",):
            continue
        if f.suffix not in ("", ".xml"):
            continue
        sha1, size = _parse_fileinfo(f.read_bytes())
        if sha1 is not None:
            return sha1, size
    return None, None


def write_snapshot(export_root: Path, snapshot_path: Path) -> int:
    """Write a sorted JSONL snapshot for the given export. Returns count."""
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    entries = list(iter_artifacts(export_root))
    entries.sort(key=lambda e: e.sha1)
    tmp = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(e.to_json() + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, snapshot_path)
    return len(entries)

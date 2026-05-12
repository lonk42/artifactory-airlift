from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .export_unpacker import ArtifactEntry

_Key = tuple[str, str, str]  # (sha1, repo_key, repo_path)


def _iter_entries(path: Path) -> Iterator[ArtifactEntry]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield ArtifactEntry.from_json(line)


def _iter_sorted_keys(path: Path) -> Iterator[tuple[_Key, ArtifactEntry]]:
    """Yield ((sha1, repo, path), entry) in fully sorted order.

    The snapshot file is sha1-sorted but ties within a sha1 are not
    guaranteed to be ordered by (repo, path). Buffer one sha1 group at
    a time and sort within it for a deterministic merge.
    """
    group: list[ArtifactEntry] = []
    current_sha1: str | None = None
    for entry in _iter_entries(path):
        if entry.sha1 != current_sha1:
            for e in sorted(group, key=lambda x: (x.repo_key, x.repo_path)):
                yield (e.sha1, e.repo_key, e.repo_path), e
            group = []
            current_sha1 = entry.sha1
        group.append(entry)
    for e in sorted(group, key=lambda x: (x.repo_key, x.repo_path)):
        yield (e.sha1, e.repo_key, e.repo_path), e


def added(previous: Path | None, current: Path) -> Iterator[ArtifactEntry]:
    """Yield entries present in ``current`` but absent from ``previous``.

    Identity is by (sha1, repo, path). The same sha1 appearing under a
    new (repo, path) is reported as added so the receiver creates the
    link, even though the underlying blob may already be on the receiver.
    """
    if previous is None or not previous.exists():
        for _key, entry in _iter_sorted_keys(current):
            yield entry
        return

    prev_it = _iter_sorted_keys(previous)
    cur_it = _iter_sorted_keys(current)
    prev = next(prev_it, None)
    cur = next(cur_it, None)

    while cur is not None:
        if prev is None:
            yield cur[1]
            cur = next(cur_it, None)
            continue
        if prev[0] < cur[0]:
            prev = next(prev_it, None)
        elif prev[0] == cur[0]:
            prev = next(prev_it, None)
            cur = next(cur_it, None)
        else:
            yield cur[1]
            cur = next(cur_it, None)

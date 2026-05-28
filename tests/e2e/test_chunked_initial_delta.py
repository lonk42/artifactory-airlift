"""End-to-end verification of the chunked-delta path (manifest v3).

Uploads several blobs whose cumulative raw bytes exceed
``AIRLIFT_MAX_ARCHIVE_BYTES`` on the sender sidecar, forcing the cycle
to split into multiple ``<parent>-cNNN.tar.zst`` archives. Then asserts:

* Multiple chunk files land in the sender's spool, all sharing one
  ``parent_cycle_id`` prefix.
* The standard shuttle delivers them in lexical order.
* Every uploaded artifact arrives on artifactory-b intact (bytes + sha1).
* The receiver's ``processed.jsonl`` shows ``N-1`` ``status=blob-staged``
  rows and one ``status=ok`` row, all carrying the same
  ``parent_cycle_id``.

To run, the sender sidecar must have a chunk threshold low enough for
the test's payload to exceed it. Pass the cluster's effective threshold
via ``E2E_CHUNK_THRESHOLD_BYTES`` (default ``10485760`` = 10 MiB, which
matches the dev cluster's test override). When unset and the cluster is
on the default 8 GiB threshold the test will still pass mechanically
(everything lands in one archive), but the chunk-count assertion guards
that we actually exercised the split path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from ._http import deploy, fetch
from .conftest import wait_until
from .helpers import kubectl_run, pod_for, shuttle_spool


# Chunk filenames are <parent_cycle_id>-cNNN.tar.zst where NNN is zero-padded
# 3-digit sequence (see sender._chunk_names). A loose "-c" substring match
# would false-positive on single-chunk archives whose uuid suffix happens to
# start with "c" (e.g. "1778637823-c126a4ec.tar.zst"); the strict suffix
# pattern below avoids that.
_CHUNK_NAME_RE = re.compile(
    r"^(?P<parent>\d{10}-[0-9a-f]{8})-c(?P<seq>\d{3})\.tar\.zst$"
)


def _group_chunks_by_parent(names: list[str]) -> dict[str, list[str]]:
    """Bucket chunk archive filenames by their parent_cycle_id."""
    parents: dict[str, list[str]] = {}
    for n in names:
        m = _CHUNK_NAME_RE.match(n)
        if m:
            parents.setdefault(m.group("parent"), []).append(n)
    return parents


def _spool_listing(kubectl: str, namespace: str, pod: str) -> list[str]:
    out = kubectl_run([
        kubectl, "-n", namespace, "exec", pod, "-c", "airlift", "--",
        "sh", "-c", "ls -1 /var/airlift/spool/*.tar.zst 2>/dev/null || true",
    ])
    return [Path(line).name for line in out.splitlines() if line.strip()]


def _processed_rows(kubectl: str, namespace: str, pod: str) -> list[dict]:
    out = kubectl_run([
        kubectl, "-n", namespace, "exec", pod, "-c", "airlift", "--",
        "sh", "-c", "cat /var/airlift/state/processed.jsonl 2>/dev/null || true",
    ])
    rows: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def test_chunked_initial_delta(art_a, art_b, test_repo, cycle_seconds, kube):
    threshold = int(os.environ.get("E2E_CHUNK_THRESHOLD_BYTES", str(10 * 1024**2)))
    # Six blobs sized so that exactly two fit per chunk under the configured
    # threshold. The grouping algorithm is greedy: a third blob would push
    # the cumulative size over the budget, so the chunk splits after every
    # second blob. Result: 3 chunks, 2 blobs each.
    blob_size = threshold // 3 + 1
    blob_count = 6

    print(
        f"\n  threshold={threshold}B ({threshold / 1024**2:.1f}MiB); "
        f"will upload {blob_count} blobs of {blob_size}B each "
        f"({blob_count * blob_size / 1024**2:.1f}MiB total)",
        flush=True,
    )

    pod_a = pod_for(kube["kubectl"], kube["ns_a"], kube["selector"])
    pod_b = pod_for(kube["kubectl"], kube["ns_b"], kube["selector"])
    print(f"  pods: src={pod_a} dst={pod_b}; cycle={cycle_seconds}s", flush=True)

    # The sender's "one delta in flight" gate refuses to start a cycle while
    # any prior archive remains in spool. Drain leftover archives from prior
    # runs BEFORE the upload, so the first post-upload cycle is unambiguously
    # the chunked cycle we are about to assert on.
    drain_started = time.time()

    def _spool_drained():
        names = _spool_listing(kube["kubectl"], kube["ns_a"], pod_a)
        moved = shuttle_spool(
            kube["kubectl"], src_ns=kube["ns_a"], dst_ns=kube["ns_b"],
            src_pod=pod_a, dst_pod=pod_b,
        ) if names else []
        elapsed = time.time() - drain_started
        print(
            f"  [{elapsed:5.1f}s] drain pass: pre={len(names)} moved={len(moved)}",
            flush=True,
        )
        post = _spool_listing(kube["kubectl"], kube["ns_a"], pod_a)
        return len(post) == 0

    assert wait_until(_spool_drained, timeout=cycle_seconds * 6 + 60, interval=5), (
        "sender spool would not drain; the new chunked cycle cannot start "
        "while archives remain (\"one delta in flight\" gate). Check the "
        "transport between A and B."
    )

    # Spool is empty. Upload the payload; the next sender cycle will pick
    # it up and produce a chunked delta.
    artifacts: list[tuple[str, bytes, str]] = []
    run_id = uuid.uuid4().hex[:8]
    for i in range(blob_count):
        blob = os.urandom(blob_size)
        sha1 = hashlib.sha1(blob).hexdigest()
        name = f"e2e-chunked-{run_id}-{i:02d}.bin"
        deploy(art_a, test_repo, name, blob, sha1)
        artifacts.append((name, blob, sha1))
        print(f"  uploaded {name} sha1={sha1[:12]}", flush=True)

    # Wait until the sender writes a chunked cycle (>= 2 archives sharing one
    # parent_cycle_id). Filenames match "<parent>-cNNN.tar.zst"; the bare
    # "<cycle_id>.tar.zst" form is reserved for single-chunk cycles, which
    # would mean the threshold was not exercised by our upload payload.
    #
    # Prior test runs can leave chunked archives in the spool, so we accept
    # any parent with >= 2 chunks and pick the most recent one (cycle ids
    # are <epoch>-<uuid8>, sortable by epoch prefix). The test's own parent
    # is the newest, since uploads happen seconds before this poll begins.
    chunk_wait_started = time.time()

    def _chunked_parent():
        names = _spool_listing(kube["kubectl"], kube["ns_a"], pod_a)
        parents = _group_chunks_by_parent(names)
        multi = {p: cs for p, cs in parents.items() if len(cs) >= 2}
        elapsed = time.time() - chunk_wait_started
        print(
            f"  [{elapsed:5.1f}s] sender spool: total={len(names)} "
            f"chunked_parents={list(multi.keys())} "
            f"chunks_per_parent={{ {', '.join(f'{p}:{len(cs)}' for p, cs in multi.items())} }}",
            flush=True,
        )
        if not multi:
            return None
        # Pick the lex-max parent: cycle ids start with a 10-digit epoch, so
        # max() == newest. That is the one this test just created.
        newest_parent = max(multi)
        return newest_parent, sorted(multi[newest_parent])

    result = wait_until(_chunked_parent, timeout=cycle_seconds * 4 + 60, interval=5)
    assert result, (
        f"no chunked cycle (>= 2 chunks under one parent_cycle_id) appeared "
        f"in sender spool within {cycle_seconds * 4 + 60}s; check "
        f"AIRLIFT_MAX_ARCHIVE_BYTES on the sender sidecar and "
        f"E2E_CHUNK_THRESHOLD_BYTES on this test (currently {threshold}B)"
    )
    parent_id, chunks = result
    print(f"  saw {len(chunks)} chunks for parent={parent_id}: {chunks}", flush=True)

    # Shuttle in lexical order (the helper already sorts by ls -1) and wait
    # for every artifact to arrive on B. The final chunk runs the import +
    # delete steps; earlier chunks just stage blobs.
    arrive_started = time.time()

    def _shuttle_until_all_present():
        moved = shuttle_spool(
            kube["kubectl"], src_ns=kube["ns_a"], dst_ns=kube["ns_b"],
            src_pod=pod_a, dst_pod=pod_b,
        )
        present = 0
        for name, _, _ in artifacts:
            status, _c, _b = fetch(art_b, test_repo, name)
            if status == 200:
                present += 1
        elapsed = time.time() - arrive_started
        print(
            f"  [{elapsed:5.1f}s] arrivals on B: {present}/{blob_count} "
            f"moved_this_pass={','.join(moved) if moved else '-'}",
            flush=True,
        )
        return present == blob_count

    assert wait_until(_shuttle_until_all_present, timeout=cycle_seconds * 8 + 120, interval=5), (
        "not all chunked artifacts arrived on B within window"
    )

    # Each artifact's bytes must round-trip with matching sha1.
    for name, blob, sha1 in artifacts:
        status, checksum, body = fetch(art_b, test_repo, name)
        assert status == 200, f"{name}: GET returned {status}"
        assert checksum == sha1, f"{name}: checksum mismatch ({checksum} != {sha1})"
        assert hashlib.sha1(body).hexdigest() == sha1, f"{name}: body sha1 mismatch"
        assert body == blob, f"{name}: body bytes mismatch"

    # processed.jsonl on the receiver should record one row per chunk, all
    # tagged with the same parent_cycle_id. Chunks 1..N-1 are "blob-staged"
    # (no import, no delete); the final chunk is "ok" (import + delete done).
    rows = _processed_rows(kube["kubectl"], kube["ns_b"], pod_b)
    chunk_rows = [r for r in rows if r.get("parent_cycle_id") == parent_id]
    assert len(chunk_rows) == len(chunks), (
        f"expected {len(chunks)} processed rows for parent {parent_id}, "
        f"got {len(chunk_rows)}: {chunk_rows}"
    )
    staged = [r for r in chunk_rows if r.get("status") == "blob-staged"]
    final = [r for r in chunk_rows if r.get("status") in ("ok", "partial")]
    assert len(staged) == len(chunks) - 1, (
        f"expected {len(chunks) - 1} blob-staged rows, got {len(staged)}: "
        f"{[r.get('cycle_id') for r in staged]}"
    )
    assert len(final) == 1, (
        f"expected exactly one commit-chunk row, got {len(final)}: "
        f"{[(r.get('cycle_id'), r.get('status')) for r in final]}"
    )
    assert final[0]["chunk_seq"] == final[0]["chunk_total"], (
        f"commit chunk must satisfy chunk_seq == chunk_total: {final[0]}"
    )
    print(
        f"  ledger ok: {len(staged)} blob-staged + 1 commit "
        f"(status={final[0]['status']}) for parent={parent_id}",
        flush=True,
    )

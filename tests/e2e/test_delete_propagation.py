"""End-to-end verification of deletion propagation (manifest v2 `removed[]`).

Runs against `airlift-npm-test-local`, the unproject-scoped control repo
provisioned by the session fixture.
"""

from __future__ import annotations

import time

import pytest

from . import artifacts
from ._http import delete, deploy, fetch
from .conftest import wait_until
from .helpers import pod_for, shuttle_spool


REPO = "airlift-npm-test-local"


def _progress(label: str, started: float, moved: list[str], status: int) -> None:
    elapsed = time.time() - started
    note = f" moved={','.join(moved)}" if moved else ""
    print(
        f"  [{elapsed:5.1f}s] {label}: status_on_B={status}{note}",
        flush=True,
    )


def test_deletion_propagates_to_b(art_a, art_b, cycle_seconds, kube, provisioned_repos):
    filename, blob, sha1 = artifacts.build("npm")
    print(f"\n  uploading {filename} ({len(blob)}B sha1={sha1[:12]}) to A:{REPO}", flush=True)
    deploy(art_a, REPO, filename, blob, sha1)

    pod_a = pod_for(kube["kubectl"], kube["ns_a"], kube["selector"])
    pod_b = pod_for(kube["kubectl"], kube["ns_b"], kube["selector"])
    print(f"  pods: src={pod_a} dst={pod_b}; cycle={cycle_seconds}s", flush=True)

    arrive_started = time.time()

    def _shuttle_until_present():
        moved = shuttle_spool(
            kube["kubectl"], src_ns=kube["ns_a"], dst_ns=kube["ns_b"],
            src_pod=pod_a, dst_pod=pod_b,
        )
        status, _checksum, _body = fetch(art_b, REPO, filename)
        _progress("await upload  ", arrive_started, moved, status)
        return status == 200

    print(f"  waiting for upload to land on B (budget {cycle_seconds * 4 + 60}s)", flush=True)
    assert wait_until(_shuttle_until_present, timeout=cycle_seconds * 4 + 60, interval=5), (
        f"upload of {filename} did not arrive on B before delete step"
    )
    print(f"  upload landed on B after {time.time() - arrive_started:.1f}s", flush=True)

    status = delete(art_a, REPO, filename)
    assert status in (200, 204), f"DELETE on A returned {status}"
    print(f"  DELETE on A returned {status}; waiting for B to mirror the removal", flush=True)

    delete_started = time.time()

    def _shuttle_until_absent():
        moved = shuttle_spool(
            kube["kubectl"], src_ns=kube["ns_a"], dst_ns=kube["ns_b"],
            src_pod=pod_a, dst_pod=pod_b,
        )
        status, _c, _b = fetch(art_b, REPO, filename)
        _progress("await deletion", delete_started, moved, status)
        return status == 404

    assert wait_until(_shuttle_until_absent, timeout=cycle_seconds * 4 + 60, interval=5), (
        f"{filename} was not removed from B after delete on A"
    )
    print(f"  deletion mirrored to B after {time.time() - delete_started:.1f}s", flush=True)

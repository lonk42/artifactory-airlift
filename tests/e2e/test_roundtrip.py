from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from .conftest import ArtClient, wait_until
from .helpers import pod_for, shuttle_spool


@pytest.fixture(scope="module")
def payload() -> tuple[bytes, str]:
    blob = os.urandom(1024 * 1024)
    sha1 = hashlib.sha1(blob).hexdigest()
    return blob, sha1


def _deploy(client: ArtClient, repo: str, path: str, blob: bytes, sha1: str) -> None:
    with client.http() as http:
        r = http.put(
            f"/{repo}/{path}",
            content=blob,
            headers={
                "X-Checksum-Sha1": sha1,
                "Content-Type": "application/octet-stream",
            },
        )
        r.raise_for_status()


def _fetch(client: ArtClient, repo: str, path: str) -> tuple[int, str | None, bytes]:
    with client.http() as http:
        r = http.get(f"/{repo}/{path}")
        return r.status_code, r.headers.get("X-Checksum-Sha1"), r.content


def test_roundtrip(art_a, art_b, test_repo, cycle_seconds, kube, payload):
    blob, sha1 = payload
    name = f"e2e-{uuid.uuid4().hex}.bin"
    _deploy(art_a, test_repo, name, blob, sha1)

    pod_a = pod_for(kube["kubectl"], kube["ns_a"], kube["selector"])
    pod_b = pod_for(kube["kubectl"], kube["ns_b"], kube["selector"])

    def _shuttle_then_check():
        shuttle_spool(
            kube["kubectl"],
            src_ns=kube["ns_a"],
            dst_ns=kube["ns_b"],
            src_pod=pod_a,
            dst_pod=pod_b,
        )
        status, _checksum, _body = _fetch(art_b, test_repo, name)
        return status == 200

    landed = wait_until(_shuttle_then_check, timeout=cycle_seconds * 4 + 60)
    assert landed, f"{name} did not arrive on artifactory-b within window"

    status, checksum, body = _fetch(art_b, test_repo, name)
    assert status == 200
    assert checksum == sha1
    assert hashlib.sha1(body).hexdigest() == sha1
    assert body == blob

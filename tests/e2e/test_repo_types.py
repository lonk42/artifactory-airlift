"""Parameterised roundtrip over the typed, project-scoped repos.

Each iteration uploads a minimal-but-format-shaped artifact to instance A,
shuttles the airlift spool to instance B, and verifies bit-for-bit parity.
Covers RPM, Debian, Helm, PyPI, and npm; all five repos are assigned to the
"airlift" project, so this exercises project-scoped repos too.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from . import artifacts
from ._http import deploy, fetch
from .conftest import PROVISION_SPECS, wait_until
from .helpers import pod_for, shuttle_spool


# parametrize is resolved at collection time, before the session fixture runs;
# the static PROVISION_SPECS in conftest.py is the source of truth for the
# repo matrix, and the project-scoped subset is what this test covers.
PROJECT_SCOPED = [spec for spec in PROVISION_SPECS if spec[2] is not None]


@pytest.mark.parametrize("spec", PROJECT_SCOPED, ids=lambda s: s[1])
def test_typed_repo_roundtrip(spec, art_a, art_b, cycle_seconds, kube, provisioned_repos):
    repo_key, package_type, _project = spec
    filename, blob, sha1 = artifacts.build(package_type)
    print(f"\n  [{package_type}] uploading {filename} ({len(blob)}B sha1={sha1[:12]}) to A:{repo_key}", flush=True)
    deploy(art_a, repo_key, filename, blob, sha1)

    pod_a = pod_for(kube["kubectl"], kube["ns_a"], kube["selector"])
    pod_b = pod_for(kube["kubectl"], kube["ns_b"], kube["selector"])

    started = time.time()

    def _shuttle_then_check():
        moved = shuttle_spool(
            kube["kubectl"], src_ns=kube["ns_a"], dst_ns=kube["ns_b"],
            src_pod=pod_a, dst_pod=pod_b,
        )
        status, _checksum, _body = fetch(art_b, repo_key, filename)
        elapsed = time.time() - started
        note = f" moved={','.join(moved)}" if moved else ""
        print(f"  [{package_type}] [{elapsed:5.1f}s] status_on_B={status}{note}", flush=True)
        return status == 200

    budget = cycle_seconds * 4 + 60
    print(f"  [{package_type}] polling B for {filename} (budget {budget}s)", flush=True)
    landed = wait_until(_shuttle_then_check, timeout=budget, interval=5)
    assert landed, f"{package_type} artifact {filename} did not arrive on B"

    status, checksum, body = fetch(art_b, repo_key, filename)
    assert status == 200
    assert checksum == sha1, f"sha1 mismatch on {repo_key}/{filename}"
    assert hashlib.sha1(body).hexdigest() == sha1
    assert body == blob


def test_provisioning_ran(provisioned_repos):
    # Sanity: the fixture executed and gave us the expected six repos.
    keys = {r.key for r in provisioned_repos}
    assert keys == {
        "airlift-rpm-local",
        "airlift-deb-local",
        "airlift-helm-local",
        "airlift-pypi-local",
        "airlift-npm-local",
        "airlift-npm-test-local",
    }

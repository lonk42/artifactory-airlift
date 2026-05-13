"""In-memory generators for minimal, format-shaped test artifacts.

Airlift moves blobs by sha1 and re-imports them through the system import API;
the receiver does not parse package contents. So for the e2e battery, we only
need uploads to be:

- Right file extension (so the repo accepts the PUT)
- Magic-byte-shaped enough that Artifactory does not reject on read
- Non-empty so the sha1 is unique per call

Each builder returns (filename, body_bytes, sha1_hex). Filenames embed a UUID
so each call is unique even within a single test run.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import tarfile
import uuid
import zipfile


def _finalise(filename: str, body: bytes) -> tuple[str, bytes, str]:
    return filename, body, hashlib.sha1(body).hexdigest()


def rpm_artifact() -> tuple[str, bytes, str]:
    # RPM lead magic ed ab ee db, then 92 bytes of zero padding to reach the
    # 96-byte lead, followed by a small random payload. Artifactory accepts
    # the upload because the lead recognises as RPM; index generation will fail
    # later for this fake package, but airlift never triggers indexing.
    lead = b"\xed\xab\xee\xdb" + b"\x00" * 92
    body = lead + os.urandom(256)
    return _finalise(f"airlift-test-{uuid.uuid4().hex}.rpm", body)


def deb_artifact() -> tuple[str, bytes, str]:
    # Minimal ar archive: 8-byte global header + one entry with a tiny payload.
    # ar entry header: 16-byte name, 12 mtime, 6 uid, 6 gid, 8 mode, 10 size, 2 magic.
    payload = b"airlift test " + uuid.uuid4().hex.encode()
    name = b"debian-binary".ljust(16)
    mtime = b"0".ljust(12)
    uid = b"0".ljust(6)
    gid = b"0".ljust(6)
    mode = b"100644".ljust(8)
    size = str(len(payload)).encode().ljust(10)
    fmag = b"`\n"
    entry = name + mtime + uid + gid + mode + size + fmag + payload
    if len(payload) % 2:
        entry += b"\n"
    body = b"!<arch>\n" + entry
    return _finalise(f"airlift-test-{uuid.uuid4().hex}.deb", body)


def _make_targz(files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return gzip.compress(raw.getvalue())


def helm_artifact() -> tuple[str, bytes, str]:
    name = f"airlift-test-{uuid.uuid4().hex[:8]}"
    version = "0.0.1"
    chart_yaml = (
        f"apiVersion: v2\nname: {name}\nversion: {version}\n"
        "description: airlift e2e fixture\ntype: application\n"
    ).encode()
    body = _make_targz({f"{name}/Chart.yaml": chart_yaml})
    return _finalise(f"{name}-{version}.tgz", body)


def npm_artifact() -> tuple[str, bytes, str]:
    name = f"airlift-test-{uuid.uuid4().hex[:8]}"
    version = "0.0.1"
    pkg_json = (
        f'{{"name":"{name}","version":"{version}",'
        '"description":"airlift e2e fixture"}'
    ).encode()
    # npm packs files under a top-level "package/" directory.
    body = _make_targz({"package/package.json": pkg_json})
    return _finalise(f"{name}-{version}.tgz", body)


def pypi_artifact() -> tuple[str, bytes, str]:
    name = f"airlift_test_{uuid.uuid4().hex[:8]}"
    version = "0.0.1"
    dist_info = f"{name}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        "Summary: airlift e2e fixture\n"
    ).encode()
    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: airlift-e2e\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode()
    record = (
        f"{dist_info}/METADATA,,{len(metadata)}\n"
        f"{dist_info}/WHEEL,,{len(wheel_meta)}\n"
        f"{dist_info}/RECORD,,\n"
    ).encode()
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/WHEEL", wheel_meta)
        zf.writestr(f"{dist_info}/RECORD", record)
    body = raw.getvalue()
    return _finalise(f"{name}-{version}-py3-none-any.whl", body)


BUILDERS = {
    "rpm": rpm_artifact,
    "debian": deb_artifact,
    "helm": helm_artifact,
    "npm": npm_artifact,
    "pypi": pypi_artifact,
}


def build(package_type: str) -> tuple[str, bytes, str]:
    builder = BUILDERS.get(package_type.lower())
    if builder is None:
        raise ValueError(f"no artifact builder for package type {package_type!r}")
    return builder()

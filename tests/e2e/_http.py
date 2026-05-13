"""Shared helpers for issuing PUT/GET/DELETE against an Artifactory instance.

Lifted out of the original test_roundtrip.py so every e2e test uses the same
upload/fetch shape.
"""

from __future__ import annotations

from .conftest import ArtClient


def deploy(client: ArtClient, repo: str, path: str, blob: bytes, sha1: str) -> None:
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


def fetch(client: ArtClient, repo: str, path: str) -> tuple[int, str | None, bytes]:
    with client.http() as http:
        r = http.get(f"/{repo}/{path}")
        return r.status_code, r.headers.get("X-Checksum-Sha1"), r.content


def delete(client: ArtClient, repo: str, path: str) -> int:
    with client.http() as http:
        r = http.delete(f"/{repo}/{path}")
        return r.status_code

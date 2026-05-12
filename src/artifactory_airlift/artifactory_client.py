from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import log

logger = log.get("artifactory.client")

_RETRY = dict(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
)


class ArtifactoryClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        username: str = "",
        password: str = "",
        timeout: float = 60.0,
    ):
        if base_url.endswith("/"):
            base_url = base_url[:-1]
        self.base_url = base_url
        # Basic auth (when both username + password are supplied) takes
        # precedence over bearer because in this codebase it's the
        # explicit override path; operators who want the simpler,
        # reliably-scoped path can set both and ignore the token field.
        auth: httpx.BasicAuth | None = None
        headers: dict[str, str] = {}
        if username and password:
            auth = httpx.BasicAuth(username, password)
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        # Separate clients: a long-poll one for export, normal one for everything else.
        self._http = httpx.Client(headers=headers, auth=auth, timeout=timeout)
        self._http_long = httpx.Client(headers=headers, auth=auth, timeout=None)

    def close(self) -> None:
        self._http.close()
        self._http_long.close()

    def __enter__(self) -> "ArtifactoryClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @retry(**_RETRY)
    def ping(self) -> bool:
        r = self._http.get(f"{self.base_url}/api/system/ping")
        r.raise_for_status()
        return r.text.strip() == "OK"

    @retry(**_RETRY)
    def list_repositories(self) -> list[dict[str, Any]]:
        r = self._http.get(f"{self.base_url}/api/repositories")
        r.raise_for_status()
        return r.json()

    def export_system(self, export_path: Path) -> None:
        """Trigger a system export with excludeContent=true.

        Artifactory writes the export tree to ``export_path`` on the
        instance's local disk. Because airlift runs as a sidecar with
        the binarystore PVC mounted, that path is visible to us directly;
        no need to stream the archive over HTTP.
        """
        body = {
            "exportPath": str(export_path),
            "includeMetadata": True,
            "createArchive": False,
            "bypassFiltering": False,
            "verbose": False,
            "failOnError": True,
            "failIfEmpty": True,
            "m2": False,
            "incremental": False,
            "excludeContent": True,
        }
        logger.info("export.start", path=str(export_path))
        r = self._http_long.post(f"{self.base_url}/api/export/system", json=body)
        r.raise_for_status()
        logger.info("export.done", path=str(export_path))

    @retry(**_RETRY)
    def import_repositories(self, path: Path) -> str:
        """Run a batch repository import against ``path``.

        ``path`` must be the directory that *contains* the per-repo
        subdirectories (i.e. the `repositories/` directory from a
        system export). Returns the verbose response body so callers
        can record per-repo errors; Artifactory replies 200 even when
        individual repos fail with ``500 :`` lines inline.

        ``path`` also must not be under Artifactory's own data dir
        (``/var/opt/jfrog/artifactory/...``); it gets rejected the
        same way ``/api/import/system`` does. Use the state PVC.
        """
        params = {"path": str(path), "verbose": "1"}
        r = self._http_long.post(
            f"{self.base_url}/api/import/repositories", params=params
        )
        r.raise_for_status()
        logger.info("import.repositories_done", path=str(path))
        return r.text

    @retry(**_RETRY)
    def delete_artifact(self, repo_key: str, repo_path: str) -> int:
        """Delete a single artifact. Returns HTTP status.

        A 404 is treated as success (idempotent desired-state-already-achieved)
        and surfaced as the status code rather than raised, so the tenacity
        retry doesn't fire on a known-missing artifact. Any other non-2xx
        still raises and retries per ``_RETRY``.
        """
        url = f"{self.base_url}/{repo_key}/{repo_path.lstrip('/')}"
        r = self._http.delete(url)
        if r.status_code == 404:
            return 404
        r.raise_for_status()
        return r.status_code

    def deploy_by_checksum(
        self, repo_key: str, repo_path: str, sha1: str, size: int
    ) -> None:
        """Fallback: link an artifact to an already-present sha1 blob.

        Used when the import API refuses to re-link a checksum that
        already exists in the filestore.
        """
        url = f"{self.base_url}/{repo_key}/{repo_path.lstrip('/')}"
        headers = {
            "X-Checksum-Deploy": "true",
            "X-Checksum-Sha1": sha1,
            "Content-Length": str(size),
        }
        r = self._http.put(url, headers=headers, content=b"")
        r.raise_for_status()

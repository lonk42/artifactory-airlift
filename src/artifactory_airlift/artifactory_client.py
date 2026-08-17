from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .config import Settings
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


class FileTokenAuth(httpx.Auth):
    """Bearer auth that re-reads the token from disk on every request.

    For deployments where the token is rotated in place: a Kubernetes Secret
    refreshed by an external controller, or an agent rewriting the file. The
    client itself lives for the whole process (one long-running loop), so a
    token resolved at construction would go stale at the first rotation and
    only recover on a restart. Reading inside ``auth_flow`` means each
    request carries whatever the file holds right now, and the retry
    decorator on the calling methods turns a mid-rotation 401 into a retry
    with the fresh token.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def auth_flow(self, request: httpx.Request):
        # Strip: secret tooling almost always leaves a trailing newline,
        # which would otherwise corrupt the header value.
        token = self.path.read_text().strip()
        if not token:
            raise RuntimeError(f"artifactory token file is empty: {self.path}")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class ArtifactoryClient:
    @classmethod
    def from_settings(cls, settings: "Settings") -> "ArtifactoryClient":
        """Build a client from Settings, resolving auth and the TLS verify target.

        When ``artifactory_ca_cert`` is set it becomes the httpx ``verify``
        value (a path to a PEM CA bundle used to trust a private/self-signed
        CA); otherwise verification uses httpx's bundled certifi store.

        ``artifactory_token_file`` selects the rotation-aware bearer auth
        (see :class:`FileTokenAuth`) in place of the static
        ``artifactory_token``.
        """
        verify: bool | str = settings.artifactory_ca_cert or True
        return cls(
            settings.artifactory_url,
            settings.artifactory_token,
            username=settings.artifactory_username,
            password=settings.artifactory_password,
            token_file=settings.artifactory_token_file,
            verify=verify,
        )

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        username: str = "",
        password: str = "",
        token_file: str = "",
        timeout: float = 60.0,
        verify: bool | str = True,
    ):
        if base_url.endswith("/"):
            base_url = base_url[:-1]
        self.base_url = base_url
        # Basic auth (when both username + password are supplied) takes
        # precedence over bearer because in this codebase it's the
        # explicit override path; operators who want the simpler,
        # reliably-scoped path can set both and ignore the token field.
        # A token file outranks a literal token: naming a file is the more
        # deliberate act, and it is the only one of the two that survives
        # rotation.
        auth: httpx.Auth | None = None
        headers: dict[str, str] = {}
        if username and password:
            auth = httpx.BasicAuth(username, password)
        elif token_file:
            auth = FileTokenAuth(token_file)
            if not Path(token_file).is_file():
                # Not fatal: an agent may not have written it yet, and the
                # per-request read is what actually matters. Log it so a
                # mis-wired mount is visible without a crashloop.
                logger.warning("artifactory.token_file_missing", path=token_file)
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        # Separate clients: a long-poll one for export, normal one for everything else.
        self._http = httpx.Client(
            headers=headers, auth=auth, timeout=timeout, verify=verify
        )
        self._http_long = httpx.Client(
            headers=headers, auth=auth, timeout=None, verify=verify
        )

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

    @retry(**_RETRY)
    def aql(self, query: str) -> list[dict[str, Any]]:
        """Run an AQL query and return its ``results`` list.

        Uses the long-timeout client: a whole-instance enumeration on a
        populated source is a single request that can run for seconds and
        return tens of megabytes.

        The response also carries a ``range`` block; ``range.total`` is
        returned alongside so callers can cross-check it against the number
        of rows they actually received. See ``aql.py`` for why that matters.
        """
        r = self._http_long.post(
            f"{self.base_url}/api/search/aql",
            content=query,
            headers={"Content-Type": "text/plain"},
        )
        r.raise_for_status()
        return r.json().get("results", [])

    @retry(**_RETRY)
    def aql_count(self, criteria: str) -> int:
        """Return ``range.total`` for ``items.find(<criteria>)`` with no projection.

        Deliberately issues the query with **no** ``include()`` clause, which
        is the only form guaranteed not to collapse rows (see ``aql.py``).
        Used as an independent row-count reference.
        """
        r = self._http_long.post(
            f"{self.base_url}/api/search/aql",
            content=f"items.find({criteria})",
            headers={"Content-Type": "text/plain"},
        )
        r.raise_for_status()
        return int(r.json().get("range", {}).get("total", 0))

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

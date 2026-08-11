"""Azure AD tokens for a platform-assigned identity.

Artifactory's ``<useInstanceCredentials>true</useInstanceCredentials>`` means
it never holds a storage credential: it asks the platform who it is and gets a
short-lived OAuth token for the storage service. Airlift runs beside it under
the same identity, so it can do exactly the same and needs no account key.

Two sources are supported, detected in this order:

* **Federated (workload) identity.** A projected token file plus
  ``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` in the environment, exchanged at
  the authority for an access token. Preferred when present, because it is
  scoped to this workload rather than to the whole node.
* **Instance metadata.** A link-local endpoint that mints a token for the
  identity attached to the compute, reachable by anything running on it.

Deliberately implemented on httpx rather than by taking a dependency on the
Azure SDK, matching how the S3 and Azure signing in this package already
avoids vendor SDKs.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from tenacity import retry

from .. import log
from ._retry import RETRY

logger = log.get("binarystore.azure_identity")

# The storage data plane's resource id. The v2 endpoint wants it as a scope
# with the ".default" suffix; the instance metadata endpoint wants the bare
# resource, so both spellings are kept.
_STORAGE_RESOURCE = "https://storage.azure.com/"
_STORAGE_SCOPE = "https://storage.azure.com/.default"

_DEFAULT_AUTHORITY = "https://login.microsoftonline.com/"
_IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
_IMDS_API_VERSION = "2018-02-01"

# Refresh this long before expiry, so a token never expires mid-upload. A
# staged-block upload of a large artifact can run for minutes.
_REFRESH_MARGIN_SECONDS = 300

_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class IdentityUnavailable(RuntimeError):
    """No platform identity could be found in this environment."""


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires_at: float

    @property
    def stale(self) -> bool:
        return time.time() >= self.expires_at - _REFRESH_MARGIN_SECONDS


class FederatedTokenSource:
    """Exchange a projected federated token for an access token.

    The assertion file is re-read on every exchange, never cached: the
    platform rotates it (commonly hourly) and a container that read it once at
    start would fail the moment it turned over.
    """

    kind = "federated"

    def __init__(
        self,
        *,
        client_id: str,
        tenant_id: str,
        token_file: Path,
        authority: str,
        http: httpx.Client,
    ) -> None:
        self._client_id = client_id
        self._token_file = token_file
        self._http = http
        base = authority.rstrip("/")
        self._url = f"{base}/{tenant_id}/oauth2/v2.0/token"

    def describe(self) -> str:
        return f"federated identity {self._client_id} via {self._token_file}"

    @retry(**RETRY)
    def fetch(self) -> _Token:
        assertion = self._token_file.read_text().strip()
        if not assertion:
            raise IdentityUnavailable(f"federated token file is empty: {self._token_file}")

        response = self._http.post(
            self._url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "scope": _STORAGE_SCOPE,
                "client_assertion_type": _ASSERTION_TYPE,
                "client_assertion": assertion,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return _Token(
            value=payload["access_token"],
            expires_at=time.time() + float(payload.get("expires_in", 3600)),
        )


class InstanceMetadataTokenSource:
    """Ask the compute's own metadata endpoint for a token."""

    kind = "instance-metadata"

    def __init__(self, *, client_id: str, http: httpx.Client) -> None:
        self._client_id = client_id
        self._http = http

    def describe(self) -> str:
        target = self._client_id or "system-assigned identity"
        return f"instance metadata ({target})"

    @retry(**RETRY)
    def fetch(self) -> _Token:
        params = {"api-version": _IMDS_API_VERSION, "resource": _STORAGE_RESOURCE}
        if self._client_id:
            params["client_id"] = self._client_id

        response = self._http.get(
            _IMDS_TOKEN_URL, params=params, headers={"Metadata": "true"}
        )
        response.raise_for_status()
        payload = response.json()
        # This endpoint reports absolute epoch seconds, as a string, rather
        # than the relative "expires_in" the OAuth endpoint returns.
        return _Token(
            value=payload["access_token"],
            expires_at=float(payload["expires_on"]),
        )


class TokenCredential:
    """A cached access token for the platform identity.

    One instance is shared by every request the blob store makes; ``token()``
    hands back the cached value until it is close enough to expiry to be worth
    replacing.
    """

    def __init__(self, source, *, http: httpx.Client) -> None:
        self._source = source
        self._http = http
        self._token: _Token | None = None

    @property
    def kind(self) -> str:
        return self._source.kind

    def describe(self) -> str:
        return self._source.describe()

    def token(self) -> str:
        if self._token is None or self._token.stale:
            self._token = self._source.fetch()
            logger.debug("binarystore.azure_token_acquired", source=self._source.kind)
        return self._token.value

    def close(self) -> None:
        self._http.close()


def detect(*, env: dict[str, str] | None = None, timeout: float = 30.0):
    """Build a credential from the environment, or raise IdentityUnavailable.

    Detection is by what the platform actually injected rather than by a
    setting, so an operator does not have to restate which identity flavour a
    cluster uses.
    """
    env = os.environ if env is None else env

    client_id = env.get("AZURE_CLIENT_ID", "").strip()
    tenant_id = env.get("AZURE_TENANT_ID", "").strip()
    token_file = env.get("AZURE_FEDERATED_TOKEN_FILE", "").strip()

    if token_file and client_id and tenant_id:
        path = Path(token_file)
        if not path.is_file():
            raise IdentityUnavailable(
                f"AZURE_FEDERATED_TOKEN_FILE points at {token_file}, which does not "
                "exist in this container. The projected token volume has to be "
                "mounted into the airlift container, not only into Artifactory's."
            )
        http = httpx.Client(timeout=timeout)
        authority = env.get("AZURE_AUTHORITY_HOST", "").strip() or _DEFAULT_AUTHORITY
        source = FederatedTokenSource(
            client_id=client_id,
            tenant_id=tenant_id,
            token_file=path,
            authority=authority,
            http=http,
        )
        return TokenCredential(source, http=http)

    # No federated identity. The metadata endpoint is only worth trying where
    # something suggests a managed identity is present, since an unreachable
    # link-local address otherwise costs a timeout on every startup.
    if client_id or env.get("AZURE_USE_INSTANCE_METADATA", "").strip().lower() == "true":
        http = httpx.Client(timeout=timeout)
        return TokenCredential(
            InstanceMetadataTokenSource(client_id=client_id, http=http), http=http
        )

    raise IdentityUnavailable(
        "no Azure identity found in this container: expected "
        "AZURE_FEDERATED_TOKEN_FILE with AZURE_CLIENT_ID and AZURE_TENANT_ID "
        "(workload identity), or AZURE_CLIENT_ID for a managed identity"
    )

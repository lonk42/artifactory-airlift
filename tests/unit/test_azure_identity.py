"""Authenticating to Azure Blob as a platform identity instead of with a key.

Token endpoints are driven through httpx's MockTransport, so the exact request
shapes are asserted (they are the part that has to match Azure, and the part
that cannot be checked from a test environment).
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from artifactory_airlift.binarystore import azure_identity
from artifactory_airlift.binarystore.azure import AzureBlobStore
from artifactory_airlift.binarystore.azure_identity import (
    FederatedTokenSource,
    IdentityUnavailable,
    InstanceMetadataTokenSource,
    TokenCredential,
)
from artifactory_airlift.binarystore.config import AzureConfig

SHA1 = "5f85c6dfc672334addea0fe63cc7132ca0498fc6"

CFG = AzureConfig(
    container="artifactory",
    endpoint_url="https://acct.blob.core.windows.net",
    account="acct",
    prefix="filestore",
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _federated(handler, token_file: Path, **kwargs) -> TokenCredential:
    source = FederatedTokenSource(
        client_id="client-1",
        tenant_id="tenant-1",
        token_file=token_file,
        authority=kwargs.pop("authority", "https://login.microsoftonline.com/"),
        http=_client(handler),
    )
    return TokenCredential(source, http=_client(handler))


def test_federated_exchange_request_shape(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("assertion-jwt\n")
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(
            pair.split("=", 1) for pair in request.content.decode().split("&") if pair
        )
        seen.append((str(request.url), body))
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    credential = _federated(handler, token_file)
    assert credential.token() == "tok-1"

    url, body = seen[0]
    assert url == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    assert body["grant_type"] == "client_credentials"
    assert body["client_id"] == "client-1"
    # The trailing newline every projected token file carries must not reach
    # the assertion, and the scope needs the ".default" suffix on the v2
    # endpoint.
    assert body["client_assertion"] == "assertion-jwt"
    assert body["scope"] == "https%3A%2F%2Fstorage.azure.com%2F.default"


def test_federated_token_file_is_reread_every_exchange(tmp_path: Path) -> None:
    """The platform rotates the projected token; caching it would break later."""
    token_file = tmp_path / "token"
    token_file.write_text("first")
    assertions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(
            pair.split("=", 1) for pair in request.content.decode().split("&") if pair
        )
        assertions.append(body["client_assertion"])
        # Already expired, so the next token() call must exchange again.
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 0})

    credential = _federated(handler, token_file)
    credential.token()
    token_file.write_text("rotated")
    credential.token()

    assert assertions == ["first", "rotated"]


def test_empty_token_file_is_reported(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("\n")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the authority")

    credential = _federated(handler, token_file)
    with pytest.raises(IdentityUnavailable, match="empty"):
        credential.token()


def test_token_is_cached_until_close_to_expiry(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("assertion")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    credential = _federated(handler, token_file)
    for _ in range(5):
        credential.token()
    assert len(calls) == 1


def test_token_refreshes_before_it_expires(tmp_path: Path) -> None:
    """Refreshed early, so a token cannot expire mid-upload."""
    token_file = tmp_path / "token"
    token_file.write_text("assertion")
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        # Inside the refresh margin, though not yet expired.
        return httpx.Response(
            200,
            json={
                "access_token": "tok",
                "expires_in": azure_identity._REFRESH_MARGIN_SECONDS - 1,
            },
        )

    credential = _federated(handler, token_file)
    credential.token()
    credential.token()
    assert len(calls) == 2


def test_instance_metadata_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "tok-imds",
                # This endpoint reports absolute epoch seconds, as a string.
                "expires_on": str(int(time.time()) + 3600),
            },
        )

    http = _client(handler)
    credential = TokenCredential(
        InstanceMetadataTokenSource(client_id="user-assigned-1", http=http), http=http
    )
    assert credential.token() == "tok-imds"

    request = seen[0]
    assert request.url.host == "169.254.169.254"
    assert request.headers["Metadata"] == "true"
    assert request.url.params["resource"] == "https://storage.azure.com/"
    assert request.url.params["client_id"] == "user-assigned-1"


def test_detect_prefers_federated_identity(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("assertion")
    credential = azure_identity.detect(
        env={
            "AZURE_CLIENT_ID": "client-1",
            "AZURE_TENANT_ID": "tenant-1",
            "AZURE_FEDERATED_TOKEN_FILE": str(token_file),
        }
    )
    assert credential.kind == "federated"
    credential.close()


def test_detect_falls_back_to_instance_metadata() -> None:
    credential = azure_identity.detect(env={"AZURE_CLIENT_ID": "client-1"})
    assert credential.kind == "instance-metadata"
    credential.close()


def test_detect_without_an_identity_is_reported() -> None:
    with pytest.raises(IdentityUnavailable, match="no Azure identity"):
        azure_identity.detect(env={})


def test_detect_names_a_token_file_missing_from_this_container(tmp_path: Path) -> None:
    """The projected volume has to be mounted into the airlift container too."""
    with pytest.raises(IdentityUnavailable, match="does not exist in this container"):
        azure_identity.detect(
            env={
                "AZURE_CLIENT_ID": "client-1",
                "AZURE_TENANT_ID": "tenant-1",
                "AZURE_FEDERATED_TOKEN_FILE": str(tmp_path / "absent"),
            }
        )


class _StubCredential:
    kind = "stub"

    def __init__(self) -> None:
        self.closed = False
        self.tokens = 0

    def token(self) -> str:
        self.tokens += 1
        return f"tok-{self.tokens}"

    def describe(self) -> str:
        return "stub identity"

    def close(self) -> None:
        self.closed = True


def _store_with(credential, tmp_path: Path, seen: list[httpx.Request], **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404 if request.method == "HEAD" else 201)

    store = AzureBlobStore(CFG, credential=credential, **kwargs)
    store._http = httpx.Client(
        transport=httpx.MockTransport(handler), auth=store._http.auth
    )
    return store


def test_blob_requests_carry_a_bearer_token(tmp_path: Path) -> None:
    credential = _StubCredential()
    seen: list[httpx.Request] = []
    store = _store_with(credential, tmp_path, seen)

    src = tmp_path / "b.bin"
    src.write_bytes(b"hello")
    assert store.write(src, SHA1) is True

    assert [r.headers["Authorization"] for r in seen] == ["Bearer tok-1", "Bearer tok-2"]
    # Bearer auth needs a service version recent enough to accept it.
    assert all(r.headers["x-ms-version"] >= "2017-11-09" for r in seen)

    store.close()
    assert credential.closed is True


def test_account_key_wins_over_a_credential(tmp_path: Path) -> None:
    """An explicit key pins behaviour, and must not be silently ignored."""
    credential = _StubCredential()
    seen: list[httpx.Request] = []
    store = _store_with(
        credential, tmp_path, seen, account_key="c2VjcmV0MDAwMDAwMDAwMDAwMDAwMA=="
    )

    src = tmp_path / "b.bin"
    src.write_bytes(b"hello")
    store.write(src, SHA1)

    assert all(r.headers["Authorization"].startswith("SharedKey ") for r in seen)
    assert credential.tokens == 0


def test_store_needs_some_credential() -> None:
    with pytest.raises(ValueError, match="account key or a credential"):
        AzureBlobStore(CFG)

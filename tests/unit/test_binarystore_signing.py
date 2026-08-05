"""Request signing for the object-storage backends.

Both schemes are implemented by hand, so these tests pin them against
independently computed references rather than against themselves. The S3 case
uses the canonical request published in the AWS SigV4 documentation; the Azure
case recomputes the SharedKey string-to-sign from the protocol description.

Both implementations were also exercised against real servers during
development (MinIO for S3, Azurite for Azure), which is what establishes they
are correct end to end; these tests are the regression guard.
"""

from __future__ import annotations

import base64
import datetime as real_dt
import hashlib
import hmac

import httpx
import pytest

from artifactory_airlift.binarystore import azure as azure_mod
from artifactory_airlift.binarystore import s3 as s3_mod

ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
FROZEN = real_dt.datetime(2013, 5, 24, 0, 0, 0, tzinfo=real_dt.timezone.utc)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin both modules' clocks so signatures are deterministic."""

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            return FROZEN

    class _FakeDT:
        timezone = real_dt.timezone
        datetime = _FakeDateTime

    monkeypatch.setattr(s3_mod, "dt", _FakeDT)
    monkeypatch.setattr(azure_mod, "dt", _FakeDT)


def _aws_reference_signature() -> str:
    """Sign the canonical request exactly as printed in the AWS documentation.

    Independent of our implementation: the canonical request string is
    transcribed from AWS's "GET Bucket Lifecycle" example, and only the HMAC
    chain below is computed here.
    """
    canonical = "\n".join(
        [
            "GET",
            "/",
            "lifecycle=",
            "host:examplebucket.s3.amazonaws.com",
            f"x-amz-content-sha256:{EMPTY_SHA256}",
            "x-amz-date:20130524T000000Z",
            "",
            "host;x-amz-content-sha256;x-amz-date",
            EMPTY_SHA256,
        ]
    )
    scope = "20130524/us-east-1/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            "20130524T000000Z",
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    key = f"AWS4{SECRET_KEY}".encode()
    for part in ("20130524", "us-east-1", "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def test_sigv4_matches_aws_canonical_request(frozen_clock) -> None:
    auth = s3_mod.SigV4Auth(ACCESS_KEY, SECRET_KEY, region="us-east-1")
    request = httpx.Request("GET", "https://examplebucket.s3.amazonaws.com/?lifecycle")
    signed = next(auth.auth_flow(request))

    header = signed.headers["authorization"]
    assert header.startswith("AWS4-HMAC-SHA256 ")
    assert f"Credential={ACCESS_KEY}/20130524/us-east-1/s3/aws4_request" in header
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in header
    assert f"Signature={_aws_reference_signature()}" in header


def test_sigv4_sets_required_headers(frozen_clock) -> None:
    auth = s3_mod.SigV4Auth(ACCESS_KEY, SECRET_KEY, region="us-east-1")
    request = httpx.Request("GET", "https://examplebucket.s3.amazonaws.com/k")
    signed = next(auth.auth_flow(request))
    assert signed.headers["x-amz-date"] == "20130524T000000Z"
    assert signed.headers["x-amz-content-sha256"] == EMPTY_SHA256
    assert signed.headers["host"] == "examplebucket.s3.amazonaws.com"


def test_sigv4_uploads_declare_unsigned_payload(frozen_clock) -> None:
    """Hashing a multi-gigabyte body purely to sign it would be pure waste."""
    auth = s3_mod.SigV4Auth(ACCESS_KEY, SECRET_KEY, region="us-east-1")
    request = httpx.Request(
        "PUT", "https://examplebucket.s3.amazonaws.com/k", content=b"payload"
    )
    signed = next(auth.auth_flow(request))
    assert signed.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"


def test_sigv4_resigns_each_attempt(frozen_clock, monkeypatch) -> None:
    """A retried request must be re-signed, not replayed with a stale signature."""
    auth = s3_mod.SigV4Auth(ACCESS_KEY, SECRET_KEY, region="us-east-1")
    request = httpx.Request("GET", "https://examplebucket.s3.amazonaws.com/k")
    first = next(auth.auth_flow(request)).headers["x-amz-date"]

    later = FROZEN + real_dt.timedelta(minutes=30)

    class _Later:
        timezone = real_dt.timezone

        class datetime:
            @staticmethod
            def now(tz=None):
                return later

    monkeypatch.setattr(s3_mod, "dt", _Later)
    second = next(auth.auth_flow(request)).headers["x-amz-date"]
    assert first != second


def test_sharedkey_signature(frozen_clock) -> None:
    account = "devstoreaccount1"
    account_key = base64.b64encode(b"0" * 32).decode()
    auth = azure_mod.SharedKeyAuth(account, account_key)
    request = httpx.Request(
        "GET", f"https://{account}.blob.core.windows.net/container/a/b"
    )
    signed = next(auth.auth_flow(request))

    date = signed.headers["x-ms-date"]
    version = signed.headers["x-ms-version"]
    # Recomputed from the SharedKey definition: verb, the eleven standard
    # header slots, canonicalised x-ms-* headers, then the resource.
    string_to_sign = (
        "GET\n"
        + "\n" * 11
        + f"x-ms-date:{date}\nx-ms-version:{version}\n"
        + f"/{account}/container/a/b"
    )
    expected = base64.b64encode(
        hmac.new(
            base64.b64decode(account_key), string_to_sign.encode(), hashlib.sha256
        ).digest()
    ).decode()
    assert signed.headers["authorization"] == f"SharedKey {account}:{expected}"


def test_sharedkey_signs_query_parameters(frozen_clock) -> None:
    """Block uploads carry comp/blockid in the query, which must be signed."""
    account = "devstoreaccount1"
    account_key = base64.b64encode(b"0" * 32).decode()
    auth = azure_mod.SharedKeyAuth(account, account_key)
    request = httpx.Request(
        "PUT",
        f"https://{account}.blob.core.windows.net/c/k",
        params={"comp": "block", "blockid": "MDAwMDAwMDE="},
    )
    signed = next(auth.auth_flow(request))

    date = signed.headers["x-ms-date"]
    version = signed.headers["x-ms-version"]
    string_to_sign = (
        "PUT\n"
        + "\n" * 11
        + f"x-ms-date:{date}\nx-ms-version:{version}\n"
        + f"/{account}/c/k\nblockid:MDAwMDAwMDE=\ncomp:block"
    )
    expected = base64.b64encode(
        hmac.new(
            base64.b64decode(account_key), string_to_sign.encode(), hashlib.sha256
        ).digest()
    ).decode()
    assert signed.headers["authorization"] == f"SharedKey {account}:{expected}"


def test_sharedkey_rejects_bad_key() -> None:
    with pytest.raises(ValueError, match="base64"):
        azure_mod.SharedKeyAuth("acct", "not valid base64!!")

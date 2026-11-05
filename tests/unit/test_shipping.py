"""Shipping finished archives to an Azure Blob container.

Request shapes are asserted with httpx's MockTransport, the same way the
binarystore backends are tested; nothing here reaches a network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from artifactory_airlift import shipping
from artifactory_airlift.config import Settings
from artifactory_airlift.shipping import AzureArchiveTarget, Shipper


class _FakeCredential:
    closed = False

    def token(self) -> str:
        return "tok"

    def describe(self) -> str:
        return "federated identity (test)"

    def close(self) -> None:
        self.closed = True


def _settings(tmp_path: Path, *, keep_hours: int = 72, **azure) -> Settings:
    s = Settings(
        mode="sender",
        state_dir=tmp_path / "state",
        spool_dir=tmp_path / "spool",
        shipping={
            "enabled": True,
            "method": "azure",
            "keep_hours": keep_hours,
            "azure": {
                "account": "exportacct",
                "container": "outbound",
                "prefix": "airlift/site-a",
            }
            | azure,
        },
    )
    s.spool_dir.mkdir(parents=True, exist_ok=True)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


def _target(handler, **kw) -> AzureArchiveTarget:
    t = AzureArchiveTarget(
        endpoint_url="https://exportacct.blob.core.windows.net",
        container="outbound",
        prefix="airlift/site-a",
        credential=_FakeCredential(),
        source_instance="site-a",
        **kw,
    )
    t._http = httpx.Client(transport=httpx.MockTransport(handler), auth=t._http.auth)
    return t


def _archive(spool: Path, name: str, data: bytes) -> Path:
    p = spool / name
    p.write_bytes(data)
    return p


def _b64md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def _ledger(settings: Settings) -> list[dict]:
    path = shipping.ledger_path(settings)
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    for fn in (
        AzureArchiveTarget._head,
        AzureArchiveTarget._put_single,
        AzureArchiveTarget._put_block,
        AzureArchiveTarget._commit,
    ):
        monkeypatch.setattr(fn.retry, "wait", wait_none())


def test_single_put_shape_then_moved_out_of_spool(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    data = b"zstd-archive-bytes"
    path = _archive(settings.spool_dir, "1778636042-ab12.tar.zst", data)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(201)

    shipped = Shipper(settings, target=_target(handler)).run_once()

    assert shipped == 1
    head, put = seen
    assert put.method == "PUT"
    assert str(put.url) == (
        "https://exportacct.blob.core.windows.net/outbound/"
        "airlift/site-a/1778636042-ab12.tar.zst"
    )
    assert put.headers["authorization"] == "Bearer tok"
    assert put.headers["x-ms-blob-type"] == "BlockBlob"
    assert put.headers["content-md5"] == _b64md5(data)
    assert put.headers["x-ms-meta-sha256"] == hashlib.sha256(data).hexdigest()
    assert put.headers["x-ms-meta-source_instance"] == "site-a"
    assert put.content == data

    # Out of the spool root, which is what reopens the sender's gate.
    assert not path.exists()
    assert shipping.pending(settings) == []
    assert (shipping.shipped_dir(settings) / path.name).read_bytes() == data
    (row,) = _ledger(settings)
    assert row["status"] == "ok" and row["archive"] == path.name
    assert row["bytes"] == len(data)


def test_large_archive_goes_up_as_verified_blocks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    data = bytes(range(256)) * 10  # 2560 bytes
    _archive(settings.spool_dir, "c.tar.zst", data)
    puts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(404)
        puts.append(request)
        return httpx.Response(201)

    target = _target(handler, single_put_max=1000, block_bytes=1000)
    assert Shipper(settings, target=target).run_once() == 1

    blocks, commit = puts[:-1], puts[-1]
    assert [len(b.content) for b in blocks] == [1000, 1000, 560]
    for i, b in enumerate(blocks):
        assert b.url.params["comp"] == "block"
        assert b.headers["content-md5"] == _b64md5(data[i * 1000 : (i + 1) * 1000])
    assert b"".join(b.content for b in blocks) == data
    assert commit.url.params["comp"] == "blocklist"
    assert commit.headers["x-ms-blob-content-md5"] == _b64md5(data)
    assert commit.headers["x-ms-meta-sha256"] == hashlib.sha256(data).hexdigest()
    assert commit.content.count(b"<Latest>") == 3


def test_identical_blob_already_there_is_not_reuploaded(tmp_path: Path) -> None:
    # The crash window: uploaded, then killed before the local move.
    settings = _settings(tmp_path)
    data = b"already-up"
    path = _archive(settings.spool_dir, "a.tar.zst", data)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            headers={"content-length": str(len(data)), "content-md5": _b64md5(data)},
        )

    assert Shipper(settings, target=_target(handler)).run_once() == 1
    assert methods == ["HEAD"]
    assert not path.exists()
    assert _ledger(settings)[0]["status"] == "already-present"


def test_differing_blob_of_same_name_is_overwritten(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _archive(settings.spool_dir, "a.tar.zst", b"new-content")
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(
                200, headers={"content-length": "3", "content-md5": _b64md5(b"old")}
            )
        return httpx.Response(201)

    assert Shipper(settings, target=_target(handler)).run_once() == 1
    assert methods == ["HEAD", "PUT"]


def test_failure_keeps_archive_and_stops_so_chunks_stay_ordered(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = _archive(settings.spool_dir, "100-x-c001.tar.zst", b"one")
    second = _archive(settings.spool_dir, "100-x-c002.tar.zst", b"two")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(404)
        urls.append(str(request.url))
        return httpx.Response(403, text="AuthorizationPermissionMismatch")

    assert Shipper(settings, target=_target(handler)).run_once() == 0
    # Retried, but only ever the first chunk.
    assert urls and all(u.endswith("c001.tar.zst") for u in urls)
    assert first.exists() and second.exists()
    (row,) = _ledger(settings)
    assert row["status"] == "failed" and "403" in row["error"]


def test_keep_hours_zero_deletes_local_copy(tmp_path: Path) -> None:
    settings = _settings(tmp_path, keep_hours=0)
    path = _archive(settings.spool_dir, "a.tar.zst", b"x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.method == "HEAD" else 201)

    Shipper(settings, target=_target(handler)).run_once()
    assert not path.exists()
    assert not (shipping.shipped_dir(settings) / "a.tar.zst").exists()


def test_old_shipped_copies_are_pruned(tmp_path: Path) -> None:
    settings = _settings(tmp_path, keep_hours=1)
    kept = shipping.shipped_dir(settings)
    kept.mkdir(parents=True)
    old = kept / "old.tar.zst"
    fresh = kept / "fresh.tar.zst"
    old.write_bytes(b"o")
    fresh.write_bytes(b"f")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))

    Shipper(settings, target=_target(lambda r: httpx.Response(500))).run_once()
    assert not old.exists() and fresh.exists()


def test_retention_counts_from_shipping_not_from_build(tmp_path: Path) -> None:
    settings = _settings(tmp_path, keep_hours=1)
    path = _archive(settings.spool_dir, "a.tar.zst", b"x")
    built_long_ago = time.time() - 7200
    os.utime(path, (built_long_ago, built_long_ago))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.method == "HEAD" else 201)

    shipper = Shipper(settings, target=_target(handler))
    shipper.run_once()
    shipper.run_once()  # prunes first; must not take the just-shipped copy
    assert (shipping.shipped_dir(settings) / "a.tar.zst").exists()


def test_unavailable_target_waits_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    path = _archive(settings.spool_dir, "a.tar.zst", b"x")

    def boom(_settings):
        raise RuntimeError("no Azure identity found in this container")

    monkeypatch.setattr(shipping, "build_target", boom)
    shipper = Shipper(settings)
    assert shipper.run_once() == 0
    assert shipper.run_once() == 0
    assert path.exists()


def test_nothing_pending_does_not_build_a_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        shipping, "build_target", lambda s: pytest.fail("built a target for nothing")
    )
    assert Shipper(settings).run_once() == 0


def test_partials_and_done_dirs_are_never_shipped(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _archive(settings.spool_dir, "a.tar.zst.partial", b"half")
    (settings.spool_dir / ".staging").mkdir()
    _archive(settings.spool_dir / ".staging", "b.tar.zst", b"x")
    assert shipping.pending(settings) == []


def test_endpoint_defaults_from_account_and_can_be_overridden(tmp_path: Path) -> None:
    assert (
        shipping.endpoint_url(_settings(tmp_path))
        == "https://exportacct.blob.core.windows.net"
    )
    assert (
        shipping.endpoint_url(
            _settings(tmp_path, endpoint="https://pe.example/")
        )
        == "https://pe.example"
    )


def test_enabled_without_container_or_account_is_rejected() -> None:
    with pytest.raises(ValueError, match="shipping.azure.container") as exc:
        Settings(shipping={"enabled": True})
    assert "shipping.azure.account" in str(exc.value)


def test_endpoint_stands_in_for_account() -> None:
    s = Settings(
        shipping={
            "enabled": True,
            "azure": {"container": "c", "endpoint": "https://pe.example"},
        }
    )
    assert s.shipping.enabled


def test_unknown_method_is_rejected() -> None:
    with pytest.raises(ValueError, match="method"):
        Settings(shipping={"enabled": True, "method": "s3"})


def test_shipping_is_off_by_default() -> None:
    assert Settings().shipping.enabled is False


def test_azure_values_alone_do_not_enable_shipping() -> None:
    s = Settings(shipping={"azure": {"account": "a", "container": "c"}})
    assert s.shipping.enabled is False


def test_nested_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRLIFT_SHIPPING__ENABLED", "true")
    monkeypatch.setenv("AIRLIFT_SHIPPING__AZURE__ACCOUNT", "exportacct")
    monkeypatch.setenv("AIRLIFT_SHIPPING__AZURE__CONTAINER", "outbound")
    s = Settings()
    assert s.shipping.enabled and s.shipping.azure.container == "outbound"


def test_env_var_overlays_one_key_of_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artifactory_airlift import config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "shipping:\n  enabled: true\n  azure:\n"
        "    account: fileacct\n    container: filecont\n    prefix: p\n"
    )
    monkeypatch.setenv("AIRLIFT_SHIPPING__AZURE__CONTAINER", "envcont")
    azure = config.load(cfg).shipping.azure
    assert (azure.account, azure.container, azure.prefix) == ("fileacct", "envcont", "p")


# -- doctor -------------------------------------------------------------------


def _doctor_checks(settings: Settings) -> dict[str, dict]:
    from artifactory_airlift.cli import views

    checks: dict[str, dict] = {}

    def add(name, ok, detail, *, fatal=True):
        checks[name] = {"ok": ok, "detail": detail, "fatal": fatal}

    views._doctor_ship_target(settings, add)
    return checks


def test_doctor_write_check_never_commits_a_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200 if request.method == "GET" else 201)

    monkeypatch.setattr(shipping, "build_target", lambda s: _target(handler))
    checks = _doctor_checks(_settings(tmp_path))

    assert [c["ok"] for c in checks.values()] == [True, True, True]
    get, put = seen
    assert get.url.params["restype"] == "container"
    assert put.url.params["comp"] == "block"  # staged only, never blocklist
    assert "x-ms-blob-type" not in put.headers


def test_doctor_reports_missing_role_with_a_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200)
        return httpx.Response(
            403, headers={"x-ms-error-code": "AuthorizationPermissionMismatch"}
        )

    monkeypatch.setattr(shipping, "build_target", lambda s: _target(handler))
    checks = _doctor_checks(_settings(tmp_path))
    assert checks["ship write"]["ok"] is False
    assert "Storage Blob Data Contributor" in checks["ship write"]["detail"]


def test_doctor_stops_at_missing_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"x-ms-error-code": "ContainerNotFound"})

    monkeypatch.setattr(shipping, "build_target", lambda s: _target(handler))
    checks = _doctor_checks(_settings(tmp_path))
    assert checks["ship container"]["ok"] is False
    assert "ship write" not in checks


def test_doctor_reports_missing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_s):
        raise RuntimeError("no Azure identity found in this container")

    monkeypatch.setattr(shipping, "build_target", boom)
    checks = _doctor_checks(_settings(tmp_path))
    assert checks["ship target"]["ok"] is False and len(checks) == 1


def test_doctor_skips_ship_checks_when_shipping_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from artifactory_airlift.cli import views

    monkeypatch.setattr(
        shipping, "build_target", lambda s: pytest.fail("checked a disabled target")
    )
    settings = Settings(state_dir=tmp_path / "state", spool_dir=tmp_path / "spool")
    for d in (settings.state_dir, settings.spool_dir):
        d.mkdir()
    names = [c["name"] for c in views.doctor(settings)["checks"]]
    assert not any(n.startswith("ship") for n in names)


def test_config_view_reports_nested_keys_with_their_own_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from artifactory_airlift.cli import views

    monkeypatch.setenv("AIRLIFT_SHIPPING__AZURE__CONTAINER", "envcont")
    file_values = {"shipping": {"enabled": True, "azure": {"account": "a", "container": "c"}}}
    settings = Settings(**file_values)
    rows = {f["name"]: f for f in views.effective_config(settings, file_values)["fields"]}

    assert "shipping" not in rows
    assert rows["shipping.azure.container"]["origin"] == "env"
    assert rows["shipping.azure.container"]["masked_file_value"] == "c"
    assert rows["shipping.azure.container"]["env_var"] == "AIRLIFT_SHIPPING__AZURE__CONTAINER"
    assert rows["shipping.azure.account"]["origin"] == "file"
    assert rows["shipping.keep_hours"]["origin"] == "default"
    assert rows["mode"]["env_var"] == "AIRLIFT_MODE"

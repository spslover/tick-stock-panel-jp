"""Security & reliability tests for M1.2 (SSRF, secrets, DuckDB SQL, cache, scheduler)."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import duckdb
import httpx
import polars as pl
import pytest
from fastapi import HTTPException

from app import secrets_store
from app.api import ext_data as ext_api
from app.api import settings as settings_api
from app.plugins.tdxquant import client as tdx_client
from app.services import ext_pull, preferences, safe_http, webhook_adapter
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig

# ---------------------------------------------------------------------------
# Helpers: local test HTTP server (used with explicit allowlist + DNS mock)
# ---------------------------------------------------------------------------

def _start_http_server(handler_cls: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, int, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


# ---------------------------------------------------------------------------
# 1. safe_http: URL validation, IP blocking, DNS pinning, redirects, redaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "",
    "   ",
    "file:///etc/passwd",
    "ftp://example.com/data",
    "gopher://example.com/1",
    "javascript:alert(1)",
    "http://user:pass@example.com/hook",
    "http://:pass@example.com/hook",
    "https://user@example.com/hook",
    "https://:8080/hook",
    "https://example.com/\nfoo",
    "https://example.com/\x00foo",
    "http://localhost/admin",
    "http://sub.localhost./admin",
    "http://127.0.0.1/admin",
    "http://127.1/admin",
    "http://2130706433/admin",
    "http://0x7f000001/admin",
    "http://[::1]/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/internal",
    "http://172.16.0.1/internal",
    "http://192.168.1.1/internal",
    "http://100.64.0.1/cgnat",
    "http://[fc00::1]/ula",
    "http://[fd00:ec2::254]/latest/meta-data/",
    "http://[fe80::1]/link-local",
    "http://0.0.0.0/",
    "http://[::]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://[2002:7f00:0001::1]/",
])
def test_safe_http_validate_url_blocks_unsafe_targets(monkeypatch: pytest.MonkeyPatch, bad_url: str) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SAFE_HTTP_ALLOW_HOSTS", raising=False)
    with pytest.raises(safe_http.UnsafeURLError):
        safe_http.validate_url(bad_url)


def test_safe_http_validate_url_allows_public_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SAFE_HTTP_ALLOW_HOSTS", raising=False)
    parsed = safe_http.validate_url("https://106.15.238.72:50443/health")
    assert parsed.hostname == "106.15.238.72"
    assert parsed.port == 50443
    assert safe_http.is_safe_url("https://api.example.com/v1") is True


def test_safe_http_env_opt_in_and_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.setenv("SAFE_HTTP_ALLOW_HOSTS", "10.20.30.40,192.168.50.0/24,ollama.local")

    assert safe_http.is_safe_url("http://10.20.30.40:8080/hook") is True
    assert safe_http.is_safe_url("http://192.168.50.12:8080/hook") is True
    assert safe_http.is_safe_url("http://192.168.51.12:8080/hook") is False
    assert safe_http.is_safe_url("http://127.0.0.1:8080/hook") is False

    monkeypatch.setenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", "true")
    assert safe_http.is_safe_url("http://127.0.0.1:8080/hook") is True
    assert safe_http.is_safe_url("http://169.254.169.254/latest") is True


def test_resolve_and_pin_host_fails_closed_on_mixed_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SAFE_HTTP_ALLOW_HOSTS", raising=False)

    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ],
    )
    with pytest.raises(safe_http.UnsafeURLError):
        safe_http.resolve_and_pin_host("dual.example.com", 443)

    monkeypatch.setattr(safe_http.socket, "getaddrinfo", lambda host, port, **kw: [])
    with pytest.raises(safe_http.UnsafeURLError):
        safe_http.resolve_and_pin_host("empty.example.com", 443)


def test_safe_http_dns_rebinding_blocked_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SAFE_HTTP_ALLOW_HOSTS", raising=False)

    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ],
    )
    with pytest.raises(safe_http.UnsafeURLError):
        safe_http.sync_request("GET", "http://evil-rebind.example.com/secret")


def test_safe_http_dns_pinning_and_host_header_with_real_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_hosts: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen_hosts.append(self.headers.get("Host", ""))
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server, port, thread = _start_http_server(_Handler)
    try:
        dns_calls = 0
        real_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host: str, p: int, *args: object, **kw: object) -> list:
            nonlocal dns_calls
            if host == "127.0.0.1":
                return real_getaddrinfo(host, p, *args, **kw)
            dns_calls += 1
            if dns_calls > 1:
                raise AssertionError("DNS lookup executed more than once per connection!")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]

        monkeypatch.setenv("SAFE_HTTP_ALLOW_HOSTS", "pinned.example.test")
        monkeypatch.setattr(safe_http.socket, "getaddrinfo", fake_getaddrinfo)

        resp = safe_http.sync_request("GET", f"http://pinned.example.test:{port}/ok")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert seen_hosts == [f"pinned.example.test:{port}"]
        assert dns_calls == 1

        # Reset counter and verify async transport pins IP with single lookup too
        dns_calls = 0
        resp_async = asyncio.run(
            safe_http.async_request("GET", f"http://pinned.example.test:{port}/ok")
        )
        assert resp_async.status_code == 200
        assert dns_calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_safe_http_blocks_unsafe_redirect_and_strips_cross_origin_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_headers: list[dict[str, str]] = []
    redirect_target = {"url": "http://169.254.169.254/latest/meta-data/"}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 0:
                self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", redirect_target["url"])
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_GET(self) -> None:
            if self.path == "/hop":
                self.send_response(302)
                self.send_header("Location", redirect_target["url"])
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            captured_headers.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    server, port, thread = _start_http_server(_Handler)
    try:
        real_getaddrinfo = socket.getaddrinfo
        monkeypatch.setenv("SAFE_HTTP_ALLOW_HOSTS", "origin.example.test,other.example.test")
        monkeypatch.setattr(
            safe_http.socket,
            "getaddrinfo",
            lambda host, p, *args, **kw: (
                real_getaddrinfo(host, p, *args, **kw)
                if host == "127.0.0.1"
                else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]
            ),
        )

        # 1) Redirect to metadata IP must fail closed
        with pytest.raises(safe_http.UnsafeURLError):
            asyncio.run(
                safe_http.async_request(
                    "GET",
                    f"http://origin.example.test:{port}/hop",
                    max_redirects=3,
                )
            )

        # 2) Redirect to file:// must fail closed
        redirect_target["url"] = "file:///etc/passwd"
        with pytest.raises(safe_http.UnsafeURLError):
            safe_http.sync_request("GET", f"http://origin.example.test:{port}/hop", max_redirects=3)

        # 3) Cross-origin 302 POST -> GET downgrade + Authorization/Cookie/X-Api-Key stripped
        redirect_target["url"] = f"http://other.example.test:{port}/final"
        resp = safe_http.sync_request(
            "POST",
            f"http://origin.example.test:{port}/submit",
            max_redirects=3,
            content=b'{"x":1}',
            headers={
                "Authorization": "Bearer topsecret",
                "Cookie": "sid=abc",
                "X-Api-Key": "k123",
                "X-Custom": "keep",
            },
        )
        assert resp.status_code == 200
        assert len(captured_headers) == 1
        assert "authorization" not in captured_headers[0]
        assert "cookie" not in captured_headers[0]
        assert "x-api-key" not in captured_headers[0]
        assert captured_headers[0].get("x-custom") == "keep"

        # 4) Exceeding max_redirects raises UnsafeURLError
        redirect_target["url"] = f"http://origin.example.test:{port}/hop"
        with pytest.raises(safe_http.UnsafeURLError):
            safe_http.sync_request("GET", f"http://origin.example.test:{port}/hop", max_redirects=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_safe_http_disables_proxy_env_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    sync_client = safe_http.create_safe_sync_client()
    async_client = safe_http.create_safe_async_client()
    try:
        assert sync_client.trust_env is False
        assert sync_client.follow_redirects is False
        assert async_client.trust_env is False
        assert async_client.follow_redirects is False
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())


def test_redact_url_secrets_masks_tokens_userinfo_and_custom_params() -> None:
    msg = (
        "Client error '404 Not Found' for url "
        "'https://user:pass@api.example.com/v1?date=2026-01-01&token=tok_secret&my_param=abc%20def'"
    )
    redacted = safe_http.redact_url_secrets(
        msg,
        extra_params=["my_param"],
        secret_values=["tok_secret", "abc def"],
    )
    assert "user:pass" not in redacted
    assert "tok_secret" not in redacted
    assert "abc%20def" not in redacted
    assert "date=2026-01-01" in redacted
    assert "token=%2A%2A%2A" in redacted or "token=***" in redacted


# ---------------------------------------------------------------------------
# 2. ext_data.py: SSRF detect_url, DuckDB SQL, bounded/locked cache
# ---------------------------------------------------------------------------

def test_detect_url_blocks_ssrf_and_redacts_query_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for target in ("http://127.0.0.1:8000/x", "http://169.254.169.254/latest", "file:///etc/passwd"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ext_api.detect_url(ext_api.DetectUrlReq(url=target)))
        assert exc.value.status_code == 400

    async def boom_request(*args, **kwargs):
        raise httpx.ConnectError(
            "failed for https://api.example.com/data?token=topsecret123&page=1"
        )

    monkeypatch.setattr(safe_http, "async_request", boom_request)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            ext_api.detect_url(
                ext_api.DetectUrlReq(url="https://api.example.com/data?token=topsecret123&page=1")
            )
        )
    assert exc.value.status_code == 400
    assert "topsecret123" not in exc.value.detail


def test_describe_parquet_columns_parameterized_and_rejects_injection(tmp_path: Path) -> None:
    good = tmp_path / "good.parquet"
    pl.DataFrame({"symbol": ["000001.SZ"], "score": [1.5]}).write_parquet(good)
    cols = ext_api._describe_parquet_columns(str(good))
    assert ("symbol", "VARCHAR") in cols
    assert any(name == "score" for name, _ in cols)

    evil = str(tmp_path / "x.parquet'), union_by_name=true); CREATE TABLE pwned(x INT); --")
    with pytest.raises(duckdb.Error):
        ext_api._describe_parquet_columns(evil)


def test_refresh_views_validates_ident_escapes_quotes_and_supports_db_none(tmp_path: Path) -> None:
    # 1) Legacy SimpleNamespace(db=None) still works
    req_none = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path, db=None))
    )))
    ext_api._refresh_views(req_none)

    # 2) Valid config id + apostrophe in parent path + malicious id rejected
    quoted_root = tmp_path / "user's_data"
    cfg_dir = quoted_root / "ext_data" / "good_tbl"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"id": "good_tbl", "mode": "snapshot"}), encoding="utf-8")
    pl.DataFrame({"symbol": ["000001.SZ"], "v": [1.0]}).write_parquet(cfg_dir / "part.parquet")

    # Malicious config.json id that doesn't match dir or valid ident
    bad_dir = quoted_root / "ext_data" / "bad_dir"
    bad_dir.mkdir(parents=True)
    (bad_dir / "config.json").write_text(
        json.dumps({"id": "bad; DROP VIEW x; --", "mode": "snapshot"}),
        encoding="utf-8",
    )
    pl.DataFrame({"symbol": ["000001.SZ"]}).write_parquet(bad_dir / "part.parquet")

    calls: list[tuple[str, str]] = []

    class _Repo:
        def __init__(self) -> None:
            self.store = SimpleNamespace(data_dir=quoted_root, db=None)

        def refresh_view(self, name: str, glob: str) -> bool:
            calls.append((name, glob))
            return True

        def clear_cache(self) -> None:
            pass

    req_repo = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=_Repo())))
    ext_api._refresh_views(req_repo)
    assert len(calls) == 1
    assert calls[0][0] == "ext_good_tbl"
    assert "user''s_data" in calls[0][1]


def test_dimension_intraday_cache_bounded_lru_ttl_data_dir_and_invalidation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ext_api._invalidate_dimension_intraday_cache()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    for d in (dir_a, dir_b):
        store = ExtConfigStore(d)
        store.upsert(ExtConfig(
            id="concept",
            label="概念",
            mode="snapshot",
            fields=[ExtField("symbol"), ExtField("tag")],
        ))

    compute_calls: list[tuple[str, str]] = []

    def fake_compute(config, data_dir: Path, field: str, value: str, snapshot_date: str | None) -> dict:
        compute_calls.append((str(data_dir), value))
        return {"status": "ok", "dir": str(data_dir), "value": value, "points": []}

    monkeypatch.setattr(ext_api, "_dimension_intraday_compute", fake_compute)

    req_a = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=dir_a, db=None))
    )))
    req_b = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=dir_b, db=None))
    )))

    # Isolated by data_dir
    res_a = ext_api.dimension_intraday(req_a, "concept", field="tag", value="AI", snapshot_date=None)
    res_b = ext_api.dimension_intraday(req_b, "concept", field="tag", value="AI", snapshot_date=None)
    assert res_a["dir"] == str(dir_a)
    assert res_b["dir"] == str(dir_b)

    # Bounded LRU cap
    for i in range(ext_api._DIMENSION_INTRADAY_CACHE_MAX + 40):
        ext_api.dimension_intraday(req_a, "concept", field="tag", value=f"V{i}", snapshot_date=None)
    assert len(ext_api._DIMENSION_INTRADAY_CACHE) <= ext_api._DIMENSION_INTRADAY_CACHE_MAX

    # Thread-safe concurrent calls
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(ext_api.dimension_intraday, req_a, "concept", "tag", f"C{i % 5}", None)
            for i in range(40)
        ]
        for fut in futures:
            assert fut.result()["status"] == "ok"

    # Refresh dir_b entry before invalidating dir_a so dir_b remains cached
    ext_api.dimension_intraday(req_b, "concept", field="tag", value="AI", snapshot_date=None)

    # Invalidation on config update clears dir_a only
    ext_api.update_config(req_a, "concept", ext_api.UpdateExtReq(label="新概念"))
    assert all(k[0] != str(dir_a) for k in ext_api._DIMENSION_INTRADAY_CACHE)
    assert any(k[0] == str(dir_b) for k in ext_api._DIMENSION_INTRADAY_CACHE)

    # TTL expiry forces recomputation
    fake_clock = {"now": 1000.0}
    monkeypatch.setattr(ext_api.time, "monotonic", lambda: fake_clock["now"])
    calls_before = len(compute_calls)
    ext_api.dimension_intraday(req_a, "concept", field="tag", value="TTL", snapshot_date=None)
    assert len(compute_calls) == calls_before + 1
    ext_api.dimension_intraday(req_a, "concept", field="tag", value="TTL", snapshot_date=None)
    assert len(compute_calls) == calls_before + 1
    fake_clock["now"] += ext_api._DIMENSION_INTRADAY_CACHE_TTL_S + 1.0
    ext_api.dimension_intraday(req_a, "concept", field="tag", value="TTL", snapshot_date=None)
    assert len(compute_calls) == calls_before + 2
    ext_api._invalidate_dimension_intraday_cache()


# ---------------------------------------------------------------------------
# 3. settings.py: Non-destructive TickFlow probe, SSRF, M-05 fixes
# ---------------------------------------------------------------------------

def test_save_tickflow_key_restores_previous_key_and_caps_on_invalid_or_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.config import settings as app_settings
    from app.tickflow import policy

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    secrets_store._cache = None
    secrets_store._cache_sig = None

    secrets_store.save({
        "tickflow_api_key": "tf-valid-old",
        "tickflow_base_url": "https://custom.tickflow.example",
    })
    cap_path = tmp_path / policy._CAPSET_CACHE_FILE
    cap_payload = {
        "schema_version": policy._CACHE_SCHEMA_VERSION,
        "label": "Pro",
        "capabilities": {"kline.daily.by_symbol": {}, "adj_factor": {}},
        "probe_log": [],
        "missing_caps": [],
        "extras_caps": [],
        "invalid_key": False,
    }
    cap_path.write_text(json.dumps(cap_payload), encoding="utf-8")
    old_cap_bytes = cap_path.read_bytes()
    old_caps_obj = SimpleNamespace(all=lambda: {"kline.daily.by_symbol": object()})

    monkeypatch.setattr(settings_api.tf_client, "reset_clients", lambda: None)

    def fake_detect_invalid(force: bool = False):
        # Verify capabilities.json was isolated during probe so prev_was_paid cannot leak
        assert not cap_path.exists()
        cap_path.write_text(json.dumps({
            "schema_version": policy._CACHE_SCHEMA_VERSION,
            "label": "None",
            "capabilities": {},
            "invalid_key": True,
        }), encoding="utf-8")
        return SimpleNamespace(all=lambda: {})

    monkeypatch.setattr(settings_api, "detect_capabilities", fake_detect_invalid)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(capabilities=old_caps_obj)))

    res = settings_api.save_tickflow_key(settings_api.TickflowKeyIn(api_key="tf-bad"), req)
    assert res["ok"] is False
    assert secrets_store.load()["tickflow_api_key"] == "tf-valid-old"
    assert secrets_store.load()["tickflow_base_url"] == "https://custom.tickflow.example"
    assert cap_path.read_bytes() == old_cap_bytes
    assert req.app.state.capabilities is old_caps_obj

    # Also restore snapshot when detect_capabilities raises an exception
    def boom_detect(force: bool = False):
        raise RuntimeError("network timeout")

    monkeypatch.setattr(settings_api, "detect_capabilities", boom_detect)
    res2 = settings_api.save_tickflow_key(settings_api.TickflowKeyIn(api_key="tf-boom"), req)
    assert res2["ok"] is False
    assert secrets_store.load()["tickflow_api_key"] == "tf-valid-old"
    assert cap_path.read_bytes() == old_cap_bytes


def test_settings_ai_and_endpoint_ssrf_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("AI_ALLOW_LOCAL", raising=False)

    res = asyncio.run(settings_api.test_ai_settings(settings_api.AiTestIn(
        provider="openai_compat",
        base_url="http://169.254.169.254/v1",
        api_key="sk-secret-1234",
        model="gpt-4",
    )))
    assert res["ok"] is False
    assert res["error_type"] == "UnsafeURLError"

    with pytest.raises(HTTPException) as exc:
        settings_api.save_ai_settings(settings_api.AiSettingsIn(
            provider="openai_compat",
            base_url="http://127.0.0.1:11434/v1",
        ))
    assert exc.value.status_code == 400

    # AI_ALLOW_LOCAL=1 permits local AI endpoints
    monkeypatch.setenv("AI_ALLOW_LOCAL", "1")
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda updates: updates)
    saved = settings_api.save_ai_settings(settings_api.AiSettingsIn(
        provider="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
    ))
    assert saved["ok"] is True

    # Endpoint SSRF checks
    with pytest.raises(HTTPException) as ep_exc:
        asyncio.run(settings_api.test_endpoint(settings_api.TestEndpointIn(url="http://169.254.169.254")))
    assert ep_exc.value.status_code == 400

    monkeypatch.setattr(settings_api.tf_client, "current_mode", lambda: "api_key")
    sw = settings_api.switch_endpoint(
        settings_api.SwitchEndpointIn(url="https://127.0.0.1:8443"),
        SimpleNamespace(),
    )
    assert sw["ok"] is False


def test_settings_error_sanitization_and_schedule_return_and_provider_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1) update_instruments_schedule returns sched even when scheduler is None
    monkeypatch.setattr(preferences, "set_instruments_schedule", lambda h, m: {"hour": h, "minute": m})
    req_no_sched = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(scheduler=None)))
    out = settings_api.update_instruments_schedule(
        settings_api.PipelineScheduleIn(hour=8, minute=45),
        req_no_sched,
    )
    assert out == {"hour": 8, "minute": 45}

    # 2) list_sponsor_models does not leak raw exception internals
    async def fail_req(*args, **kwargs):
        raise RuntimeError("secret-internal-host https://api.example/v1?token=leak123")

    monkeypatch.setattr(safe_http, "async_request", fail_req)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(settings_api.list_sponsor_models())
    assert exc.value.status_code == 502
    assert "leak123" not in exc.value.detail
    assert "secret-internal-host" not in exc.value.detail

    # 3) delete_data_source & uninstall_plugin reset all 7 provider preferences
    from app.data_providers import custom as custom_sources

    stored_prefs = {
        "daily_data_provider": "MySrc",
        "adj_factor_provider": "mysrc",
        "realtime_data_provider": "mysrc",
        "minute_data_provider": "mysrc",
        "depth5_data_provider": "mysrc",
        "financial_data_provider": "mysrc",
        "full_minute_data_provider": "mysrc",
    }
    monkeypatch.setattr(custom_sources, "delete_config", lambda name: None)
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [])
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(custom_sources, "errors", lambda: [])
    monkeypatch.setattr(custom_sources, "data_sources_dir", lambda: Path("/tmp"))
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: SimpleNamespace(all=lambda: {}))
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: stored_prefs["daily_data_provider"])
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: stored_prefs["adj_factor_provider"])
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: stored_prefs["realtime_data_provider"])
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: stored_prefs["minute_data_provider"])
    monkeypatch.setattr(preferences, "get_depth5_data_provider", lambda: stored_prefs["depth5_data_provider"])
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: stored_prefs["financial_data_provider"])
    monkeypatch.setattr(preferences, "get_full_minute_data_provider", lambda: stored_prefs["full_minute_data_provider"])
    monkeypatch.setattr(preferences, "save", lambda updates: stored_prefs.update(updates) or stored_prefs)

    req_app = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(capabilities=None)))
    settings_api.delete_data_source("mysrc", req_app)
    assert all(v == "tickflow" for v in stored_prefs.values())

    # 4) uninstall_plugin also resets all 7 provider preferences
    for k in stored_prefs:
        stored_prefs[k] = "fuyao"
    monkeypatch.setattr(custom_sources, "is_builtin", lambda name: True)
    monkeypatch.setattr(custom_sources, "uninstall_plugin", lambda name: (True, "ok"))
    settings_api.uninstall_plugin("FuYao", req_app)
    assert all(v == "tickflow" for v in stored_prefs.values())

    # 5) test_data_source sanitizes unexpected internal exceptions
    class _BrokenProvider:
        def test_dataset(self, ds, syms):
            raise RuntimeError("internal crash at https://api.secret/?token=secret999")

    monkeypatch.setattr(custom_sources, "get_provider", lambda name: _BrokenProvider())
    with pytest.raises(HTTPException) as ds_exc:
        settings_api.test_data_source(settings_api.CustomSourceTestIn(provider="p", dataset="daily"))
    assert ds_exc.value.status_code == 400
    assert "secret999" not in ds_exc.value.detail
    assert "internal crash" not in ds_exc.value.detail


# ---------------------------------------------------------------------------
# 4. ext_pull.py: SSRF, query-param redaction, thread-safe PullScheduler.stop
# ---------------------------------------------------------------------------

def test_ext_pull_blocks_ssrf_and_redacts_query_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # SSRF block in _request_json
    with pytest.raises(safe_http.UnsafeURLError):
        asyncio.run(ext_pull._request_json(PullConfig(url="http://169.254.169.254/meta"), "cfg"))

    # Query-param secret redaction (both custom param & standard token) while keeping 404/429 status
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda cid: "super-secret-query-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if "2026-09-17" in str(request.url):
            return httpx.Response(404, request=request)
        return httpx.Response(500, request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        ext_pull.httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(
        "app.services.dragon_tiger._local_trading_days",
        lambda _: [date(2026, 9, 17), date(2026, 9, 18)],
    )

    async def no_sleep(_):
        return None

    monkeypatch.setattr(ext_pull.asyncio, "sleep", no_sleep)

    cfg = ExtConfig(
        id="secret_tbl",
        label="secret",
        mode="timeseries",
        fields=[ExtField("symbol")],
        pull=PullConfig(
            url="https://api.example.test/v1",
            date_param="date",
            auth={"type": "query", "param": "custom_tok"},
        ),
    )
    result = asyncio.run(ext_pull.backfill_history(cfg, tmp_path, date(2026, 9, 17), date(2026, 9, 18)))
    assert result["empty"] == 1  # 404 preserved
    assert len(result["failed"]) == 1
    assert "super-secret-query-key" not in result["failed"][0]["reason"]

    # PullScheduler._execute_pull redacts last_message & warning log
    store = ExtConfigStore(tmp_path)
    store.upsert(cfg)

    async def fail_fetch(*args, **kwargs):
        raise RuntimeError("error calling https://api.example.test/v1?custom_tok=super-secret-query-key")

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fail_fetch)
    sched = ext_pull.PullScheduler()
    sched._data_dir = tmp_path
    with caplog.at_level(logging.WARNING, logger="app.services.ext_pull"):
        asyncio.run(sched._execute_pull(cfg, store))
    saved = store.get("secret_tbl")
    assert saved is not None and saved.pull is not None
    assert "super-secret-query-key" not in (saved.pull.last_message or "")
    assert "super-secret-query-key" not in caplog.text


def test_pull_scheduler_stop_from_other_thread_and_no_resurrection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    loop_ready = threading.Event()
    stop_done = threading.Event()
    holder: dict[str, asyncio.AbstractEventLoop] = {}

    def run_loop() -> None:
        lp = asyncio.new_event_loop()
        asyncio.set_event_loop(lp)
        holder["loop"] = lp
        loop_ready.set()
        try:
            lp.run_forever()
        finally:
            pending = asyncio.all_tasks(lp)
            for t in pending:
                t.cancel()
            if pending:
                lp.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            lp.close()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    assert loop_ready.wait(timeout=3)
    loop = holder["loop"]

    async def _idle(self, config):
        await asyncio.Event().wait()

    monkeypatch.setattr(ext_pull.PullScheduler, "_run_loop", _idle)
    cfg = ExtConfig(
        id="t1", label="t1", mode="snapshot", fields=[],
        pull=PullConfig(enabled=True, url="https://example.test/x", schedule_minutes=60),
    )
    monkeypatch.setattr(ext_pull.ExtConfigStore, "load_all", lambda self: [cfg])

    sched = ext_pull.PullScheduler()
    sched._running = True
    sched._loop = loop
    sched.refresh(tmp_path)

    # Wait briefly until task is created on loop
    fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop)
    fut.result(timeout=2)
    assert "t1" in sched._tasks

    # Stop from main thread (off-loop)
    sched.stop()
    # Allow call_soon_threadsafe callback to run
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(timeout=2)
    assert sched._tasks == {}
    assert sched._running is False

    # Calling refresh after stop must not resurrect tasks
    sched.refresh(tmp_path)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(timeout=2)
    assert sched._tasks == {}

    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=3)
    stop_done.set()
    assert stop_done.is_set()


# ---------------------------------------------------------------------------
# 5. webhook_adapter.py: SSRF + WeCom/custom secret redaction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_hook", [
    "http://127.0.0.1:8080/hook",
    "http://169.254.169.254/latest",
    "http://localhost/hook",
    "http://[::1]/hook",
    "http://100.64.0.1/hook",
    "http://[fc00::1]/hook",
    "https://:pass@example.com/hook",
    "https://:8080/hook",
    "file:///tmp/hook",
])
def test_webhook_rejects_ssrf_and_malformed_urls(monkeypatch: pytest.MonkeyPatch, bad_hook: str) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SAFE_HTTP_ALLOW_HOSTS", raising=False)
    assert webhook_adapter.is_valid_custom_url(bad_hook) is False
    assert webhook_adapter.send_custom(bad_hook, "t", "b", "evt", max_attempts=1) is False


def test_webhook_redacts_wecom_key_and_custom_token_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def boom_post(url: str, **kwargs):
        raise RuntimeError(f"connection failed to {url}")

    monkeypatch.setattr("httpx.post", boom_post)
    with caplog.at_level(logging.WARNING, logger=webhook_adapter.__name__):
        ok1 = webhook_adapter.send_wecom(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key-999",
            "title",
            "body",
        )
        ok2 = webhook_adapter.send_custom(
            "https://hooks.example.com/notify?token=custom-secret-888",
            "title",
            "body",
            "evt",
            max_attempts=1,
        )
    assert ok1 is False and ok2 is False
    assert "wecom-secret-key-999" not in caplog.text
    assert "custom-secret-888" not in caplog.text


# ---------------------------------------------------------------------------
# 6. tdxquant/client.py: 0o600 INI permissions + no os.environ mutation
# ---------------------------------------------------------------------------

def test_tdxquant_client_no_env_mutation_and_ini_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / tdx_client.LIB_DLL_NAME).write_bytes(b"DLL")
    (src / "TdxAiData.ini").write_text("[Token]\ntoken=OLD\n", encoding="utf-8")
    (src / "user.ini").write_text("[Token]\ntoken=OLD\n", encoding="utf-8")

    icacls_calls: list[list[str]] = []
    real_run = tdx_client.subprocess.run

    def spy_run(cmd, *args, **kwargs):
        icacls_calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tdx_client.subprocess, "run", spy_run)
    lib_root = tmp_path / "priv_lib"
    dll = tdx_client.materialize_lib("TDX-SECRET-1", lib_root, source=src)
    assert dll == lib_root / tdx_client.LIB_DLL_NAME
    if sys.platform == "win32":
        assert any("icacls" in str(c[0]) and "TdxAiData.ini" in str(c[1]) for c in icacls_calls)
    else:
        assert (lib_root / "TdxAiData.ini").stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(tdx_client.subprocess, "run", real_run)

    # Verify TdxQuantClient.__init__ does NOT mutate os.environ[LIB_ENV]
    monkeypatch.delenv(tdx_client.LIB_ENV, raising=False)
    monkeypatch.setattr(tdx_client, "materialize_lib", lambda k, r: dll)

    init_calls: list[str | None] = []

    class _FakeNative:
        _instance = None

        def __init__(self, dll_path: str | None = None) -> None:
            init_calls.append(dll_path)
            _FakeNative._instance = self

        @classmethod
        def get_instance(cls):
            return cls._instance

    import tdxaidata
    import tdxaidata.tdxaidata as tdxmod

    monkeypatch.setattr(tdxmod, "_NativeTdxAiData", _FakeNative)
    monkeypatch.setattr(tdxaidata.tqs, "get_market_snapshot_batch", lambda **kw: {"600519.SH": {"Now": 1}})

    client = tdx_client.TdxQuantClient("TDX-SECRET-1", lib_root=lib_root)
    assert tdx_client.LIB_ENV not in tdx_client.os.environ
    assert init_calls == [str(dll)]
    assert client.api_key == "TDX-SECRET-1"


# ---------------------------------------------------------------------------
# 7. Iteration 2 remediation regression tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_idna_or_nat64", [
    "http://127\u30020\u30020\u30021/",
    "http://127\uff0e0\uff0e0\uff0e1/",
    "http://127\uff610\uff610\uff611/",
    "http://localhost\u3002/",
    "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11/",
    "http://\uff11\uff12\uff17\u3002\uff10\u3002\uff10\u3002\uff11/",
    "http://\u24db\u24de\u24d2\u24d0\u24db\u24d7\u24de\u24e2\u24e3/",
    "http://xn--invalid--/",
    "http://[64:ff9b::127.0.0.1]/",
    "http://[64:ff9b::10.0.0.1]/",
    "http://[64:ff9b::169.254.169.254]/",
])
def test_safe_http_blocks_unicode_idna_and_nat64(
    monkeypatch: pytest.MonkeyPatch, bad_idna_or_nat64: str,
) -> None:
    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SAFE_HTTP_ALLOW_HOSTS", raising=False)
    assert safe_http.is_safe_url(bad_idna_or_nat64) is False
    with pytest.raises(safe_http.UnsafeURLError):
        safe_http.validate_url(bad_idna_or_nat64)

    # Also verify resolve_and_pin_host blocks the extracted hostname directly
    from urllib.parse import urlsplit

    host = urlsplit(bad_idna_or_nat64).hostname or ""
    if "xn--invalid--" not in host:
        with pytest.raises(safe_http.UnsafeURLError):
            safe_http.resolve_and_pin_host(host, 80)


def test_safe_http_strips_cookies_kwarg_and_cookie_jar_across_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/hop":
                self.send_response(302)
                self.send_header("Set-Cookie", "jar_sid=jar_secret; Domain=.example.test; Path=/")
                self.send_header("Location", f"http://other.example.test:{self.server.server_port}/final")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            seen_headers.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return None

    server, port, thread = _start_http_server(_Handler)
    try:
        real_getaddrinfo = socket.getaddrinfo
        monkeypatch.setenv("SAFE_HTTP_ALLOW_HOSTS", "origin.example.test,other.example.test")
        monkeypatch.setattr(
            safe_http.socket,
            "getaddrinfo",
            lambda host, p, *args, **kw: (
                real_getaddrinfo(host, p, *args, **kw)
                if host == "127.0.0.1"
                else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]
            ),
        )

        resp_sync = safe_http.sync_request(
            "GET",
            f"http://origin.example.test:{port}/hop",
            max_redirects=3,
            cookies={"sid": "leaked_cookie_kwarg"},
        )
        assert resp_sync.status_code == 200
        resp_async = asyncio.run(
            safe_http.async_request(
                "GET",
                f"http://origin.example.test:{port}/hop",
                max_redirects=3,
                cookies={"sid": "leaked_cookie_kwarg"},
            )
        )
        assert resp_async.status_code == 200
        assert len(seen_headers) == 2
        for hdrs in seen_headers:
            assert "cookie" not in hdrs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_save_tickflow_key_restores_on_probe_failure_none_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import tickflow as tickflow_sdk

    from app.config import settings as app_settings
    from app.tickflow import policy

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    secrets_store._cache = None
    secrets_store._cache_sig = None

    secrets_store.save({
        "tickflow_api_key": "tf-paid-good",
        "tickflow_base_url": "https://paid.tickflow.test",
    })
    cap_path = tmp_path / policy._CAPSET_CACHE_FILE
    cap_path.write_text(
        json.dumps({
            "schema_version": policy._CACHE_SCHEMA_VERSION,
            "label": "Pro",
            "capabilities": {"kline.daily.by_symbol": {}, "adj_factor": {}},
            "probe_log": [],
            "missing_caps": [],
            "extras_caps": [],
            "invalid_key": False,
        }),
        encoding="utf-8",
    )
    old_cap_bytes = cap_path.read_bytes()
    old_caps_obj = SimpleNamespace(all=lambda: {"kline.daily.by_symbol": object(), "adj_factor": object()})
    monkeypatch.setattr(settings_api.tf_client, "reset_clients", lambda: None)
    monkeypatch.setattr(
        tickflow_sdk,
        "TickFlow",
        lambda **kw: (_ for _ in ()).throw(TimeoutError("connect timed out to https://paid.tickflow.test?token=tf-probe-bad")),
    )

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(capabilities=old_caps_obj)))
    res = settings_api.save_tickflow_key(settings_api.TickflowKeyIn(api_key="tf-probe-bad"), req)
    assert res["ok"] is False
    assert secrets_store.load()["tickflow_api_key"] == "tf-paid-good"
    assert secrets_store.load()["tickflow_base_url"] == "https://paid.tickflow.test"
    assert cap_path.read_bytes() == old_cap_bytes
    assert req.app.state.capabilities is old_caps_obj


def test_ai_settings_blocks_dns_ssrf_and_unicode_idna_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    from app.services import ai_provider

    monkeypatch.delenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SAFE_HTTP_ALLOW_HOSTS", raising=False)
    monkeypatch.delenv("AI_ALLOW_LOCAL", raising=False)

    hits: list[str] = []

    class _TrapHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            hits.append(self.path)
            body = json.dumps({"choices": [{"message": {"content": "PWNED"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server, port, thread = _start_http_server(_TrapHandler)
    try:
        # 1) Zero-mock Unicode IDNA literal must be blocked before any HTTP call
        res_idna = asyncio.run(settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="openai_compat",
            base_url=f"http://127\u30020\u30020\u30021:{port}/v1",
            api_key="sk-test",
            model="gpt-4",
        )))
        assert res_idna["ok"] is False
        assert res_idna["error_type"] == "UnsafeURLError"
        assert hits == []

        # 2) DNS-resolved private IP must be blocked before probe_ai_config connects
        monkeypatch.setattr(
            safe_http.socket,
            "getaddrinfo",
            lambda host, p, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))],
        )
        res_dns = asyncio.run(settings_api.test_ai_settings(settings_api.AiTestIn(
            provider="openai_compat",
            base_url=f"http://evil-rebind.example.test:{port}/v1",
            api_key="sk-test",
            model="gpt-4",
        )))
        assert res_dns["ok"] is False
        assert res_dns["error_type"] == "UnsafeURLError"
        assert hits == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    # 3) OpenAI transport errors redact api_key and URL query secrets
    req_obj = httpx.Request("POST", "https://api.example.com/v1/chat/completions?token=SECRET_TOK")
    resp_obj = httpx.Response(500, request=req_obj)

    async def fail_openai(**kwargs):
        raise openai.InternalServerError(
            "bad key sk-test-secret at https://api.example.com?token=SECRET_TOK",
            response=resp_obj,
            body={"error": {"message": "bad key sk-test-secret at https://api.example.com?token=SECRET_TOK"}},
        )

    monkeypatch.setattr(ai_provider, "probe_ai_config", fail_openai)
    res_transport = asyncio.run(settings_api.test_ai_settings(settings_api.AiTestIn(
        provider="openai_compat",
        base_url="https://api.example.com/v1",
        api_key="sk-test-secret",
        model="gpt-4",
    )))
    assert res_transport["ok"] is False
    assert "sk-test-secret" not in res_transport["error"]
    assert "SECRET_TOK" not in res_transport["error"]

    # 4) Non-transport exceptions do not leak internal filesystem paths
    async def fail_path(**kwargs):
        raise RuntimeError(r"Config failed at C:\Users\admin\.tick\secrets.json")

    monkeypatch.setattr(ai_provider, "probe_ai_config", fail_path)
    res_path = asyncio.run(settings_api.test_ai_settings(settings_api.AiTestIn(
        provider="openai_compat",
        base_url="https://api.example.com/v1",
        api_key="sk-test-secret",
        model="gpt-4",
    )))
    assert res_path["ok"] is False
    assert "secrets.json" not in res_path["error"]
    assert "admin" not in res_path["error"]


def test_data_source_sanitizes_value_error_paths_secrets_and_json_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.data_providers import custom as custom_sources

    class _LeakValueProvider:
        def test_dataset(self, ds, syms):
            raise ValueError(r"Failed reading C:\internal\secret_path with header secret MY_HEADER_SECRET_999")

    monkeypatch.setattr(custom_sources, "get_provider", lambda name: _LeakValueProvider())
    with pytest.raises(HTTPException) as exc_val:
        settings_api.test_data_source(settings_api.CustomSourceTestIn(provider="p", dataset="daily"))
    assert exc_val.value.status_code == 400
    assert "secret_path" not in exc_val.value.detail
    assert "MY_HEADER_SECRET_999" not in exc_val.value.detail

    class _JsonErrProvider:
        def test_dataset(self, ds, syms):
            raise json.JSONDecodeError("Expecting value", "SECRET_DOC_PAYLOAD", 0)

    monkeypatch.setattr(custom_sources, "get_provider", lambda name: _JsonErrProvider())
    with pytest.raises(HTTPException) as exc_json:
        settings_api.test_data_source(settings_api.CustomSourceTestIn(provider="p", dataset="daily"))
    assert exc_json.value.status_code == 400
    assert "SECRET_DOC_PAYLOAD" not in exc_json.value.detail


def test_uninstall_and_delete_reset_unloaded_provider_preferences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.config import settings as app_settings
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    preferences._cache = None
    preferences._cache_sig = None

    monkeypatch.setattr(custom_sources, "names", lambda: set())
    monkeypatch.setattr(custom_sources, "is_builtin", lambda name: name == "fuyao")
    monkeypatch.setattr(custom_sources, "uninstall_plugin", lambda name: (True, "ok"))
    monkeypatch.setattr(custom_sources, "delete_config", lambda name: None)
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [])
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(custom_sources, "errors", lambda: [])
    monkeypatch.setattr(custom_sources, "data_sources_dir", lambda: tmp_path)
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: SimpleNamespace(all=lambda: {}))

    req_app = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(capabilities=None)))

    # 1) Unloaded plugin still reset in preferences.json on uninstall_plugin
    preferences.save({k: "fuyao" for _, k in settings_api._ALL_PROVIDER_PREFS})
    assert preferences.get_daily_data_provider() == "tickflow"  # Masked by _validated_provider
    settings_api.uninstall_plugin("fuyao", req_app)
    raw_after_uninstall = preferences.load()
    assert all(raw_after_uninstall[k] == "tickflow" for _, k in settings_api._ALL_PROVIDER_PREFS)

    # 2) Unloaded custom source still reset in preferences.json on delete_data_source
    preferences.save({k: "unloaded_custom" for _, k in settings_api._ALL_PROVIDER_PREFS})
    settings_api.delete_data_source("unloaded_custom", req_app)
    raw_after_delete = preferences.load()
    assert all(raw_after_delete[k] == "tickflow" for _, k in settings_api._ALL_PROVIDER_PREFS)


def test_dimension_intraday_cache_generation_and_pull_scheduler_invalidation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ext_api._invalidate_dimension_intraday_cache()
    store = ExtConfigStore(tmp_path)
    cfg = ExtConfig(
        id="sec_tbl",
        label="概念",
        mode="snapshot",
        fields=[ExtField("symbol"), ExtField("tag")],
        pull=PullConfig(url="https://api.example.test/data", enabled=True),
    )
    store.upsert(cfg)

    # 1) Compute-vs-invalidation race: stale in-flight compute must NOT populate cache after invalidation
    in_compute = threading.Event()
    proceed_compute = threading.Event()
    version = {"v": 1}

    def slow_compute(config, data_dir: Path, field: str, value: str, snapshot_date: str | None) -> dict:
        cur = version["v"]
        if cur == 1:
            in_compute.set()
            assert proceed_compute.wait(timeout=3)
        return {"status": "ok", "version": cur, "points": []}

    monkeypatch.setattr(ext_api, "_dimension_intraday_compute", slow_compute)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path, db=None))
    )))

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut = pool.submit(ext_api.dimension_intraday, req, "sec_tbl", "tag", "AI", None)
        assert in_compute.wait(timeout=3)
        version["v"] = 2
        ext_api._invalidate_dimension_intraday_cache("sec_tbl", tmp_path)
        proceed_compute.set()
        first_res = fut.result(timeout=3)
        assert first_res["version"] == 1

    # Subsequent call must compute fresh v=2 instead of returning cached stale v=1
    second_res = ext_api.dimension_intraday(req, "sec_tbl", field="tag", value="AI", snapshot_date=None)
    assert second_res["version"] == 2

    # 2) PullScheduler._execute_pull invalidates _DIMENSION_INTRADAY_CACHE
    version["v"] = 3

    async def fake_fetch_and_ingest(config, data_dir, target_date=None, *, keep_strategy_cache=False):
        return 5, "2026-09-18"

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fake_fetch_and_ingest)
    sched = ext_pull.PullScheduler()
    sched._data_dir = tmp_path
    asyncio.run(sched._execute_pull(cfg, store))

    third_res = ext_api.dimension_intraday(req, "sec_tbl", field="tag", value="AI", snapshot_date=None)
    assert third_res["version"] == 3
    ext_api._invalidate_dimension_intraday_cache()


def test_ext_pull_401_and_webhook_business_error_redaction(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # 1) ext_pull._request_json 401/403 redacts resp.text[:160]
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda cid: "super-secret-query-key")

    def handler_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text="denied for https://api.example.test/v1?token=SECRET401 key=super-secret-query-key",
            request=request,
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        ext_pull.httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler_401), **kw),
    )
    pull = PullConfig(url="https://api.example.test/v1", auth={"type": "query", "param": "custom_tok"})
    with pytest.raises(ValueError) as err:
        asyncio.run(ext_pull._request_json(pull, "cfg"))
    assert "SECRET401" not in str(err.value)
    assert "super-secret-query-key" not in str(err.value)

    # 2) webhook_adapter._post_feishu & _post_wecom redact business error dict logs
    with caplog.at_level(logging.WARNING, logger=webhook_adapter.__name__):
        monkeypatch.setattr(
            safe_http,
            "post_sync",
            lambda url, **kw: httpx.Response(
                200,
                json={
                    "errcode": 40001,
                    "errmsg": "bad url https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=WECOM_SECRET_XYZ raw WECOM_SECRET_XYZ",
                },
                request=httpx.Request("POST", url),
            ),
        )
        assert webhook_adapter._post_wecom(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=WECOM_SECRET_XYZ",
            {"msgtype": "text"},
        ) is False
        assert "WECOM_SECRET_XYZ" not in caplog.text

        caplog.clear()
        monkeypatch.setattr(
            safe_http,
            "post_sync",
            lambda url, **kw: httpx.Response(
                200,
                json={
                    "code": 19001,
                    "msg": "bad url https://open.feishu.cn/open-apis/bot/v2/hook/abc?token=TOK_F feishu-sec",
                },
                request=httpx.Request("POST", url),
            ),
        )
        assert webhook_adapter._post_feishu(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            {"msg_type": "text"},
            "feishu-sec",
            max_attempts=1,
        ) is False
        assert "feishu-sec" not in caplog.text
        assert "TOK_F" not in caplog.text

"""M1.1 边缘认证、开放网关、CORS、CSRF、响应头、请求体守卫与备份重载安全回归测试。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config as app_config
from app import secrets_store
from app.api import auth as auth_api
from app.api import config_backup as config_backup_api
from app.config import Settings
from app.custom.assistant import sessions as assistant_sessions
from app.main import (
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
    _DynamicCORSMiddleware,
    _register_static_routes,
    _verify_csrf,
    auth_middleware,
)
from app.main import (
    app as main_app,
)
from app.services import (
    api_gateway,
    api_tokens,
    preferences,
    watchlist,
)
from app.services import (
    auth as auth_service,
)
from app.services import (
    config_backup as config_backup_svc,
)
from app.tickflow import client as tf_client


@pytest.fixture(autouse=True)
def _isolated_m1_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(app_config.settings, "open_api_cors_origins", "")
    monkeypatch.setattr(app_config.settings, "trusted_proxy_cidrs", "127.0.0.1/32,::1/128")
    monkeypatch.setattr(app_config.settings, "auth_cookie_secure", None)
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    api_gateway.compile_trusted_cidrs.cache_clear()
    api_gateway.clear_rate_limit_buckets()
    api_tokens.clear_cache()
    with auth_service._lock:
        auth_service._sessions.clear()
        auth_service._configured_cache = None
        auth_service._fail_counter.clear()
        auth_service._fail_loaded_path = None
    secrets_store._cache = None
    secrets_store._cache_sig = None
    preferences._invalidate_cache()
    assistant_sessions._cache = None
    assistant_sessions._cache_sig = None
    tf_client.reset_clients()
    yield tmp_path
    api_gateway.compile_trusted_cidrs.cache_clear()
    api_gateway.clear_rate_limit_buckets()
    api_tokens.clear_cache()
    with auth_service._lock:
        auth_service._sessions.clear()
        auth_service._configured_cache = None
        auth_service._fail_counter.clear()
        auth_service._fail_loaded_path = None
    secrets_store._cache = None
    secrets_store._cache_sig = None
    preferences._invalidate_cache()
    assistant_sessions._cache = None
    assistant_sessions._cache_sig = None
    tf_client.reset_clients()


def _make_fake_request(
    *,
    client_host: str | None = "127.0.0.1",
    headers: list[tuple[str, str]] | None = None,
    scheme: str = "http",
) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or [])
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": scheme,
        "path": "/api/auth/login",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host is not None else None,
        "server": ("testserver", 443 if scheme == "https" else 80),
    }
    return Request(scope)


# ================================================================
# 1. Settings 配置校验
# ================================================================

def test_settings_edge_fields_defaults_and_validators(tmp_path: Path):
    s = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    assert s.open_api_cors_origins == ""
    assert s.cors_origins() == []
    assert s.trusted_proxy_cidrs == "127.0.0.1/32,::1/128"
    assert len(s.trusted_proxy_networks()) == 2
    assert s.auth_cookie_secure is None
    assert s.ai_daily_token_budget == 500_000

    s_cors = Settings(
        _env_file=None,
        data_dir=tmp_path,
        open_api_cors_origins=" https://a.example.com/ , http://localhost:5173, https://a.example.com ",
    )  # type: ignore[call-arg]
    assert s_cors.cors_origins() == ["https://a.example.com", "http://localhost:5173"]

    for auto_val in (None, "", "auto", "AUTO"):
        s_auto = Settings(_env_file=None, data_dir=tmp_path, auth_cookie_secure=auto_val)  # type: ignore[call-arg]
        assert s_auto.auth_cookie_secure is None
    for true_val in (True, "true", "1", "yes", "on", "TRUE"):
        s_true = Settings(_env_file=None, data_dir=tmp_path, auth_cookie_secure=true_val)  # type: ignore[call-arg]
        assert s_true.auth_cookie_secure is True
    for false_val in (False, "false", "0", "no", "off", "FALSE"):
        s_false = Settings(_env_file=None, data_dir=tmp_path, auth_cookie_secure=false_val)  # type: ignore[call-arg]
        assert s_false.auth_cookie_secure is False

    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, auth_cookie_secure="sometimes")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, trusted_proxy_cidrs="not-a-cidr")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, data_dir=tmp_path, ai_daily_token_budget=-1)  # type: ignore[call-arg]


# ================================================================
# 2. Trusted Proxy 右向左 XFF 解析 & is_local_network 边界
# ================================================================

def test_trusted_proxy_xff_right_to_left_and_fail_closed(monkeypatch: pytest.MonkeyPatch):
    # 未受信直连公网 peer 伪造 XFF -> 必须忽略 XFF
    req_untrusted = _make_fake_request(
        client_host="203.0.113.9",
        headers=[("x-forwarded-for", "127.0.0.1")],
    )
    assert auth_api._client_ip(req_untrusted) == "203.0.113.9"

    # 可信回环代理转发客户端伪造的 XFF ("127.0.0.1, 203.0.113.9") -> 从右向左取到真实公网 IP
    req_spoofed = _make_fake_request(
        client_host="127.0.0.1",
        headers=[("x-forwarded-for", "127.0.0.1, 203.0.113.9")],
    )
    assert auth_api._client_ip(req_spoofed) == "203.0.113.9"
    assert api_gateway.token_network_verdict("203.0.113.9") is not None

    # 多跳可信代理链 (127.0.0.1 <- 10.1.2.3 <- 198.51.100.4)
    monkeypatch.setattr(app_config.settings, "trusted_proxy_cidrs", "127.0.0.0/8,10.0.0.0/8")
    req_chain = _make_fake_request(
        client_host="127.0.0.1",
        headers=[("x-forwarded-for", "198.51.100.4"), ("x-forwarded-for", "10.1.2.3")],
    )
    assert auth_api._client_ip(req_chain) == "198.51.100.4"

    # 全可信跳 -> 返回最左可信跳
    req_all_trusted = _make_fake_request(
        client_host="127.0.0.1",
        headers=[("x-forwarded-for", "10.9.9.9, 10.1.2.3")],
    )
    assert auth_api._client_ip(req_all_trusted) == "10.9.9.9"

    # 最右跳或非可信跳畸形 -> fail-closed 返回 "unknown"
    req_bad_hop = _make_fake_request(
        client_host="127.0.0.1",
        headers=[("x-forwarded-for", "127.0.0.1, garbage")],
    )
    assert auth_api._client_ip(req_bad_hop) == "unknown"
    assert api_gateway.is_local_network("unknown") is False

    # 双栈 IPv4-mapped IPv6 回环代理
    monkeypatch.setattr(app_config.settings, "trusted_proxy_cidrs", "127.0.0.1/32,::1/128")
    req_v4_mapped = _make_fake_request(
        client_host="::ffff:127.0.0.1",
        headers=[("x-forwarded-for", "198.51.100.8")],
    )
    assert auth_api._client_ip(req_v4_mapped) == "198.51.100.8"

    # TRUSTED_PROXY_CIDRS 为空时彻底禁用 XFF
    monkeypatch.setattr(app_config.settings, "trusted_proxy_cidrs", "")
    req_disabled = _make_fake_request(
        client_host="127.0.0.1",
        headers=[("x-forwarded-for", "198.51.100.8")],
    )
    assert auth_api._client_ip(req_disabled) == "127.0.0.1"


def test_is_local_network_strict_cidrs():
    for addr in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1", "localhost", "10.0.0.1", "172.16.0.1", "192.168.1.1", "fc00::1", "fd12::1"):
        assert api_gateway.is_local_network(addr) is True, addr
    for non_local in ("169.254.169.254", "100.64.0.1", "172.32.0.1", "0.0.0.0", "8.8.8.8", "testclient", "unknown", "", None):
        assert api_gateway.is_local_network(non_local) is False, repr(non_local)


def test_setup_rejects_spoofed_xff_behind_trusted_proxy():
    with TestClient(main_app, client=("127.0.0.1", 12345)) as client:
        resp = client.post(
            "/api/auth/setup",
            json={"password": "secret-password"},
            headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.55"},
        )
        assert resp.status_code == 403


# ================================================================
# 3. Secure Cookie 与 Logout / Change-Password
# ================================================================

def test_auth_cookie_secure_auto_explicit_and_deletion(monkeypatch: pytest.MonkeyPatch):
    # auto mode
    assert auth_api._should_set_secure_cookie(_make_fake_request(scheme="http")) is False
    assert auth_api._should_set_secure_cookie(_make_fake_request(scheme="https")) is True
    assert auth_api._should_set_secure_cookie(
        _make_fake_request(client_host="127.0.0.1", headers=[("x-forwarded-proto", "http, https")])
    ) is True
    assert auth_api._should_set_secure_cookie(
        _make_fake_request(client_host="127.0.0.1", headers=[("x-forwarded-proto", "https, http")])
    ) is False
    assert auth_api._should_set_secure_cookie(
        _make_fake_request(client_host="203.0.113.10", headers=[("x-forwarded-proto", "https")])
    ) is False

    # explicit True / False
    monkeypatch.setattr(app_config.settings, "auth_cookie_secure", True)
    assert auth_api._should_set_secure_cookie(_make_fake_request(scheme="http")) is True
    monkeypatch.setattr(app_config.settings, "auth_cookie_secure", False)
    assert auth_api._should_set_secure_cookie(_make_fake_request(scheme="https")) is False

    # end-to-end login / change-password / logout Set-Cookie attributes
    auth_service.set_password("initial-pw-123")
    monkeypatch.setattr(app_config.settings, "auth_cookie_secure", True)
    with TestClient(
        main_app, base_url="https://testserver", client=("127.0.0.1", 12345),
    ) as client:
        r_login = client.post("/api/auth/login", json={"password": "initial-pw-123"})
        assert r_login.status_code == 200
        set_cookie_login = r_login.headers.get("set-cookie", "").lower()
        assert "tf_session=" in set_cookie_login
        assert "httponly" in set_cookie_login
        assert "samesite=lax" in set_cookie_login
        assert "secure" in set_cookie_login

        r_chg = client.post(
            "/api/auth/change-password",
            json={"old_password": "initial-pw-123", "new_password": "updated-pw-456"},
        )
        assert r_chg.status_code == 200
        set_cookie_chg = r_chg.headers.get("set-cookie", "").lower()
        assert "tf_session=" in set_cookie_chg
        assert "max-age=0" in set_cookie_chg
        assert "secure" in set_cookie_chg

        # re-login with new password and test logout cookie deletion
        assert client.post("/api/auth/login", json={"password": "updated-pw-456"}).status_code == 200
        r_logout = client.post("/api/auth/logout")
        assert r_logout.status_code == 200
        set_cookie_out = r_logout.headers.get("set-cookie", "").lower()
        assert "tf_session=" in set_cookie_out
        assert "max-age=0" in set_cookie_out
        assert "httponly" in set_cookie_out
        assert "samesite=lax" in set_cookie_out
        assert "secure" in set_cookie_out


# ================================================================
# 4. 持久化登录锁定、并发锁、会话清理与 TOCTOU 防护
# ================================================================

def test_persistent_login_lockout_and_change_password_enforcement(tmp_path: Path):
    auth_service.set_password("correct-pw-123")
    with TestClient(main_app, client=("127.0.0.1", 12345)) as client:
        for _ in range(5):
            r = client.post(
                "/api/auth/login",
                json={"password": "wrong-pw"},
                headers={"X-Forwarded-For": "198.51.100.77"},
            )
            assert r.status_code == 401
        assert client.post(
            "/api/auth/login",
            json={"password": "correct-pw-123"},
            headers={"X-Forwarded-For": "198.51.100.77"},
        ).status_code == 429

    # 模拟进程重启: 清空内存计数与加载路径标记, 从磁盘重新加载依然锁定
    with auth_service._lock:
        auth_service._fail_counter.clear()
        auth_service._fail_loaded_path = None
    assert (tmp_path / "user_data" / "auth_lockouts.json").is_file()
    assert auth_service.check_login_rate_limit("198.51.100.77") is not None

    # 坏条目不跳过后续有效锁定记录
    lockouts_path = tmp_path / "user_data" / "auth_lockouts.json"
    future_until = time.time() + 180
    lockouts_path.write_text(
        json.dumps({
            "entries": {
                "198.51.100.1": {"count": "corrupt", "until": "bad", "updated_at": None},
                "198.51.100.2": {"count": None, "until": future_until, "updated_at": time.time()},
                "198.51.100.77": {"count": 5, "until": future_until, "updated_at": time.time()},
            }
        }),
        encoding="utf-8",
    )
    with auth_service._lock:
        auth_service._fail_counter.clear()
        auth_service._fail_loaded_path = None
    assert auth_service.check_login_rate_limit("198.51.100.77") is not None

    # change_password 也受锁定约束, 且成功后清零
    with TestClient(main_app, client=("127.0.0.1", 12345)) as client:
        assert client.post(
            "/api/auth/login",
            json={"password": "correct-pw-123"},
            headers={"X-Forwarded-For": "198.51.100.88"},
        ).status_code == 200
        for _ in range(5):
            r = client.post(
                "/api/auth/change-password",
                json={"old_password": "bad-old", "new_password": "new-pw-999"},
                headers={"X-Forwarded-For": "198.51.100.88"},
            )
            assert r.status_code == 401
        assert client.post(
            "/api/auth/change-password",
            json={"old_password": "correct-pw-123", "new_password": "new-pw-999"},
            headers={"X-Forwarded-For": "198.51.100.88"},
        ).status_code == 429


def test_lockout_bounded_and_concurrent_safety(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth_service, "MAX_LOCKOUT_ENTRIES", 16)
    for i in range(40):
        auth_service.record_login_fail(f"198.51.100.{i}")
    with auth_service._lock:
        assert len(auth_service._fail_counter) <= 16

    def _hammer(idx: int) -> None:
        auth_service.record_login_fail(f"203.0.113.{idx % 4}")
        auth_service.check_login_rate_limit(f"203.0.113.{idx % 4}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_hammer, range(40)))


def test_restore_sessions_and_invalidate_all_sessions():
    auth_service.set_password("pw-one-123")
    tok_old = auth_service.verify_and_create_session("pw-one-123")
    assert tok_old and auth_service.is_valid_session(tok_old)

    # 外部覆盖 auth.json (不含 tok_old) 后调用 _restore_sessions(), 旧内存会话必须被清除
    auth_service._save({"password_hash": "other", "password_salt": "00", "sessions": {}})
    auth_service.invalidate_runtime_caches()
    assert auth_service.is_valid_session(tok_old) is False

    # invalidate_all_sessions() 清空内存与磁盘 sessions
    auth_service.set_password("pw-two-123")
    tok_new = auth_service.verify_and_create_session("pw-two-123")
    assert tok_new and auth_service.is_valid_session(tok_new)
    cleared = auth_service.invalidate_all_sessions()
    assert cleared >= 1
    assert auth_service.is_valid_session(tok_new) is False


# ================================================================
# 5. api_tokens.py M-01 缓存、并发、恒定时间比较
# ================================================================

def test_api_tokens_mtime_cache_hit_invalidation_and_dir_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    rec_a, plain_a = api_tokens.create_token(dir_a, "tokA", ["read:market"])
    rec_b, plain_b = api_tokens.create_token(dir_b, "tokB", ["read:ext"])
    assert rec_a["id"] != rec_b["id"]

    # 首查可能触发 last_used_at 节流落盘, 之后 10 次命中缓存应 0 次 read_text
    assert api_tokens.verify_token(dir_a, plain_a) is not None
    read_calls = 0
    orig_read_text = Path.read_text

    def _counting_read(self: Path, *args, **kwargs):
        nonlocal read_calls
        if self.name == "api_tokens.json":
            read_calls += 1
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting_read)
    for _ in range(10):
        got = api_tokens.verify_token(dir_a, plain_a)
        assert got is not None and got["id"] == rec_a["id"]
    assert read_calls == 0

    # 返回值深拷贝隔离: 修改返回对象不可污染缓存
    got["scopes"].append("admin")
    assert api_tokens.verify_token(dir_a, plain_a)["scopes"] == ["read:market"]
    views = api_tokens.list_tokens(dir_a)
    views[0]["scopes"].append("admin")
    assert api_tokens.list_tokens(dir_a)[0]["scopes"] == ["read:market"]

    # 多 data_dir 隔离
    assert api_tokens.verify_token(dir_b, plain_a) is None
    assert api_tokens.verify_token(dir_b, plain_b) is not None

    # 模拟同一秒相同 stat 签名下的快速创建/吊销, 缓存必须立即同步
    monkeypatch.setattr(api_tokens, "_stat_sig", lambda _p: (123456789, 512, 99))
    rec_fast, plain_fast = api_tokens.create_token(dir_a, "fast", ["read:market"])
    assert api_tokens.verify_token(dir_a, plain_fast) is not None
    assert api_tokens.revoke_token(dir_a, rec_fast["id"]) is True
    assert api_tokens.verify_token(dir_a, plain_fast) is None
    monkeypatch.undo()

    # 外部删改文件立即失效
    tokens_file = dir_a / "user_data" / "api_tokens.json"
    tokens_file.unlink()
    assert api_tokens.verify_token(dir_a, plain_a) is None


def test_api_tokens_hmac_compare_digest_and_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _, plain = api_tokens.create_token(tmp_path, "const-time", ["read:market"])
    p = tmp_path / "user_data" / "api_tokens.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["tokens"].insert(0, {
        "id": "tok_bad",
        "name": "bad",
        "token_hash": None,
        "scopes": ["read:market"],
        "created_at": "2026-01-01T00:00:00",
        "revoked": False,
    })
    p.write_text(json.dumps(raw), encoding="utf-8")

    digest_calls = 0
    orig_compare = hmac.compare_digest

    def _spy_compare(a, b):
        nonlocal digest_calls
        digest_calls += 1
        return orig_compare(a, b)

    monkeypatch.setattr(api_tokens.hmac, "compare_digest", _spy_compare)
    got = api_tokens.verify_token(tmp_path, plain)
    assert got is not None
    assert digest_calls >= 1

    # 并发读写压测
    created: list[tuple[str, str]] = []

    def _worker(i: int):
        r, pt = api_tokens.create_token(tmp_path, f"w{i}", ["read:market"])
        created.append((r["id"], pt))
        assert api_tokens.verify_token(tmp_path, pt) is not None
        api_tokens.list_tokens(tmp_path)
        if i % 2 == 0:
            api_tokens.revoke_token(tmp_path, r["id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_worker, range(24)))

    final_tokens = api_tokens.list_tokens(tmp_path)
    assert len(final_tokens) == 26  # 2 initial + 24 created


def test_api_tokens_and_gateway_tolerate_malformed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    _, plain_none = api_tokens.create_token(tmp_path, "none-scopes", ["read:market"])
    _, plain_int = api_tokens.create_token(tmp_path, "int-scopes", ["read:market"])
    _, plain_str = api_tokens.create_token(tmp_path, "str-scopes", ["read:market"])
    _, plain_mixed = api_tokens.create_token(tmp_path, "mixed-scopes", ["read:market"])
    _, plain_noid = api_tokens.create_token(tmp_path, "no-id", ["read:market"])

    p = tmp_path / "user_data" / "api_tokens.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    tokens = raw["tokens"]
    tokens[0]["scopes"] = None
    tokens[1]["scopes"] = 123
    tokens[2]["scopes"] = "read:market"
    tokens[3]["scopes"] = [123, None, "read:market"]
    tokens[4].pop("id", None)
    # 缺失 id/name/created_at 的残缺记录、缺失 name/created_at 的半残记录、非 ASCII token_hash、以及非 dict 脏项
    tokens.append({"revoked": False})
    tokens.append({"id": "tok_partial", "scopes": 999, "revoked": False})
    tokens.append({
        "id": "tok_nonascii_hash",
        "name": "nonascii",
        "token_hash": "非ASCII哈希",
        "scopes": ["read:market"],
        "created_at": "2026-01-01T00:00:00",
        "revoked": False,
    })
    plain_minimal = f"{api_tokens.TOKEN_PREFIX}{'ab' * 32}"
    tokens.append({
        "id": "tok_minimal_valid",
        "token_hash": hashlib.sha256(plain_minimal.encode("utf-8")).hexdigest(),
        "scopes": ["read:market", 42],
        "revoked": False,
    })
    tokens.extend([None, "corrupt-entry", 123])  # type: ignore[list-item]
    p.write_text(json.dumps(raw), encoding="utf-8")
    api_tokens.clear_cache()

    # list_tokens 不应抛出 KeyError / TypeError
    listed = api_tokens.list_tokens(tmp_path)
    by_id = {t["id"]: t for t in listed}
    assert len(by_id) == 7  # 4 个有效 id + tok_partial + tok_nonascii_hash + tok_minimal_valid; 缺 id / 非 dict 被跳过
    assert by_id[tokens[0]["id"]]["scopes"] == []
    assert by_id[tokens[1]["id"]]["scopes"] == []
    assert by_id[tokens[2]["id"]]["scopes"] == []
    assert by_id[tokens[3]["id"]]["scopes"] == ["read:market"]
    assert by_id["tok_partial"] == {
        "id": "tok_partial",
        "name": "",
        "scopes": [],
        "created_at": "",
        "last_used_at": None,
        "revoked": False,
    }
    assert by_id["tok_minimal_valid"]["name"] == ""
    assert by_id["tok_minimal_valid"]["created_at"] == ""
    assert by_id["tok_minimal_valid"]["scopes"] == ["read:market"]

    # verify_token 归一化 scopes 为 list[str], 缺 id 记录 fail-closed 返回 None
    assert api_tokens.verify_token(tmp_path, plain_none)["scopes"] == []
    assert api_tokens.verify_token(tmp_path, plain_int)["scopes"] == []
    assert api_tokens.verify_token(tmp_path, plain_str)["scopes"] == []
    assert api_tokens.verify_token(tmp_path, plain_mixed)["scopes"] == ["read:market"]
    assert api_tokens.verify_token(tmp_path, plain_noid) is None
    minimal_verified = api_tokens.verify_token(tmp_path, plain_minimal)
    assert minimal_verified is not None and minimal_verified["scopes"] == ["read:market"]
    # 含非 ASCII token_hash 时不抛 TypeError, 未知/不匹配令牌返回 None 且不影响其它有效令牌
    assert api_tokens.verify_token(tmp_path, f"{api_tokens.TOKEN_PREFIX}{'cd' * 32}") is None
    assert api_gateway.evaluate(tmp_path, "GET", "/api/kline/daily", f"{api_tokens.TOKEN_PREFIX}{'cd' * 32}")["status"] == 401

    # revoke_token 在含畸形条目文件与非法 token_id 下不崩溃且准确吊销
    assert api_tokens.revoke_token(tmp_path, "") is False
    assert api_tokens.revoke_token(tmp_path, None) is False  # type: ignore[arg-type]
    assert api_tokens.revoke_token(tmp_path, "tok_nonexistent") is False
    assert api_tokens.revoke_token(tmp_path, "tok_minimal_valid") is True
    assert api_tokens.verify_token(tmp_path, plain_minimal) is None

    # api_gateway.evaluate 遇到异常 scopes 返回 403 (绝不抛 TypeError 500)
    for bad_plain in (plain_none, plain_int, plain_str):
        verdict = api_gateway.evaluate(tmp_path, "GET", "/api/kline/daily", bad_plain)
        assert verdict["status"] == 403
    assert api_gateway.evaluate(tmp_path, "GET", "/api/kline/daily", plain_mixed)["status"] is None
    assert api_gateway.evaluate(tmp_path, "GET", "/api/kline/daily", plain_noid)["status"] == 401

    # 防御纵深: 即使 verify_token 返回未清洗的畸形字典, evaluate 仍安全拦截
    monkeypatch.setattr(
        api_tokens,
        "verify_token",
        lambda _d, _p: {"id": "tok_mock", "scopes": None},
    )
    assert api_gateway.evaluate(tmp_path, "GET", "/api/kline/daily", "tsp_any")["status"] == 403
    monkeypatch.setattr(
        api_tokens,
        "verify_token",
        lambda _d, _p: {"id": "tok_mock", "scopes": 123},
    )
    assert api_gateway.evaluate(tmp_path, "GET", "/api/kline/daily", "tsp_any")["status"] == 403
    monkeypatch.setattr(
        api_tokens,
        "verify_token",
        lambda _d, _p: {"scopes": ["read:market"]},
    )
    assert api_gateway.evaluate(tmp_path, "GET", "/api/kline/daily", "tsp_any")["status"] == 401


# ================================================================
# 6. api_gateway.py 有界 _BUCKETS LRU/TTL 与路径穿越防护
# ================================================================

def test_gateway_bounded_buckets_lru_and_ttl(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_gateway, "_MAX_BUCKETS", 4)
    now = [1000.0]
    monkeypatch.setattr(api_gateway.time, "monotonic", lambda: now[0])

    # 写入 4 个活跃桶, 并刷新 k0 使其成为 MRU
    for k in ("k0", "k1", "k2", "k3"):
        ok, rem, _ = api_gateway._allow(k, 5)
        assert ok is True and rem == 4
    api_gateway._allow("k0", 5)  # k0 剩 3; LRU 顺序 k1, k2, k3, k0

    # 新增 k4..k6 时总桶数始终 <= 4, 且活跃的 k0 不被先淘汰
    for k in ("k4", "k5"):
        api_gateway._allow(k, 5)
    assert len(api_gateway._BUCKETS) <= 4
    assert "k0" in api_gateway._BUCKETS
    assert "k1" not in api_gateway._BUCKETS

    # 推进时间超过窗口 -> 过期桶先被 TTL 清理
    now[0] += api_gateway._RATE_WINDOW_S + 5.0
    api_gateway._allow("fresh", 5)
    assert list(api_gateway._BUCKETS.keys()) == ["fresh"]

    # 并发限流精确性
    api_gateway.clear_rate_limit_buckets()
    monkeypatch.setattr(api_gateway, "_MAX_BUCKETS", 8)

    def _hit(i: int) -> bool:
        ok, _, _ = api_gateway._allow(f"tok_{i % 2}", 5)
        return ok

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_hit, range(30)))
    assert results.count(True) == 10
    assert len(api_gateway._BUCKETS) <= 8


def test_gateway_required_scope_path_hardening():
    assert api_gateway.required_scope("get", "/api/kline/daily") == "read:market"
    assert api_gateway.required_scope("GET", "/api/events/ticket") == "*"
    assert api_gateway.required_scope("GET", "/api/strategies/v1.0/source") == "read:analysis"
    assert api_gateway.required_scope("GET", "/api/paper/../settings/api-tokens") is None
    assert api_gateway.required_scope("GET", "/api/paper/%2e./settings/api-tokens") is None
    assert api_gateway.required_scope("GET", "/api/paper/.%2e/settings/api-tokens") is None
    assert api_gateway.required_scope("GET", "/api/paper/%2e%2e/settings/api-tokens") is None
    assert api_gateway.required_scope("GET", "/api/paper/%252e%252e/settings/api-tokens") is None
    assert api_gateway.required_scope("GET", "/api/paper/%252e./settings/api-tokens") is None
    assert api_gateway.required_scope("GET", "/api/paper/.%252e/settings/api-tokens") is None
    assert api_gateway.required_scope("GET", "/api/paper\\..\\settings") is None
    assert api_gateway.required_scope("GET", "/api//paper/orders") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/api-key/") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/api-key/.") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/api-key/%2e") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/api-key/%252e") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/api-key?foo=1") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/config/export/") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/config/export/.") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x/config/export/%252e") is None
    assert api_gateway.required_scope("POST", "/api/ext-data/x/ingest/.") is None
    assert api_gateway.required_scope("POST", "/api/ext-data/x/ingest/%2e") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x%5cy") is None
    assert api_gateway.required_scope("GET", "/api/ext-data/x%2fy") is None


# ================================================================
# 7. CORS、安全响应头/CSP、请求体上限、CSRF、/api/* 404 与文档鉴权
# ================================================================

def test_cors_allowlist_and_error_headers(monkeypatch: pytest.MonkeyPatch):
    auth_service.set_password("cors-pw-123")
    with TestClient(main_app, client=("127.0.0.1", 12345)) as client:
        # 默认 OPEN_API_CORS_ORIGINS="" -> 拒绝任意跨域 Origin
        r_default = client.options(
            "/api/kline/daily",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in r_default.headers

        # 显式白名单 -> 放行指定 Origin, 且 auth_middleware 返回 401 时仍带 CORS 头
        monkeypatch.setattr(app_config.settings, "open_api_cors_origins", "https://app.example.com")
        r_ok = client.get("/api/kline/daily", headers={"Origin": "https://app.example.com"})
        assert r_ok.status_code == 401
        assert r_ok.headers.get("access-control-allow-origin") == "https://app.example.com"
        assert r_ok.headers.get("access-control-allow-credentials") != "true"

        r_bad = client.get("/api/kline/daily", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r_bad.headers

        # 通配 "*" 时强禁 allow_credentials=True
        monkeypatch.setattr(app_config.settings, "open_api_cors_origins", "*")
        r_wild = client.get("/api/kline/daily", headers={"Origin": "https://any.example"})
        assert r_wild.headers.get("access-control-allow-origin") == "*"
        assert r_wild.headers.get("access-control-allow-credentials") != "true"


def test_security_headers_and_csp(tmp_path: Path):
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    inline_js = "window.__INIT__ = 1;"
    expected_hash = hashlib.sha256(inline_js.encode("utf-8")).hexdigest()
    del expected_hash
    (static_dir / "index.html").write_text(
        f"<!doctype html><html><head><script>{inline_js}</script></head><body></body></html>",
        encoding="utf-8",
    )

    mini = FastAPI()

    @mini.get("/api/ping")
    def ping():
        return {"ok": True}

    @mini.get("/docs")
    def docs():
        return {"docs": True}

    mini.add_middleware(SecurityHeadersMiddleware, static_dir=static_dir)
    with TestClient(mini) as client:
        r_api = client.get("/api/ping")
        assert r_api.headers["x-content-type-options"] == "nosniff"
        assert r_api.headers["x-frame-options"] == "SAMEORIGIN"
        assert r_api.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        csp = r_api.headers["content-security-policy"]
        assert "script-src 'self' blob: 'sha256-" in csp
        assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
        assert "frame-src http: https:" in csp
        assert "https://api.github.com" in csp

        r_docs = client.get("/docs")
        assert "https://cdn.jsdelivr.net" in r_docs.headers["content-security-policy"]


def test_body_limit_middleware_content_length_and_streaming():
    mini = FastAPI()

    @mini.post("/api/small")
    async def small(request: Request):
        data = await request.body()
        return {"size": len(data)}

    @mini.post("/api/config-backup/inspect")
    async def backup_inspect(request: Request):
        data = await request.body()
        return {"size": len(data)}

    mini.add_middleware(BodyLimitMiddleware)
    with TestClient(mini) as client:
        # 非法 / 负 Content-Length -> 400
        assert client.post("/api/small", content=b"x", headers={"Content-Length": "abc"}).status_code == 400
        assert client.post("/api/small", content=b"x", headers={"Content-Length": "-5"}).status_code == 400

        # 超默认 10 MiB 上限 (有 Content-Length) -> 413
        oversized = b"a" * (10 * 1024 * 1024 + 1)
        assert client.post("/api/small", content=oversized).status_code == 413

        # 备份路由允许 > 10 MiB (<= 132 MiB)
        large_ok = b"b" * (11 * 1024 * 1024)
        r_ok = client.post("/api/config-backup/inspect", content=large_ok)
        assert r_ok.status_code == 200 and r_ok.json()["size"] == len(large_ok)

    # 无 Content-Length 的分片流式体超过上限 -> 413
    received = {"status": None, "body": b""}

    async def _run_chunked():
        mw = BodyLimitMiddleware(mini)
        chunks = [b"x" * (4 * 1024 * 1024), b"y" * (4 * 1024 * 1024), b"z" * (4 * 1024 * 1024)]
        idx = 0

        async def receive():
            nonlocal idx
            if idx < len(chunks):
                chunk = chunks[idx]
                idx += 1
                return {"type": "http.request", "body": chunk, "more_body": idx < len(chunks)}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                received["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                received["body"] += msg.get("body", b"")

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/small",
            "raw_path": b"/api/small",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
        await mw(scope, receive, send)

    asyncio.run(_run_chunked())
    assert received["status"] == 413

    # 原始 ASGI 重复 / 冲突 / 非法多值 Content-Length 均应被中间件直接拦截 (400 / 413)
    async def _run_raw_cl(headers: list[tuple[bytes, bytes]]) -> int | None:
        mw = BodyLimitMiddleware(mini)
        status_out: int | None = None
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"{}", "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            nonlocal status_out
            if msg["type"] == "http.response.start":
                status_out = msg["status"]

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/small",
            "raw_path": b"/api/small",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
        await mw(scope, receive, send)
        return status_out

    assert asyncio.run(_run_raw_cl([(b"content-length", b"5"), (b"content-length", b"abc")])) == 400
    assert asyncio.run(_run_raw_cl([(b"content-length", b"5"), (b"content-length", b"-1")])) == 400
    assert asyncio.run(_run_raw_cl([(b"content-length", b"5"), (b"content-length", b"10")])) == 400
    assert asyncio.run(_run_raw_cl([(b"content-length", b"5"), (b"content-length", b"5")])) == 400
    assert asyncio.run(_run_raw_cl([(b"content-length", b"5"), (b"content-length", b"20971520")])) == 400
    assert asyncio.run(_run_raw_cl([(b"content-length", b"20971520"), (b"content-length", b"20971520")])) == 413


def test_csrf_guard_across_cookie_unconfigured_and_exemptions(tmp_path: Path):
    # 1) 未设密码本机模式下, 跨站 POST 仍被 403 拦截
    with TestClient(main_app, client=("127.0.0.1", 12345), base_url="http://localhost:3018") as client:
        assert client.post(
            "/api/auth/setup",
            json={"password": "pw-setup-123"},
            headers={"Origin": "https://evil.example"},
        ).status_code == 403
        assert client.post(
            "/api/auth/setup",
            json={"password": "pw-setup-123"},
            headers={"Origin": "null"},
        ).status_code == 403
        assert client.post(
            "/api/auth/setup",
            json={"password": "pw-setup-123"},
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code == 403
        assert client.post(
            "/api/auth/setup",
            json={"password": "pw-setup-123"},
            headers={"Sec-Fetch-Site": "same-site"},
        ).status_code == 403
        assert client.post(
            "/api/auth/setup",
            json={"password": "pw-setup-123"},
            headers={"Referer": "https://evil.example/attack.html"},
        ).status_code == 403

        # 同源 Origin + Sec-Fetch-Site=same-origin 放行
        assert client.post(
            "/api/auth/setup",
            json={"password": "pw-setup-123"},
            headers={"Origin": "http://localhost:3018", "Sec-Fetch-Site": "same-origin"},
        ).status_code == 200

        # 2) 已设密码后: 登录并测试 cookie 会话与双提交可选头
        assert client.post(
            "/api/auth/login",
            json={"password": "pw-setup-123"},
            headers={"Origin": "http://localhost:3018", "Sec-Fetch-Site": "same-origin"},
        ).status_code == 200

        assert client.post(
            "/api/auth/logout",
            headers={"Origin": "https://evil.example"},
        ).status_code == 403
        # 协议不匹配 (https vs http) 拦截
        assert client.post(
            "/api/auth/logout",
            headers={"Origin": "https://localhost:3018"},
        ).status_code == 403

        # 可信反代 X-Forwarded-Proto/Host 匹配时放行
        assert client.post(
            "/api/auth/logout",
            headers={
                "Origin": "https://panel.example.com",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "panel.example.com",
                "Sec-Fetch-Site": "same-origin",
            },
        ).status_code == 200

        # 携带 tf_csrf cookie 时校验 X-CSRF-Token (含重复多值拦截)
        client.post("/api/auth/login", json={"password": "pw-setup-123"})
        client.cookies.set("tf_csrf", "csrf-secret")
        assert client.post("/api/auth/logout").status_code == 403
        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert client.post(
            "/api/auth/logout",
            headers=[
                ("x-csrf-token", "csrf-secret"),
                ("x-csrf-token", "wrong"),
            ],
        ).status_code == 403
        assert client.post(
            "/api/auth/logout",
            headers=[
                ("x-csrf-token", "csrf-secret"),
                ("x-csrf-token", "csrf-secret"),
            ],
        ).status_code == 403
        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "csrf-secret"}).status_code == 200

        # 3) Bearer Token 通道豁免浏览器 CSRF 头要求 (非 /api/auth/ 路径), 重复 Authorization 不豁免
        _, tok = api_tokens.create_token(tmp_path, "bot", ["paper:trade"])
        r_bearer = client.post(
            "/api/events/ticket",
            headers={
                "Authorization": f"Bearer {tok}",
                "Origin": "https://evil.example",
            },
        )
        assert r_bearer.status_code != 403 or "跨" not in r_bearer.text

        r_dup_auth = client.post(
            "/api/events/ticket",
            headers=[
                ("authorization", f"Bearer {tok}"),
                ("authorization", f"Bearer {tok}"),
                ("origin", "https://evil.example"),
            ],
        )
        assert r_dup_auth.status_code == 403

        # /api/auth/* 路径不豁免 Bearer CSRF 校验
        assert client.post(
            "/api/auth/logout",
            headers={
                "Authorization": f"Bearer {tok}",
                "Origin": "https://evil.example",
            },
        ).status_code == 403

        # 4) 重复多值 Origin / Sec-Fetch-Site / Referer 头纵深防御
        client.cookies.clear()
        assert client.post(
            "/api/auth/logout",
            headers=[
                ("origin", "http://localhost:3018"),
                ("origin", "https://evil.example"),
            ],
        ).status_code == 403
        assert client.post(
            "/api/auth/logout",
            headers=[
                ("sec-fetch-site", "same-origin"),
                ("sec-fetch-site", "cross-site"),
            ],
        ).status_code == 403
        assert client.post(
            "/api/auth/logout",
            headers=[
                ("referer", "http://localhost:3018/"),
                ("referer", "https://evil.example/"),
            ],
        ).status_code == 403
        # 完全相同的重复同源 Origin 仍正常放行
        assert client.post(
            "/api/auth/logout",
            headers=[
                ("origin", "http://localhost:3018"),
                ("origin", "http://localhost:3018"),
            ],
        ).status_code == 200

        # 5) 重复 Authorization 头在 GET 与同源 POST 上一致返回 401
        assert client.post(
            "/api/auth/login",
            json={"password": "pw-setup-123"},
        ).status_code == 200
        assert client.get(
            "/api/kline/daily",
            headers=[
                ("authorization", f"Bearer {tok}"),
                ("authorization", f"Bearer {tok}"),
            ],
        ).status_code == 401
        assert client.get(
            "/api/kline/daily",
            headers=[
                ("authorization", f"Bearer {tok}"),
                ("authorization", "Basic dXNlcjpwYXNz"),
            ],
        ).status_code == 401
        assert client.post(
            "/api/events/ticket",
            headers=[
                ("authorization", f"Bearer {tok}"),
                ("authorization", f"Bearer {tok}"),
            ],
        ).status_code == 401

    # 6) 非 ASCII (latin-1 0x80..0xff) X-CSRF-Token / tf_csrf 返回 403 而非 TypeError 500
    req_nonascii_hdr = Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/auth/logout",
        "headers": [
            (b"cookie", b"tf_csrf=validtoken"),
            (b"x-csrf-token", b"\xff"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    })
    csrf_resp_hdr = _verify_csrf(req_nonascii_hdr)
    assert csrf_resp_hdr is not None and csrf_resp_hdr.status_code == 403

    req_nonascii_cookie = Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/auth/logout",
        "headers": [
            (b"cookie", b"tf_csrf=\xff"),
            (b"x-csrf-token", b"validtoken"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    })
    csrf_resp_cookie = _verify_csrf(req_nonascii_cookie)
    assert csrf_resp_cookie is not None and csrf_resp_cookie.status_code == 403

    # 端到端 ASGI 确认非 ASCII X-CSRF-Token 返回 403 而非 500
    async def _run_nonascii_csrf_asgi() -> int | None:
        status_out: int | None = None
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            nonlocal status_out
            if msg["type"] == "http.response.start":
                status_out = msg["status"]

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/logout",
            "raw_path": b"/api/auth/logout",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"localhost:3018"),
                (b"origin", b"http://localhost:3018"),
                (b"cookie", b"tf_csrf=validtoken"),
                (b"x-csrf-token", b"\xff"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 3018),
        }
        await main_app(scope, receive, send)
        return status_out

    assert asyncio.run(_run_nonascii_csrf_asgi()) == 403


def test_api_health_unmatched_api_404_and_docs_openapi_gating(tmp_path: Path):
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><html><body>SPA</body></html>", encoding="utf-8")

    # 独立 SPA 测试应用验证 unmatched /api/* JSON 404 与 405 保留
    spa_app = FastAPI()
    spa_app.middleware("http")(auth_middleware)
    spa_app.add_middleware(_DynamicCORSMiddleware)

    @spa_app.get("/api/health")
    def _health():
        return {"status": "ok"}

    @spa_app.get("/docs")
    def _docs():
        return {"docs": True}

    @spa_app.get("/redoc")
    def _redoc():
        return {"redoc": True}

    @spa_app.get("/openapi.json")
    def _openapi():
        return {"openapi": "3.1.0"}

    _register_static_routes(spa_app, static_dir)

    with TestClient(spa_app, client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/health").status_code == 405

        r_get_404 = client.get("/api/does-not-exist")
        assert r_get_404.status_code == 404
        assert r_get_404.json() == {"detail": "Not Found"}

        r_post_404 = client.post("/api/does-not-exist")
        assert r_post_404.status_code == 404
        assert r_post_404.json() == {"detail": "Not Found"}

        r_docs_404 = client.get("/api/docs")
        assert r_docs_404.status_code == 404
        assert r_docs_404.json() == {"detail": "Not Found"}

        # 非 /api/ 页面仍走 SPA
        assert client.get("/screener").status_code == 200

        # 已设密码后 /docs /redoc /openapi.json 返回 401
        auth_service.set_password("docs-pw-123")
        assert client.get("/docs").status_code == 401
        assert client.get("/redoc").status_code == 401
        assert client.get("/openapi.json").status_code == 401
        tok = auth_service.verify_and_create_session("docs-pw-123")
        assert tok
        client.cookies.set(auth_api.COOKIE_NAME, tok)
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

    # 主应用 /api/health 与 /api/openapi.json?tier=a vs tier=all
    auth_service.set_password("main-pw-123")
    with TestClient(main_app, client=("127.0.0.1", 12345)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/openapi.json").status_code == 200
        assert client.get("/api/openapi.json?tier=all").status_code == 401
        tok = auth_service.verify_and_create_session("main-pw-123")
        assert tok
        client.cookies.set(auth_api.COOKIE_NAME, tok)
        assert client.get("/api/openapi.json?tier=all").status_code == 200


# ================================================================
# 8. config_backup.py 流式上传上限 & _reload_states 全状态清理
# ================================================================

def test_config_backup_streaming_upload_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config_backup_svc, "MAX_ARCHIVE_BYTES", 1024)

    class _FakeUpload:
        def __init__(self, total_bytes: int):
            self.size = None
            self.read_calls = 0
            self.closed = False
            self._remaining = total_bytes

        async def read(self, n: int = -1) -> bytes:
            self.read_calls += 1
            if self._remaining <= 0:
                return b""
            take = min(self._remaining, n if n > 0 else self._remaining)
            self._remaining -= take
            return b"z" * take

        async def close(self) -> None:
            self.closed = True

    huge = _FakeUpload(1024 * 1024 * 10)  # 10 MiB virtual stream
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(config_backup_api._read_upload(huge))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400
    assert huge.closed is True
    assert huge.read_calls <= 2  # 首片即中止, 绝不读取全流

    ok_file = _FakeUpload(512)
    data = asyncio.run(config_backup_api._read_upload(ok_file))  # type: ignore[arg-type]
    assert len(data) == 512
    assert ok_file.closed is True


def test_config_backup_reload_states_clears_auth_secrets_clients_and_watchlist(tmp_path: Path):
    # 构造含 auth.json 与 secrets.json 的备份并写入当前状态
    auth_service.set_password("old-pw-123")
    old_session = auth_service.verify_and_create_session("old-pw-123")
    assert old_session and auth_service.is_valid_session(old_session)

    secrets_store.save({"ai_api_key": "sk-old"})
    secrets_store.load()
    assert secrets_store._cache is not None

    preferences.save({"quote_interval": 3})
    preferences.load()
    assert preferences._cache is not None

    assistant_sessions._cache = {"sessions": []}
    assistant_sessions._cache_sig = (1, 1)

    tf_client._sync_client = object()  # type: ignore[assignment]
    tf_client._async_client = object()  # type: ignore[assignment]
    tf_client._paid_realtime_client = object()  # type: ignore[assignment]

    rev_before = watchlist.revision()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": config_backup_svc.BACKUP_FORMAT,
            "version": config_backup_svc.BACKUP_VERSION,
            "include_secrets": True,
        }))
        zf.writestr("user_data/auth.json", json.dumps({
            "password_hash": "00" * 32,
            "password_salt": "11" * 16,
            "sessions": {"imported_tok": time.time() + 3600},
        }))
        zf.writestr("user_data/secrets.json", json.dumps({"ai_api_key": "sk-restored"}))
        zf.writestr("user_data/preferences.json", json.dumps({"quote_interval": 9}))
        zf.writestr("user_data/assistant_sessions.json", json.dumps({"sessions": [], "activeId": None}))

    # 模拟精简 app.state (不含 repo / monitor_engine), 验证不抛 AttributeError
    mini_app = FastAPI()
    mini_app.include_router(config_backup_api.router)
    mini_app.state.strategy_engine = SimpleNamespace(reload=lambda: None)

    with TestClient(mini_app) as client:
        resp = client.post(
            "/api/config-backup/apply",
            files={"file": ("backup.zip", buf.getvalue(), "application/zip")},
            data={"allow_overwrite": "true", "categories": "auth,secrets,preferences,history"},
        )
        assert resp.status_code == 200, resp.text

    # 旧会话和导入的会话均已失效, 必须重新登录
    assert auth_service.is_valid_session(old_session) is False
    assert auth_service.is_valid_session("imported_tok") is False
    assert len(auth_service._sessions) == 0

    # secrets_store / preferences / assistant_sessions 缓存已清, tf_client 单例已重置
    assert secrets_store._cache is None
    assert secrets_store.get_ai_key() == "sk-restored"
    assert preferences._cache is None
    assert assistant_sessions._cache is None
    assert tf_client._sync_client is None
    assert tf_client._async_client is None
    assert tf_client._paid_realtime_client is None

    # watchlist reload_hint 递增 revision (含无 repo 兼容)
    req = _make_fake_request()
    req.scope["app"] = mini_app
    config_backup_api._reload_states(req, {"reload_hints": {"watchlist": True}, "categories": []})
    assert watchlist.revision() == rev_before + 1


def test_config_backup_reload_states_invokes_populated_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from app.strategy import monitor_rules, paper_watch

    calls: dict[str, object] = {"strategy_reload": 0, "set_rules": None, "paper_watch_sync": 0}
    sample_rules = [{"id": "rule_1", "name": "示例规则", "type": "price"}]

    class _FakeStrategyEngine:
        def reload(self) -> None:
            calls["strategy_reload"] = int(calls["strategy_reload"]) + 1

    class _FakeMonitorEngine:
        def __init__(self) -> None:
            self.rules: dict[str, dict] = {}

        def set_rules(self, rules: list[dict]) -> None:
            calls["set_rules"] = list(rules)
            self.rules = {str(r["id"]): r for r in rules}

    strategy_engine = _FakeStrategyEngine()
    monitor_engine = _FakeMonitorEngine()
    populated_app = FastAPI()
    populated_app.state.strategy_engine = strategy_engine
    populated_app.state.monitor_engine = monitor_engine
    populated_app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    monkeypatch.setattr(monitor_rules, "load_all", lambda d: sample_rules if d == tmp_path else [])

    def _fake_sync(data_dir: Path, mon, strat):
        assert data_dir == tmp_path
        assert mon is monitor_engine
        assert strat is strategy_engine
        calls["paper_watch_sync"] = int(calls["paper_watch_sync"]) + 1
        return {"added": [], "removed": [], "watching": []}

    monkeypatch.setattr(paper_watch, "sync", _fake_sync)

    auth_service.set_password("populated-pw-123")
    sess = auth_service.verify_and_create_session("populated-pw-123")
    assert sess and auth_service.is_valid_session(sess)
    secrets_store._cache = {"ai_api_key": "stale"}
    secrets_store._cache_sig = (1, 1)
    tf_client._sync_client = object()  # type: ignore[assignment]
    rev_before = watchlist.revision()

    req = _make_fake_request()
    req.scope["app"] = populated_app
    config_backup_api._reload_states(
        req,
        {
            "reload_hints": {"strategies": True, "monitor_rules": True, "watchlist": True},
            "categories": [{"key": "auth", "imported": 1}, {"key": "secrets", "imported": 1}],
            "secrets_merged": True,
        },
    )

    assert calls["strategy_reload"] == 1
    assert calls["set_rules"] == sample_rules
    assert calls["paper_watch_sync"] == 1
    assert auth_service.is_valid_session(sess) is False
    assert secrets_store._cache is None
    assert tf_client._sync_client is None
    assert watchlist.revision() == rev_before + 1

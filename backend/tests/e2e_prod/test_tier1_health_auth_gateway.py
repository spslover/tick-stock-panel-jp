"""Tier 1 Feature Coverage: Area 1 (Health & Observability) & Area 2 (Auth & Open API Gateway)."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .helpers import create_api_token, seed_repo_market_data, setup_and_login

# =============================================================================
# Area 1: Health, Readiness & Observability
# =============================================================================


def test_health_endpoint_returns_process_and_memory_telemetry(e2e_client: TestClient) -> None:
    resp = e2e_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]
    assert body["mode"] in {"none", "free", "api_key"}
    mem = body["memory"]
    assert isinstance(mem["rss_bytes"], int) and mem["rss_bytes"] > 0
    assert isinstance(mem["system_total_bytes"], int) and mem["system_total_bytes"] > 0
    assert isinstance(mem["effective_available_bytes"], int) and mem["effective_available_bytes"] >= 0


def test_api_health_whitelisted_after_password_setup(e2e_client: TestClient) -> None:
    setup_and_login(e2e_client)
    # Clear cookies to prove /api/health bypasses auth_middleware
    e2e_client.cookies.clear()
    resp = e2e_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert "memory" in body and body["memory"]["rss_bytes"] > 0


def test_data_workload_records_request_groups(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    r_kline = e2e_client.get("/api/kline/daily", params={"symbol": "000001.SZ", "days": 20})
    assert r_kline.status_code == 200

    resp = e2e_client.get("/api/data/workload")
    assert resp.status_code == 200
    body = resp.json()
    assert "resources" in body
    resources = body["resources"]
    for key in ("heavy_jobs", "strategy_queue", "strategy_results", "minute_results"):
        assert key in resources
    assert "workload" in body or "groups" in body or "http.kline" in json.dumps(body)


@pytest.mark.r3_post_fix
def test_health_live_probe(e2e_client: TestClient) -> None:
    resp = e2e_client.get("/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") in {"ok", "alive", "live"}


@pytest.mark.r3_post_fix
def test_health_ready_probe_when_ready(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    e2e_client.app.state.indicators_ready = True
    resp = e2e_client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") in {"ok", "ready"}


@pytest.mark.r3_post_fix
def test_metrics_prometheus_exposition(e2e_client: TestClient) -> None:
    e2e_client.get("/health")
    resp = e2e_client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    text = resp.text
    assert "# TYPE" in text or "# HELP" in text
    assert "http_" in text or "process_" in text or "tsp_" in text


@pytest.mark.r3_post_fix
def test_request_id_header_generated_and_echoed(e2e_client: TestClient) -> None:
    r1 = e2e_client.get("/health")
    assert r1.status_code == 200
    rid = r1.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8

    custom_id = "e2e-corr-id-0001"
    r2 = e2e_client.get("/health", headers={"X-Request-ID": custom_id})
    assert r2.status_code == 200
    assert r2.headers.get("X-Request-ID") == custom_id


@pytest.mark.r3_post_fix
def test_audit_jsonl_records_auth_and_token_events(
    e2e_client: TestClient,
    isolated_data_dir: Path,
) -> None:
    setup_and_login(e2e_client)
    tok, _ = create_api_token(e2e_client, "audit_tok", ["read:market"])
    r_del = e2e_client.delete(f"/api/settings/api-tokens/{tok['id']}")
    assert r_del.status_code == 200

    audit_path = isolated_data_dir / "logs" / "audit.jsonl"
    assert audit_path.is_file(), f"Missing audit log at {audit_path}"
    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 3
    events_text = json.dumps(lines, ensure_ascii=False)
    assert "audit_tok" in events_text or tok["id"] in events_text


# =============================================================================
# Area 2: Dual Auth, Open API Gateway & Security Boundary
# =============================================================================


def test_auth_setup_login_status_logout_lifecycle(e2e_client: TestClient) -> None:
    st0 = e2e_client.get("/api/auth/status")
    assert st0.status_code == 200
    assert st0.json() == {"configured": False, "authenticated": False}

    # Login before setup must fail 409
    r_pre = e2e_client.post("/api/auth/login", json={"password": "Password123!"})
    assert r_pre.status_code == 409

    # Setup
    r_setup = e2e_client.post("/api/auth/setup", json={"password": "Password123!"})
    assert r_setup.status_code == 200
    assert r_setup.json() == {"ok": True, "configured": True}

    # Duplicate setup rejected 409
    r_dup = e2e_client.post("/api/auth/setup", json={"password": "Password456!"})
    assert r_dup.status_code == 409

    # Unauthenticated protected API returns 401
    assert e2e_client.get("/api/data/status").status_code == 401

    # Login sets HttpOnly SameSite=lax cookie
    r_login = e2e_client.post("/api/auth/login", json={"password": "Password123!"})
    assert r_login.status_code == 200
    assert r_login.json() == {"ok": True, "authenticated": True}
    set_cookie = r_login.headers.get("set-cookie", "").lower()
    assert "tf_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie

    # Authenticated status & protected route pass
    st1 = e2e_client.get("/api/auth/status").json()
    assert st1 == {"configured": True, "authenticated": True}
    assert e2e_client.get("/api/data/status").status_code == 200

    # Logout revokes session
    r_out = e2e_client.post("/api/auth/logout")
    assert r_out.status_code == 200
    assert e2e_client.get("/api/auth/status").json() == {"configured": True, "authenticated": False}
    assert e2e_client.get("/api/data/status").status_code == 401


def test_auth_change_password_revokes_existing_sessions(e2e_client: TestClient) -> None:
    old_cookie = setup_and_login(e2e_client, password="OldPassword1!")

    # Wrong old password returns 401
    r_bad = e2e_client.post(
        "/api/auth/change-password",
        json={"old_password": "WrongPassword!", "new_password": "NewPassword2!"},
    )
    assert r_bad.status_code == 401

    # Correct change invalidates all sessions
    r_ok = e2e_client.post(
        "/api/auth/change-password",
        json={"old_password": "OldPassword1!", "new_password": "NewPassword2!"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["ok"] is True

    # Old session cookie is rejected
    assert e2e_client.get("/api/data/status", cookies={"tf_session": old_cookie}).status_code == 401
    # Old password fails, new password succeeds
    assert e2e_client.post("/api/auth/login", json={"password": "OldPassword1!"}).status_code == 401
    assert e2e_client.post("/api/auth/login", json={"password": "NewPassword2!"}).status_code == 200


def test_api_tokens_crud_and_plaintext_not_persisted(
    e2e_client: TestClient,
    isolated_data_dir: Path,
) -> None:
    setup_and_login(e2e_client)

    # Unknown scope rejected 400
    r_bad = e2e_client.post("/api/settings/api-tokens", json={"name": "bad", "scopes": ["admin"]})
    assert r_bad.status_code == 400

    tok, plaintext = create_api_token(e2e_client, "quant_bot", ["read:market"])
    assert plaintext.startswith("tsp_") and len(plaintext) > 40
    assert tok["id"].startswith("tok_")
    assert tok["scopes"] == ["read:market"]
    assert tok["revoked"] is False

    # Plaintext never appears in list or on disk
    r_list = e2e_client.get("/api/settings/api-tokens")
    assert r_list.status_code == 200
    tokens = r_list.json()["tokens"]
    assert len(tokens) == 1
    assert "token_hash" not in tokens[0]
    assert plaintext not in json.dumps(tokens)

    disk_raw = (isolated_data_dir / "user_data" / "api_tokens.json").read_text(encoding="utf-8")
    assert plaintext not in disk_raw

    # Revoke immediately blocks Bearer access
    r_rev = e2e_client.delete(f"/api/settings/api-tokens/{tok['id']}")
    assert r_rev.status_code == 200
    assert r_rev.json() == {"status": "revoked", "id": tok["id"]}

    e2e_client.cookies.clear()
    r_use = e2e_client.get("/api/kline/daily?symbol=000001.SZ", headers={"Authorization": f"Bearer {plaintext}"})
    assert r_use.status_code == 401


def test_open_api_gateway_enforces_all_6_scopes(e2e_client: TestClient) -> None:
    data_dir = e2e_client.app.state.repo.store.data_dir
    seed_repo_market_data(data_dir)
    setup_and_login(e2e_client)

    # Create ext table for read:ext / write:ext
    r_ext = e2e_client.post(
        "/api/ext-data",
        json={
            "id": "scope_tbl",
            "label": "Scope Table",
            "mode": "snapshot",
            "fields": [{"name": "score", "dtype": "float", "label": "Score"}],
        },
    )
    assert r_ext.status_code in (200, 201), r_ext.text

    scope_tokens: dict[str, str] = {}
    for sc in ("read:market", "read:ext", "write:ext", "read:analysis", "run:backtest", "paper:trade"):
        _, pt = create_api_token(e2e_client, f"tok_{sc}", [sc])
        scope_tokens[sc] = pt

    # Clear session cookies so requests rely solely on Bearer tokens
    e2e_client.cookies.clear()

    # 1. read:market -> kline daily 200; ext-data rows 403
    assert e2e_client.get(
        "/api/kline/daily",
        params={"symbol": "000001.SZ", "days": 20},
        headers={"Authorization": f"Bearer {scope_tokens['read:market']}"},
    ).status_code == 200
    r_deny = e2e_client.get(
        "/api/ext-data/scope_tbl/rows",
        headers={"Authorization": f"Bearer {scope_tokens['read:market']}"},
    )
    assert r_deny.status_code == 403
    assert "read:ext" in r_deny.json()["detail"]

    # 2. write:ext -> ingest 200; rows 403; create table 403
    assert e2e_client.post(
        "/api/ext-data/scope_tbl/ingest",
        json={"rows": [{"symbol": "000001.SZ", "score": 1.5}]},
        headers={"Authorization": f"Bearer {scope_tokens['write:ext']}"},
    ).status_code == 200
    assert e2e_client.get(
        "/api/ext-data/scope_tbl/rows",
        headers={"Authorization": f"Bearer {scope_tokens['write:ext']}"},
    ).status_code == 403

    # 3. read:ext -> rows 200; api-key 403 (excluded from Tier A)
    r_rows = e2e_client.get(
        "/api/ext-data/scope_tbl/rows",
        headers={"Authorization": f"Bearer {scope_tokens['read:ext']}"},
    )
    assert r_rows.status_code == 200
    assert len(r_rows.json()["rows"]) == 1
    assert e2e_client.get(
        "/api/ext-data/scope_tbl/api-key",
        headers={"Authorization": f"Bearer {scope_tokens['read:ext']}"},
    ).status_code == 403

    # 4. read:analysis -> backtest candidates 200; backtest run 403
    assert e2e_client.get(
        "/api/backtest/candidates",
        headers={"Authorization": f"Bearer {scope_tokens['read:analysis']}"},
    ).status_code == 200
    assert e2e_client.post(
        "/api/backtest/run",
        json={},
        headers={"Authorization": f"Bearer {scope_tokens['read:analysis']}"},
    ).status_code == 403

    # 5. run:backtest -> backtest status (Wait: status is read:analysis, run is run:backtest)
    assert e2e_client.get(
        "/api/backtest/status",
        headers={"Authorization": f"Bearer {scope_tokens['run:backtest']}"},
    ).status_code == 403

    # 6. paper:trade -> paper accounts 200; settings/api-tokens 403
    assert e2e_client.get(
        "/api/paper/accounts",
        headers={"Authorization": f"Bearer {scope_tokens['paper:trade']}"},
    ).status_code == 200
    assert e2e_client.get(
        "/api/settings/api-tokens",
        headers={"Authorization": f"Bearer {scope_tokens['paper:trade']}"},
    ).status_code == 403


def test_openapi_tier_a_exposes_exact_69_routes(e2e_client: TestClient) -> None:
    setup_and_login(e2e_client)
    e2e_client.cookies.clear()
    # Tier A is whitelisted without session
    r = e2e_client.get("/api/openapi.json?tier=a")
    assert r.status_code == 200
    spec = r.json()
    assert spec.get("x-tier") == "a"
    endpoints = {
        f"{m.upper()} {p}"
        for p, ops in spec.get("paths", {}).items()
        for m in ops
        if m in {"get", "post", "put", "delete", "patch"}
    }
    assert len(endpoints) == 69
    assert "GET /api/settings/api-tokens" not in endpoints
    assert "GET /api/ext-data/{config_id}/api-key" not in endpoints
    assert "POST /api/paper/orders" in endpoints

    # Full spec requires auth once password is set
    assert e2e_client.get("/api/openapi.json?tier=full").status_code == 401


def test_open_api_rate_limit_429_and_headers(
    e2e_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPEN_API_RATE_LIMIT_PER_MIN", "3")
    setup_and_login(e2e_client)
    _, pt = create_api_token(e2e_client, "rl_tok", ["read:analysis"])

    hdr = {"Authorization": f"Bearer {pt}"}
    for expected_rem in ("2", "1", "0"):
        r = e2e_client.get("/api/backtest/candidates", headers=hdr)
        assert r.status_code == 200
        assert r.headers.get("X-RateLimit-Limit") == "3"
        assert r.headers.get("X-RateLimit-Remaining") == expected_rem

    r_limited = e2e_client.get("/api/backtest/candidates", headers=hdr)
    assert r_limited.status_code == 429
    assert int(r_limited.headers["Retry-After"]) >= 1

    # UI session cookie path is unaffected by Token rate limit bucket
    r_ui = e2e_client.get("/api/backtest/candidates")
    assert r_ui.status_code == 200


def test_api_token_local_only_gate_and_public_unconfigured_block(
    make_e2e_client: Callable[..., TestClient],
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Public peer before setup -> 403 NOT_INITIALIZED
    with make_e2e_client(client_host="203.0.113.50", data_dir=isolated_data_dir.parent / "pub_data") as pub_client:
        r_uninit = pub_client.get("/api/data/status")
        assert r_uninit.status_code == 403
        assert r_uninit.json()["code"] == "NOT_INITIALIZED"
        assert pub_client.post("/api/auth/setup", json={"password": "Password123!"}).status_code == 403

    # Setup via local client, then simulate public client behind trusted loopback proxy
    local_client = make_e2e_client(client_host="127.0.0.1")
    setup_and_login(local_client)
    _, pt = create_api_token(local_client, "net_tok", ["read:analysis"])
    local_client.cookies.clear()

    # Public peer with Bearer token rejected 403 when API_TOKEN_LOCAL_ONLY=1 (even with bogus token)
    monkeypatch.setenv("API_TOKEN_LOCAL_ONLY", "1")
    r_pub_bad = local_client.get(
        "/api/backtest/candidates",
        headers={"Authorization": "Bearer tsp_bogus", "X-Forwarded-For": "203.0.113.50"},
    )
    assert r_pub_bad.status_code == 403
    r_pub_good = local_client.get(
        "/api/backtest/candidates",
        headers={"Authorization": f"Bearer {pt}", "X-Forwarded-For": "203.0.113.50"},
    )
    assert r_pub_good.status_code == 403

    # Opt-out via API_TOKEN_LOCAL_ONLY=0 allows valid Bearer token from public IP
    monkeypatch.setenv("API_TOKEN_LOCAL_ONLY", "0")
    r_pub_allow = local_client.get(
        "/api/backtest/candidates",
        headers={"Authorization": f"Bearer {pt}", "X-Forwarded-For": "203.0.113.50"},
    )
    assert r_pub_allow.status_code == 200

"""审计加固回归测试: 健康探针、Prometheus 指标、X-Request-ID、脱敏 audit.jsonl 与受管异步任务。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import observability as obs


def test_health_live_ready_and_metrics_endpoints(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        r_live = client.get("/health/live")
        assert r_live.status_code == 200
        assert r_live.json()["status"] == "ok"

        r_ready = client.get("/health/ready")
        assert r_ready.status_code == 200
        body = r_ready.json()
        assert body["status"] == "ready"
        assert body["checks"]["repo"] is True
        assert body["checks"]["data_dir"] is True

        # Degraded readiness when indicators_ready is False
        app.state.indicators_ready = False
        try:
            r_not_ready = client.get("/health/ready")
            assert r_not_ready.status_code == 503
            assert r_not_ready.json()["status"] == "not_ready"
        finally:
            app.state.indicators_ready = True

        r_metrics = client.get("/metrics")
        assert r_metrics.status_code == 200
        assert "text/plain" in r_metrics.headers.get("content-type", "")
        assert "# HELP tsp_http_requests_total" in r_metrics.text
        assert "# TYPE tsp_http_requests_total counter" in r_metrics.text
        assert "process_resident_memory_bytes" in r_metrics.text

    # Public/unauthenticated client must not scrape /metrics
    with TestClient(app, client=("203.0.113.10", 50000)) as public_client:
        r_pub_metrics = public_client.get("/metrics")
        assert r_pub_metrics.status_code in (401, 403)


def test_request_id_generated_echoed_and_sanitized(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    with TestClient(app) as client:
        r1 = client.get("/health/live")
        rid1 = r1.headers.get("X-Request-ID")
        assert rid1 and len(rid1) >= 16

        r2 = client.get("/health/live", headers={"X-Request-ID": "audit-trace-001"})
        assert r2.headers.get("X-Request-ID") == "audit-trace-001"

        # CRLF / injection payload must be rejected and replaced with a fresh ID
        r3 = client.get("/health/live", headers={"X-Request-ID": "bad\r\nid with spaces"})
        rid3 = r3.headers.get("X-Request-ID")
        assert rid3 and rid3 != "bad\r\nid with spaces"
        assert "\r" not in rid3 and "\n" not in rid3 and " " not in rid3


def test_audit_jsonl_redacts_secrets(tmp_path: Path) -> None:
    obs.record_audit_event(
        "api_token.create",
        actor="session",
        target="tok_abc123",
        outcome="ok",
        client_ip="127.0.0.1",
        detail={
            "token_id": "tok_abc123",
            "token_name": "quant-bot",
            "scopes": ["read:market"],
            "password": "SuperSecretPassword!",
            "plaintext": "tsp_1234567890abcdef1234567890abcdef",
            "note": "Authorization: Bearer secret_bearer_val",
        },
        data_dir=tmp_path,
    )
    audit_file = tmp_path / "logs" / "audit.jsonl"
    assert audit_file.is_file()
    raw = audit_file.read_text(encoding="utf-8")
    assert "SuperSecretPassword!" not in raw
    assert "tsp_1234567890abcdef1234567890abcdef" not in raw
    assert "secret_bearer_val" not in raw

    entry = json.loads(raw.splitlines()[-1])
    assert entry["action"] == "api_token.create"
    assert entry["detail"]["token_id"] == "tok_abc123"
    assert entry["detail"]["token_name"] == "quant-bot"
    assert entry["detail"]["password"] == "[REDACTED]"
    assert entry["detail"]["plaintext"] == "[REDACTED]"


def test_spawn_background_task_tracks_and_cleans_up() -> None:
    async def _run() -> int:
        done_flag = False

        async def _job() -> None:
            nonlocal done_flag
            await asyncio.sleep(0.01)
            done_flag = True

        task = obs.spawn_background_task(_job(), name="test-bg-task")
        assert task in obs._BACKGROUND_TASKS
        await task
        await asyncio.sleep(0)
        assert done_flag is True
        assert task not in obs._BACKGROUND_TASKS
        return obs.active_background_task_count()

    remaining = asyncio.run(_run())
    assert remaining >= 0

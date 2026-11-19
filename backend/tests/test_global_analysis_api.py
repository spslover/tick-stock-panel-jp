"""API and streaming AI analyzer tests for the JP/US global_analysis extension."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.extensions.loader import configure_backend_extensions
from app.services import global_analyzer, global_market_data, global_reports
from tests.test_global_market_data import _FakeProvider


@pytest.fixture()
def global_client(tmp_path: Path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    global_market_data.clear_cache()
    prov = _FakeProvider()
    monkeypatch.setattr(global_market_data, "_default_provider", prov)

    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert "official.global-analysis" in registry.extension_ids()
    assert not [e for e in errors if "global_analysis" in e.module]

    with TestClient(app) as client:
        yield client, prov


def test_overview_search_stock_and_invalid_market(global_client) -> None:
    client, _ = global_client

    r_jp = client.get("/api/global-analysis/overview?market=JP")
    assert r_jp.status_code == 200
    assert r_jp.json()["market"] == "JP"
    assert len(r_jp.json()["benchmarks"]) == 4

    r_us = client.get("/api/global-analysis/overview?market=US")
    assert r_us.status_code == 200
    assert r_us.json()["market"] == "US"

    r_bad = client.get("/api/global-analysis/overview?market=HK")
    assert r_bad.status_code == 400

    r_search = client.get("/api/global-analysis/search?market=JP&q=7203")
    assert r_search.status_code == 200
    assert any(x["symbol"] == "7203.T" for x in r_search.json()["items"])

    r_stock = client.get("/api/global-analysis/stock?market=JP&symbol=7203")
    assert r_stock.status_code == 200
    body = r_stock.json()
    assert body["symbol"] == "7203.T"
    assert len(body["rows"]) == 90
    assert "levels" in body


@pytest.mark.parametrize("locale,expected_phrase", [
    ("en", "English"),
    ("ja", "日本語"),
    ("zh-CN", "简体中文"),
])
def test_stream_market_and_stock_analysis_respects_locale_and_focus(
    global_client, monkeypatch, locale: str, expected_phrase: str,
) -> None:
    client, _ = global_client
    captured_messages: list[list[dict]] = []

    async def fake_stream(messages, **_kwargs):
        captured_messages.append(messages)
        yield "# Report\n\n"
        yield "Objective summary."

    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", fake_stream)

    r_mkt = client.post(
        "/api/global-analysis/analyze/market",
        json={"market": "JP", "focus": "USDJPY impact", "locale": locale},
    )
    assert r_mkt.status_code == 200
    events = [json.loads(line) for line in r_mkt.text.strip().splitlines() if line.strip()]
    assert events[0]["type"] == "meta"
    assert events[0]["market"] == "JP"
    assert any(e["type"] == "delta" for e in events)
    assert events[-1]["type"] == "done"

    sys_msg = captured_messages[-1][0]["content"]
    user_msg = captured_messages[-1][1]["content"]
    assert expected_phrase in sys_msg
    assert "USDJPY impact" in user_msg

    r_stk = client.post(
        "/api/global-analysis/analyze/stock",
        json={"market": "US", "symbol": "NVDA", "focus": "", "locale": locale},
    )
    assert r_stk.status_code == 200
    stk_events = [json.loads(line) for line in r_stk.text.strip().splitlines() if line.strip()]
    assert stk_events[0]["type"] == "meta"
    assert stk_events[0]["symbol"] == "NVDA"
    assert stk_events[-1]["type"] == "done"
    assert expected_phrase in captured_messages[-1][0]["content"]


def test_reports_crud_roundtrip(global_client) -> None:
    client, _ = global_client

    r_empty = client.get("/api/global-analysis/reports")
    assert r_empty.status_code == 200
    assert r_empty.json()["reports"] == []

    r_save = client.post(
        "/api/global-analysis/reports",
        json={
            "market": "JP",
            "kind": "stock",
            "symbol": "7203.T",
            "name": "Toyota Motor",
            "focus": "",
            "locale": "ja",
            "content": "# Toyota Report",
            "summary": "3400 JPY",
            "currency": "JPY",
            "close": 3400.0,
        },
    )
    assert r_save.status_code == 200
    rid = r_save.json()["report"]["id"]
    assert rid.startswith("gar_")

    r_list = client.get("/api/global-analysis/reports")
    assert len(r_list.json()["reports"]) == 1

    r_del = client.delete(f"/api/global-analysis/reports/{rid}")
    assert r_del.status_code == 200
    assert r_del.json()["ok"] is True
    assert global_reports.list_reports() == []


def test_normalize_locale() -> None:
    assert global_analyzer.normalize_locale("en") == "en"
    assert global_analyzer.normalize_locale("en-US") == "en"
    assert global_analyzer.normalize_locale("ja") == "ja"
    assert global_analyzer.normalize_locale("ja-JP") == "ja"
    assert global_analyzer.normalize_locale("zh-CN") == "zh-CN"
    assert global_analyzer.normalize_locale(None) == "zh-CN"

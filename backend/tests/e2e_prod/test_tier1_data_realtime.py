"""Tier 1 Feature Coverage: Areas 3, 4, 5, 6 (Capability Routing, Kline/Integrity/PIT, Ext Data, Realtime/Depth5/SSE)."""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from .helpers import (
    create_api_token,
    make_adj_factor_df,
    make_daily_df,
    make_depth5_snapshot,
    make_minute_df,
    make_pit_financials,
    parse_sse_frames,
    seed_repo_market_data,
    setup_and_login,
    trading_days,
)

# =============================================================================
# Area 3: Multi-Source Capability Routing (7 Datasets)
# =============================================================================


def test_capability_matrix_exposes_all_7_datasets(e2e_client: TestClient) -> None:
    resp = e2e_client.get("/api/settings/capability-matrix")
    assert resp.status_code == 200
    body = resp.json()
    caps = body["capabilities"]
    ids = [c["id"] for c in caps]
    assert set(ids) == {"daily", "adj_factor", "realtime", "minute", "full_minute", "depth5", "financial"}
    for c in caps:
        assert "usable" in c and isinstance(c["usable"], bool)
        assert "current" in c and "effective" in c and "candidates" in c


def test_provider_routing_updates_capability_matrix_and_usable(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    mock_provider_factory(
        name="mock_cap_src",
        display_name="Mock Cap Source",
        datasets={"daily", "minute", "depth5", "financial"},
    )
    r_put = e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={
            "daily_data_provider": "mock_cap_src",
            "minute_data_provider": "mock_cap_src",
            "depth5_data_provider": "mock_cap_src",
            "financial_data_provider": "mock_cap_src",
        },
    )
    assert r_put.status_code == 200

    matrix = e2e_client.get("/api/settings/capability-matrix").json()["capabilities"]
    by_id = {c["id"]: c for c in matrix}
    for cap_id in ("daily", "minute", "depth5", "financial"):
        assert by_id[cap_id]["effective"] == "mock_cap_src"
        assert by_id[cap_id]["usable"] is True
    assert by_id["full_minute"]["usable"] is False


def test_provider_request_error_returns_502_fail_closed(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    from app.data_providers.base import ProviderRequestError

    mock_provider_factory(
        name="mock_err_src",
        datasets={"daily"},
        fail_datasets={"daily": ProviderRequestError("upstream 503")},
    )
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={"daily_data_provider": "mock_err_src"},
    )
    resp = e2e_client.post("/api/kline/sync", params={"symbol": "000001.SZ", "days": 30})
    assert resp.status_code == 502
    assert resp.json()["code"] == "provider_request"


def test_depth5_and_minute_never_silently_fallback_on_failure(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    from app.data_providers.base import ProviderRequestError

    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    mock_provider_factory(
        name="mock_fail_min_d5",
        datasets={"daily", "minute", "depth5"},
        fail_datasets={
            "minute": ProviderRequestError("minute upstream down"),
            "depth5": ProviderRequestError("depth5 upstream down"),
        },
    )
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={
            "daily_data_provider": "mock_fail_min_d5",
            "minute_data_provider": "mock_fail_min_d5",
            "depth5_data_provider": "mock_fail_min_d5",
        },
    )

    # Minute sync/fetch fails closed (502), does not silently fall back to TickFlow
    r_min = e2e_client.get("/api/kline/minute", params={"symbol": "000001.SZ"})
    assert r_min.status_code in (400, 403, 502)

    # DepthService run_once must not write fake sealed rows when depth5 fails
    ds = e2e_client.app.state.depth_service
    res = ds.run_once(symbols=["000001.SZ"])
    assert not res or res.get("ok") is False or res.get("count", 0) == 0


def test_custom_yaml_data_source_test_save_list_delete(
    e2e_client: TestClient,
    local_http_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAFE_HTTP_ALLOW_HOSTS", "127.0.0.1")
    local_http_server.set_json_route(
        "/daily",
        {
            "data": [
                {
                    "symbol": "000001.SZ",
                    "date": "2026-04-15",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.9,
                    "close": 10.2,
                    "volume": 100000,
                    "amount": 1020000,
                }
            ]
        },
    )
    payload = {
        "name": "e2e_http_yaml",
        "display_name": "E2E HTTP YAML",
        "datasets": {
            "daily": {
                "url": f"{local_http_server.base_url}/daily",
                "method": "GET",
                "response_path": "data",
                "field_map": {
                    "symbol": "symbol",
                    "date": "date",
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                    "amount": "amount",
                },
            }
        },
    }
    r_test = e2e_client.post("/api/settings/data-sources/test", json={**payload, "dataset": "daily"})
    assert r_test.status_code == 200, r_test.text
    assert r_test.json()["ok"] is True

    r_save = e2e_client.post("/api/settings/data-sources", json=payload)
    assert r_save.status_code == 200, r_save.text

    r_list = e2e_client.get("/api/settings/data-sources")
    assert r_list.status_code == 200
    assert any(s["name"] == "e2e_http_yaml" for s in r_list.json()["custom"])

    r_del = e2e_client.delete("/api/settings/data-sources/e2e_http_yaml")
    assert r_del.status_code == 200
    r_list2 = e2e_client.get("/api/settings/data-sources")
    assert all(s["name"] != "e2e_http_yaml" for s in r_list2.json()["custom"])


# =============================================================================
# Area 4: Kline, Integrity, Full-Minute & PIT Financials
# =============================================================================


def test_daily_sync_and_raw_vs_fwd_adj_enriched_prices(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    days = trading_days(30, end_date=date(2026, 4, 15))
    split_day = days[15]
    # Construct unadjusted prices where pre-split close is 20.00 and post-split is 10.00 (2:1 split)
    rows = []
    for d in days:
        px = 20.0 if d < split_day else 10.0
        rows.append({
            "symbol": "000001.SZ",
            "date": d,
            "open": px,
            "high": px * 1.02,
            "low": px * 0.98,
            "close": px,
            "volume": 1_000_000.0,
            "amount": 1_000_000.0 * px,
        })
    daily_df = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    adj_df = make_adj_factor_df(["000001.SZ"], days, split_date=split_day, split_factor=2.0)

    mock_provider_factory(
        name="mock_adj_src",
        datasets={"daily", "adj_factor"},
        daily_df=daily_df,
        adj_factor_df=adj_df,
    )
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={"daily_data_provider": "mock_adj_src", "adj_factor_provider": "mock_adj_src"},
    )
    r_sync = e2e_client.post("/api/kline/sync", params={"symbol": "000001.SZ", "days": 30})
    assert r_sync.status_code == 200
    assert r_sync.json()["rows_written"] == 30

    # Persist adj_factor & rebuild enriched
    from app.services.kline_sync import sync_adj_factor

    n_adj = sync_adj_factor(["000001.SZ"], e2e_client.app.state.repo, e2e_client.app.state.capabilities)
    assert n_adj >= 30
    e2e_client.post("/api/kline/rebuild_enriched")

    r_kline = e2e_client.get("/api/kline/daily", params={"symbol": "000001.SZ", "days": 30})
    assert r_kline.status_code == 200
    bars = r_kline.json()["bars"]
    assert len(bars) == 30
    # Pre-split bar: raw_close is 20.0, forward-adjusted close is 10.0 (20.0 * 1.0 / 2.0)
    pre_bar = bars[0]
    assert pre_bar["close"] == pytest.approx(10.0, abs=0.02)
    if "raw_close" in pre_bar and pre_bar["raw_close"] is not None:
        assert pre_bar["raw_close"] == pytest.approx(20.0, abs=0.02)


def test_minute_sync_and_beijing_wallclock_query(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    trade_day = date(2026, 4, 15)
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir, end_date=trade_day)
    min_df = make_minute_df(["000001.SZ"], trade_day, base_price=12.0, bars_per_symbol=240)
    mock_provider_factory(name="mock_min_src", datasets={"minute"}, minute_df=min_df)
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={"minute_data_provider": "mock_min_src"},
    )

    r_min = e2e_client.get(
        "/api/kline/minute",
        params={"symbol": "000001.SZ", "date": trade_day.isoformat(), "live": False},
    )
    assert r_min.status_code == 200, r_min.text
    bars = r_min.json()["bars"]
    assert len(bars) == 240
    # Beijing wall-clock times: starts 09:31, ends 15:00
    first_dt = str(bars[0]["datetime"])
    last_dt = str(bars[-1]["datetime"])
    assert "09:31" in first_dt
    assert "15:00" in last_dt


def test_daily_integrity_scan_409_block_and_repair_unblock(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    data_dir = e2e_client.app.state.repo.store.data_dir
    latest = seed_repo_market_data(data_dir, stock_symbols=["000001.SZ", "600000.SH"], n_days=40)

    # Corrupt daily partition by dropping one trading day for 000001.SZ
    days = trading_days(40, end_date=latest)
    missing_day = days[-3]
    part_files = list((data_dir / "kline_daily").rglob("*.parquet"))
    assert part_files
    for pf in part_files:
        df = pl.read_parquet(pf)
        filtered = df.filter(~((pl.col("symbol") == "000001.SZ") & (pl.col("date") == missing_day)))
        filtered.write_parquet(pf)

    # Mock provider has complete 40-day data for repair
    full_daily = make_daily_df(["000001.SZ", "600000.SH"], n_days=40, end_date=latest)
    mock_provider_factory(name="mock_repair_src", datasets={"daily"}, daily_df=full_daily)
    e2e_client.put("/api/settings/preferences/data-providers", json={"daily_data_provider": "mock_repair_src"})

    r_scan = e2e_client.post("/api/data/integrity/scan", json={"lookback_days": 30, "asset_types": ["stock"]})
    assert r_scan.status_code == 200
    job_id = r_scan.json()["job_id"]
    for _ in range(50):
        st = e2e_client.get(f"/api/pipeline/jobs/{job_id}").json()
        if st["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "completed"

    integ = e2e_client.get("/api/data/integrity").json()
    report = integ["report"]
    assert report is not None
    if report["repairable_count"] > 0:
        assert "stock" in integ["blocking_assets"]
        # Screener must be blocked with 409 daily_integrity
        r_scr = e2e_client.post("/api/screener/run", json={"strategy_id": "ma_golden_cross"})
        assert r_scr.status_code == 409
        assert r_scr.json()["code"] == "daily_integrity"

        r_rep = e2e_client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        assert r_rep.status_code == 200
        rep_job = r_rep.json()["job_id"]
        for _ in range(50):
            st2 = e2e_client.get(f"/api/pipeline/jobs/{rep_job}").json()
            if st2["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert st2["status"] == "completed"
        integ_after = e2e_client.get("/api/data/integrity").json()
        assert "stock" not in integ_after["blocking_assets"]


def test_full_minute_capability_gating_and_status(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    r_disabled = e2e_client.get("/api/settings/minute-refresh/status")
    assert r_disabled.status_code == 200
    assert r_disabled.json().get("allowed") is False

    mock_provider_factory(name="mock_full_min", datasets={"full_minute"})
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={"full_minute_data_provider": "mock_full_min"},
    )
    r_enabled = e2e_client.get("/api/settings/minute-refresh/status")
    assert r_enabled.status_code == 200
    assert r_enabled.json().get("allowed") is True


def test_financial_5_table_pit_sync_and_endpoints(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    pit = make_pit_financials("000001.SZ", period_end=date(2025, 12, 31), announce_date=date(2026, 3, 25))
    mock_provider_factory(
        name="mock_fin_src",
        datasets={"financial"},
        financial_tables=pit,
    )
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={"financial_data_provider": "mock_fin_src"},
    )

    from app.services.financial_sync import sync_financial_table

    data_dir = e2e_client.app.state.repo.store.data_dir
    for tbl in ("metrics", "income", "balance_sheet", "cash_flow", "shares"):
        res = sync_financial_table(data_dir, tbl, symbols=["000001.SZ"])
        assert res["rows"] >= 1

    for route in ("metrics", "income", "balance-sheet", "cash-flow", "shares"):
        r = e2e_client.get(f"/api/financials/{route}", params={"symbol": "000001.SZ"})
        assert r.status_code == 200, f"{route}: {r.text}"
        rows = r.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["symbol"] == "000001.SZ"
        assert str(rows[0]["announce_date"]).startswith("2026-03-25")


def test_financial_routes_fail_closed_403_without_capability(e2e_client: TestClient) -> None:
    r_st = e2e_client.get("/api/financials/status")
    assert r_st.status_code == 200
    assert r_st.json()["available"] is False

    for route in ("metrics", "income", "balance-sheet", "cash-flow", "shares"):
        r = e2e_client.get(f"/api/financials/{route}")
        assert r.status_code == 403


# =============================================================================
# Area 5: Extended Data Tables & HTTP Pull Engine
# =============================================================================


def test_ext_data_crud_ingest_and_duckdb_query(e2e_client: TestClient) -> None:
    r_create = e2e_client.post(
        "/api/ext-data",
        json={
            "id": "hot_rank",
            "label": "Hot Rank",
            "mode": "timeseries",
            "fields": [
                {"name": "heat", "dtype": "float", "label": "Heat"},
                {"name": "tag", "dtype": "str", "label": "Tag"},
            ],
        },
    )
    assert r_create.status_code in (200, 201), r_create.text

    r_ingest = e2e_client.post(
        "/api/ext-data/hot_rank/ingest",
        json={
            "date": "2026-04-15",
            "rows": [
                {"symbol": "000001.SZ", "heat": 95.5, "tag": "top"},
                {"symbol": "600000.SH", "heat": 40.0, "tag": "mid"},
            ],
        },
    )
    assert r_ingest.status_code == 200, r_ingest.text

    r_rows = e2e_client.get("/api/ext-data/hot_rank/rows", params={"date": "2026-04-15"})
    assert r_rows.status_code == 200
    rows = r_rows.json()["rows"]
    assert len(rows) == 2
    by_sym = {row["symbol"]: row for row in rows}
    assert by_sym["000001.SZ"]["heat"] == pytest.approx(95.5)

    r_schema = e2e_client.get("/api/ext-data/schema/hot_rank")
    assert r_schema.status_code == 200

    r_del = e2e_client.delete("/api/ext-data/hot_rank")
    assert r_del.status_code == 200


def test_ext_data_csv_detect_fields_and_upload(e2e_client: TestClient) -> None:
    csv_bytes = b"symbol,alpha_val\n000001.SZ,12.34\n600000.SH,56.78\n"
    r_det = e2e_client.post(
        "/api/ext-data/detect-fields",
        files={"file": ("sample.csv", csv_bytes, "text/csv")},
    )
    assert r_det.status_code == 200, r_det.text
    det = r_det.json()
    assert det["rows"] == 2
    assert "symbol" in det["symbol_candidates"]

    e2e_client.post(
        "/api/ext-data",
        json={
            "id": "csv_ext",
            "label": "CSV Ext",
            "mode": "snapshot",
            "fields": [{"name": "alpha_val", "dtype": "float", "label": "Alpha"}],
        },
    )
    r_up = e2e_client.post(
        "/api/ext-data/csv_ext/upload",
        files={"file": ("sample.csv", csv_bytes, "text/csv")},
    )
    assert r_up.status_code == 200, r_up.text
    rows = e2e_client.get("/api/ext-data/csv_ext/rows").json()["rows"]
    assert len(rows) == 2
    assert {r["symbol"] for r in rows} == {"000001.SZ", "600000.SH"}


def test_ext_data_http_pull_from_local_server(
    e2e_client: TestClient,
    local_http_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAFE_HTTP_ALLOW_HOSTS", "127.0.0.1")
    local_http_server.set_json_route(
        "/ext-feed",
        {
            "items": [
                {"code": "000001.SZ", "factor_x": 3.14},
                {"code": "300750.SZ", "factor_x": 6.28},
            ]
        },
    )
    e2e_client.post(
        "/api/ext-data",
        json={
            "id": "pull_tbl",
            "label": "Pull Table",
            "mode": "snapshot",
            "fields": [{"name": "factor_x", "dtype": "float", "label": "Factor X"}],
        },
    )
    r_cfg = e2e_client.put(
        "/api/ext-data/pull_tbl/pull",
        json={
            "url": f"{local_http_server.base_url}/ext-feed",
            "method": "GET",
            "response_path": "items",
            "field_map": {"code": "symbol", "factor_x": "factor_x"},
            "enabled": False,
        },
    )
    assert r_cfg.status_code == 200, r_cfg.text

    r_test = e2e_client.post("/api/ext-data/pull_tbl/pull/test")
    assert r_test.status_code == 200, r_test.text
    assert r_test.json()["total_rows"] == 2

    r_run = e2e_client.post("/api/ext-data/pull_tbl/pull/run")
    assert r_run.status_code == 200, r_run.text

    rows = e2e_client.get("/api/ext-data/pull_tbl/rows").json()["rows"]
    assert len(rows) == 2
    assert any(r["symbol"] == "300750.SZ" and r["factor_x"] == pytest.approx(6.28) for r in rows)


def test_ext_data_api_key_redaction_and_token_isolation(e2e_client: TestClient) -> None:
    setup_and_login(e2e_client)
    e2e_client.post(
        "/api/ext-data",
        json={
            "id": "secret_tbl",
            "label": "Secret Table",
            "mode": "snapshot",
            "fields": [{"name": "v", "dtype": "float", "label": "V"}],
        },
    )
    raw_secret = "sk-very-secret-ext-key-9876543210"
    r_set = e2e_client.put("/api/ext-data/secret_tbl/api-key", json={"key": raw_secret})
    assert r_set.status_code == 200

    r_get = e2e_client.get("/api/ext-data/secret_tbl/api-key")
    assert r_get.status_code == 200
    body = r_get.json()
    assert body["key_set"] is True
    assert raw_secret not in json.dumps(body)

    _, read_tok = create_api_token(e2e_client, "ext_reader", ["read:ext"])
    e2e_client.cookies.clear()
    assert e2e_client.get(
        "/api/ext-data/secret_tbl/api-key",
        headers={"Authorization": f"Bearer {read_tok}"},
    ).status_code == 403


def test_ext_data_registers_ext_factors_in_registry(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    e2e_client.post(
        "/api/ext-data",
        json={
            "id": "quant_sig",
            "label": "Quant Sig",
            "mode": "timeseries",
            "fields": [{"name": "momentum_ext", "dtype": "float", "label": "Ext Momentum"}],
        },
    )
    e2e_client.post(
        "/api/ext-data/quant_sig/ingest",
        json={
            "date": "2026-04-15",
            "rows": [{"symbol": "000001.SZ", "momentum_ext": 1.25}],
        },
    )
    r_fac = e2e_client.get("/api/factors")
    assert r_fac.status_code == 200
    factor_ids = {f["id"] for f in r_fac.json()["factors"]}
    assert "ext_quant_sig_momentum_ext" in factor_ids


# =============================================================================
# Area 6: Watchlist, Realtime QuoteService, Depth5 & SSE Bus
# =============================================================================


def test_watchlist_groups_crud_and_enriched_join(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)

    r_grp = e2e_client.post("/api/watchlist/groups", json={"name": "CorePool", "color": "#ff0000"})
    assert r_grp.status_code == 200, r_grp.text
    grp_id = r_grp.json()["group"]["id"]

    r_add = e2e_client.post("/api/watchlist", json={"symbol": "000001.SZ", "note": "focus", "group_id": grp_id})
    assert r_add.status_code == 200, r_add.text

    r_enr = e2e_client.get("/api/watchlist/enriched")
    assert r_enr.status_code == 200, r_enr.text
    items = r_enr.json()["items"]
    assert any(item["symbol"] == "000001.SZ" and item.get("close") is not None for item in items)

    r_del = e2e_client.delete("/api/watchlist/000001.SZ")
    assert r_del.status_code == 200


def test_detail_leases_and_four_core_indices_contract(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)

    r_sub = e2e_client.put(
        "/api/intraday/subscriptions/tab_1",
        json={"symbols": ["000001.SZ", "300750.SZ"]},
    )
    assert r_sub.status_code == 200, r_sub.text

    r_idx = e2e_client.get("/api/intraday/indices")
    assert r_idx.status_code == 200
    indices = r_idx.json()["indices"]
    symbols = {idx["symbol"] for idx in indices}
    assert symbols == {"000001.SH", "399001.SZ", "399006.SZ", "000680.SH"}


def test_realtime_quote_injection_updates_intraday_indices(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    rt_df = pl.DataFrame([
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "price": 11.11,
            "prev_close": 10.10,
            "open": 10.20,
            "high": 11.11,
            "low": 10.15,
            "volume": 5_000_000.0,
            "amount": 55_000_000.0,
            "change_pct": 0.10,
            "turnover_rate": 0.03,
        }
    ])
    idx_rt = [
        {"symbol": "000001.SH", "name": "上证指数", "price": 3250.5, "prev_close": 3200.0, "change_pct": 0.01578},
        {"symbol": "399001.SZ", "name": "深证成指", "price": 10500.0, "prev_close": 10400.0, "change_pct": 0.00961},
        {"symbol": "399006.SZ", "name": "创业板指", "price": 2150.0, "prev_close": 2100.0, "change_pct": 0.02381},
        {"symbol": "000680.SH", "name": "科创综指", "price": 1150.0, "prev_close": 1100.0, "change_pct": 0.04545},
    ]
    mock_provider_factory(
        name="mock_rt_src",
        datasets={"realtime"},
        realtime_df=rt_df,
        realtime_indices=idx_rt,
    )
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={"realtime_data_provider": "mock_rt_src"},
    )
    e2e_client.put(
        "/api/settings/preferences/realtime-quotes",
        json={"enabled": True, "scope": "all"},
    )
    r_ref = e2e_client.post("/api/intraday/refresh")
    assert r_ref.status_code == 200

    indices = e2e_client.get("/api/intraday/indices").json()["indices"]
    by_sym = {i["symbol"]: i for i in indices}
    assert by_sym["000001.SH"]["price"] == pytest.approx(3250.5)


def test_depth5_lots_semantics_and_limit_ladder(
    e2e_client: TestClient,
    mock_provider_factory: Any,
) -> None:
    data_dir = e2e_client.app.state.repo.store.data_dir
    latest = seed_repo_market_data(data_dir, stock_symbols=["000001.SZ"])
    snap = make_depth5_snapshot("000001.SZ", 11.00, bid_vol_lots=500, sealed_up=True)
    mock_provider_factory(
        name="mock_d5_src",
        datasets={"depth5"},
        depth_map={"000001.SZ": snap},
    )
    e2e_client.put(
        "/api/settings/preferences/data-providers",
        json={"depth5_data_provider": "mock_d5_src"},
    )
    ds = e2e_client.app.state.depth_service
    with ds._lock:
        ds._seal_locked({"000001.SZ": snap}, latest)
    ds._persist(latest)

    sealed_map = ds.get_sealed_map(latest)
    assert "000001.SZ" in sealed_map
    assert sealed_map["000001.SZ"]["sealed_up"] is True
    # Sealed volume stored in lots (bid_volumes[0] = 500 * 5 = 2500 lots)
    assert sealed_map["000001.SZ"]["bid1_vol"] == 2500


def test_sse_ticket_issuance_and_scoped_event_stream(
    e2e_client: TestClient,
) -> None:
    from app.services import alert_store

    setup_and_login(e2e_client)
    _, analysis_tok = create_api_token(e2e_client, "sse_reader", ["read:analysis"])
    e2e_client.cookies.clear()

    r_tk = e2e_client.post("/api/events/ticket", headers={"Authorization": f"Bearer {analysis_tok}"})
    assert r_tk.status_code == 200
    ticket = r_tk.json()["ticket"]
    assert ticket.startswith("tse_")

    data_dir = e2e_client.app.state.repo.store.data_dir

    def _emit() -> None:
        alert_store.append(data_dir, {"ts": 1760000001, "rule_id": "r_e2e", "symbol": "000001.SZ", "source": "test"})

    # Test stream yields hello + alert frames
    with e2e_client.stream("GET", f"/api/events?ticket={ticket}") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        _emit()
        frames = parse_sse_frames(resp.iter_lines(), max_frames=2)

    assert len(frames) == 2
    assert frames[0]["event"] == "hello"
    assert frames[0]["data"]["scopes"] == ["read:analysis"]
    assert frames[1]["event"] == "alert"
    assert frames[1]["data"]["rule_id"] == "r_e2e"


def test_sse_scope_filtering_drops_unauthorized_events(e2e_client: TestClient) -> None:
    from app.services.events import bus

    setup_and_login(e2e_client)
    _, mkt_tok = create_api_token(e2e_client, "sse_market_only", ["read:market"])
    e2e_client.cookies.clear()

    ticket = e2e_client.post("/api/events/ticket", headers={"Authorization": f"Bearer {mkt_tok}"}).json()["ticket"]

    with e2e_client.stream("GET", f"/api/events?ticket={ticket}") as resp:
        assert resp.status_code == 200
        # Publish restricted read:analysis event (must be dropped) followed by wildcard event (must arrive)
        bus.publish("alert", {"rule_id": "secret_rule"}, scope="read:analysis")
        bus.publish("system_notice", {"msg": "ok"}, scope="*")
        frames = parse_sse_frames(resp.iter_lines(), max_frames=2)

    assert len(frames) == 2
    assert frames[0]["event"] == "hello"
    assert frames[1]["event"] == "system_notice"
    assert frames[1]["data"]["msg"] == "ok"

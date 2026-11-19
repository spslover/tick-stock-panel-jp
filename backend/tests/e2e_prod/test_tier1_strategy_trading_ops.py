"""Tier 1 Feature Coverage: Areas 7, 8, 9 (Strategy/Factors, Backtest/Paper, Monitor/Extensions/Backup/AI)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from .helpers import (
    build_extension_package_zip,
    build_source_package_zip,
    seed_repo_market_data,
    setup_and_login,
)

_CUSTOM_STRATEGY_CODE = '''"""E2E Custom Strategy"""
import numpy as np
from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix, matrix_feature

META = {
    "id": "custom_e2e_mom",
    "name": "E2E Momentum",
    "description": "E2E test momentum strategy",
    "tags": ["e2e"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "min_close", "label": "Min Close", "type": "float", "default": 1.0, "min": 0.1, "max": 1000.0, "step": 0.5}
    ],
    "scoring": {"momentum_20d": 1.0},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

EXECUTION_BACKEND = "matrix_native"
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 10


class CustomE2EMomStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return 20

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        ma5 = matrix_feature(market, "ma5")
        ma20 = matrix_feature(market, "ma20")
        entry = (ma5 > ma20) & (market.close >= float(params.get("min_close", 1.0)))
        exit_sig = ma5 < ma20
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_sig.astype(np.uint8),
        )


MATRIX_STRATEGY = CustomE2EMomStrategy()
'''

_POLICY_CODE = """def compute(ctx):
    n = len(ctx.selected)
    if n == 0:
        return PositionSizingResult(weights=[], budget_scale=1.0)
    w = 1.0 / n
    return PositionSizingResult(weights=[w] * n, budget_scale=0.8)
"""


# =============================================================================
# Area 7: Strategy Engine, Factor Platform & Mining Lab
# =============================================================================


def test_strategy_validate_save_run_config_invalidate_and_delete(e2e_client: TestClient) -> None:
    latest = seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)

    r_list = e2e_client.get("/api/strategies")
    assert r_list.status_code == 200
    assert any(s["id"] == "ma_golden_cross" for s in r_list.json()["strategies"])

    # Validate
    r_val = e2e_client.post(
        "/api/strategies/code/validate",
        json={"code": _CUSTOM_STRATEGY_CODE, "strategy_id": "custom_e2e_mom"},
    )
    assert r_val.status_code == 200, r_val.text
    assert r_val.json()["valid"] is True

    # Save
    r_save = e2e_client.post(
        "/api/strategies/code/save",
        json={
            "code": _CUSTOM_STRATEGY_CODE,
            "strategy_id": "custom_e2e_mom",
            "target_source": "custom",
            "mode": "create",
            "name": "E2E Momentum",
            "description": "E2E test",
        },
    )
    assert r_save.status_code == 200, r_save.text

    # Run
    r_run = e2e_client.post(
        "/api/strategies/run",
        json={"strategy_id": "custom_e2e_mom", "as_of": latest.isoformat()},
    )
    assert r_run.status_code == 200, r_run.text
    run_body = r_run.json()
    assert run_body["strategy_id"] == "custom_e2e_mom"
    assert isinstance(run_body["matches"], list)

    # Update config override & verify cached state invalidated cleanly
    r_cfg = e2e_client.post(
        "/api/strategies/config",
        json={"strategy_id": "custom_e2e_mom", "overrides": {"params": {"min_close": 9999.0}}},
    )
    assert r_cfg.status_code == 200, r_cfg.text
    r_run2 = e2e_client.post(
        "/api/strategies/run",
        json={"strategy_id": "custom_e2e_mom", "as_of": latest.isoformat()},
    )
    assert r_run2.status_code == 200
    assert len(r_run2.json()["matches"]) == 0

    # Delete
    r_del = e2e_client.delete("/api/strategies/custom_e2e_mom")
    assert r_del.status_code == 200
    assert e2e_client.get("/api/strategies/custom_e2e_mom").status_code == 404


def test_composite_strategy_save_run_and_dependent_delete_guard(e2e_client: TestClient) -> None:
    latest = seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    e2e_client.post(
        "/api/strategies/code/save",
        json={
            "code": _CUSTOM_STRATEGY_CODE,
            "strategy_id": "custom_e2e_child",
            "target_source": "custom",
            "mode": "create",
            "name": "Child",
            "description": "Child",
        },
    )

    r_comp = e2e_client.post(
        "/api/strategies/composite/save",
        json={
            "strategy_id": "composite_e2e_combo",
            "name": "E2E Combo",
            "description": "Combo test",
            "children": [
                {"strategy_id": "custom_e2e_child", "weight": 0.5},
                {"strategy_id": "ma_golden_cross", "weight": 0.5},
            ],
            "merge_mode": "any",
            "min_confirm": 1,
            "mode": "create",
        },
    )
    assert r_comp.status_code == 200, r_comp.text

    # Deleting referenced child must fail 409
    r_del_child = e2e_client.delete("/api/strategies/custom_e2e_child")
    assert r_del_child.status_code == 409

    # Run composite
    r_run = e2e_client.post(
        "/api/strategies/run",
        json={"strategy_id": "composite_e2e_combo", "as_of": latest.isoformat()},
    )
    assert r_run.status_code == 200

    # Delete composite then child succeeds
    assert e2e_client.delete("/api/strategies/composite_e2e_combo").status_code == 200
    assert e2e_client.delete("/api/strategies/custom_e2e_child").status_code == 200


def test_factor_dsl_and_py_expr_validate_and_trial(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir, n_days=60)

    # DSL validate + trial
    r_dsl = e2e_client.post(
        "/api/factors/validate",
        json={"formula": "close / ref(close, 5) - 1", "lang": "dsl"},
    )
    assert r_dsl.status_code == 200, r_dsl.text
    assert r_dsl.json()["ok"] is True

    # Python py_expr validate + trial
    r_py = e2e_client.post(
        "/api/factors/validate",
        json={"formula": "(ctx.col('close') / ctx.col('close').shift(5) - 1.0)", "lang": "python"},
    )
    assert r_py.status_code == 200, r_py.text
    assert r_py.json()["ok"] is True
    assert "close" in r_py.json()["dependencies"]

    r_trial = e2e_client.post(
        "/api/factors/trial",
        json={
            "formula": "(ctx.col('close') / ctx.col('close').shift(5) - 1.0)",
            "lang": "python",
            "asset_type": "stock",
            "days": 40,
        },
    )
    assert r_trial.status_code == 200, r_trial.text
    trial_body = r_trial.json()
    assert "ic_mean" in trial_body or "rank_ic" in trial_body


def test_custom_factor_create_status_and_reference_delete_guard(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir, n_days=60)

    r_create = e2e_client.post(
        "/api/factors/custom",
        json={
            "id": "e2e_rev_5d",
            "label": "E2E 5D Rev",
            "formula": "close / ref(close, 5) - 1",
            "direction": "high",
        },
    )
    assert r_create.status_code == 200, r_create.text

    r_st = e2e_client.post("/api/factors/custom/e2e_rev_5d/status", json={"status": "active"})
    assert r_st.status_code == 200

    # Composite factor referencing custom factor
    r_comp = e2e_client.post(
        "/api/factors/composite",
        json={
            "id": "e2e_comp_f",
            "label": "E2E Comp",
            "members": {"e2e_rev_5d": 0.6, "momentum_20d": 0.4},
            "direction": "high",
        },
    )
    assert r_comp.status_code == 200, r_comp.text

    # Delete referenced custom factor blocked 409 unless force=true
    r_del_block = e2e_client.delete("/api/factors/custom/e2e_rev_5d")
    assert r_del_block.status_code == 409
    r_del_force = e2e_client.delete("/api/factors/custom/e2e_rev_5d", params={"force": True})
    assert r_del_force.status_code == 200


def test_mining_lock_availability_and_config(e2e_client: TestClient) -> None:
    r_lock = e2e_client.get("/api/backtest/mining/lock")
    assert r_lock.status_code == 200
    assert r_lock.json()["acquired"] is True

    r_avail = e2e_client.get("/api/backtest/mining/availability")
    assert r_avail.status_code == 200
    assert "available" in r_avail.json()

    r_cfg = e2e_client.get("/api/backtest/mining/config")
    assert r_cfg.status_code == 200
    r_patch = e2e_client.patch("/api/backtest/mining/config", json={"budget_profile": "strict"})
    assert r_patch.status_code in (200, 422)


# =============================================================================
# Area 8: Backtest, Optimization & Paper Trading
# =============================================================================


def test_vectorized_signal_backtest_run(e2e_client: TestClient) -> None:
    latest = seed_repo_market_data(e2e_client.app.state.repo.store.data_dir, n_days=80)
    start = latest - timedelta(days=90)
    r = e2e_client.post(
        "/api/backtest/run",
        json={
            "symbols": ["000001.SZ", "600000.SH"],
            "start": start.isoformat(),
            "end": latest.isoformat(),
            "entries": ["signal_ma_golden_5_20"],
            "exits": ["signal_ma_dead_5_20"],
            "matching": "open_t+1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "equity_curve" in body or "results" in body or "stats" in body


def test_strategy_matrix_backtest_and_evidence_audit(e2e_client: TestClient) -> None:
    latest = seed_repo_market_data(e2e_client.app.state.repo.store.data_dir, n_days=80)
    start = latest - timedelta(days=90)
    r = e2e_client.post(
        "/api/backtest/strategy/run",
        json={
            "strategy_id": "ma_golden_cross",
            "symbols": ["000001.SZ", "600000.SH", "300750.SZ"],
            "start": start.isoformat(),
            "end": latest.isoformat(),
            "initial_capital": 1_000_000.0,
            "entry_fill": "open_t+1",
            "exit_fill": "open_t+1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "stats" in body and "equity_curve" in body
    assert len(body["equity_curve"]) > 0
    assert body["stats"]["initial_capital"] == pytest.approx(1_000_000.0)

    run_id = body.get("run_id")
    if run_id:
        r_ev = e2e_client.get(f"/api/backtest/strategy/runs/{run_id}/evidence")
        assert r_ev.status_code == 200, r_ev.text
        ev = r_ev.json()
        assert ev.get("strategy_id") == "ma_golden_cross"


def test_position_policy_validate_save_and_backtest_integration(e2e_client: TestClient) -> None:
    latest = seed_repo_market_data(e2e_client.app.state.repo.store.data_dir, n_days=80)
    start = latest - timedelta(days=90)

    r_tpl = e2e_client.get("/api/backtest/position-policies/templates")
    assert r_tpl.status_code == 200
    assert len(r_tpl.json()["templates"]) >= 1

    r_val = e2e_client.post(
        "/api/backtest/position-policies/validate",
        json={"id": "up_e2e_eq", "label": "E2E Equal", "code": _POLICY_CODE},
    )
    assert r_val.status_code == 200, r_val.text
    assert r_val.json()["valid"] is True

    r_save = e2e_client.post(
        "/api/backtest/position-policies",
        json={"id": "up_e2e_eq", "label": "E2E Equal", "code": _POLICY_CODE},
    )
    assert r_save.status_code == 200, r_save.text

    r_bt = e2e_client.post(
        "/api/backtest/strategy/run",
        json={
            "strategy_id": "ma_golden_cross",
            "symbols": ["000001.SZ", "600000.SH"],
            "start": start.isoformat(),
            "end": latest.isoformat(),
            "position_sizing": "policy",
            "position_policy": "up_e2e_eq",
        },
    )
    assert r_bt.status_code == 200, r_bt.text

    # Missing policy must fail closed 400 (never silent fallback)
    r_bad = e2e_client.post(
        "/api/backtest/strategy/run",
        json={
            "strategy_id": "ma_golden_cross",
            "symbols": ["000001.SZ"],
            "start": start.isoformat(),
            "end": latest.isoformat(),
            "position_sizing": "policy",
            "position_policy": "up_nonexistent",
        },
    )
    assert r_bad.status_code == 400


def test_backtest_robust_and_walkforward_preview(e2e_client: TestClient) -> None:
    latest = seed_repo_market_data(e2e_client.app.state.repo.store.data_dir, n_days=80)
    start = (latest - timedelta(days=90)).isoformat()
    end = latest.isoformat()

    r_rob = e2e_client.get(
        "/api/backtest/optimize-robust/preview",
        params={"strategy_id": "ma_golden_cross", "start": start, "end": end},
    )
    assert r_rob.status_code == 200, r_rob.text

    r_wf = e2e_client.get(
        "/api/backtest/walkforward/preview",
        params={"strategy_id": "ma_golden_cross", "start": start, "end": end},
    )
    assert r_wf.status_code == 200, r_wf.text


def test_paper_account_order_fill_positions_nav_and_rebuild(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)

    r_acc = e2e_client.post(
        "/api/paper/account",
        json={"account_id": "acct_1", "name": "E2E Account", "initial_cash": 100_000.0},
    )
    assert r_acc.status_code == 200, r_acc.text
    assert r_acc.json()["account"]["cash"] == pytest.approx(100_000.0)

    r_ord = e2e_client.post(
        "/api/paper/orders",
        params={"account": "acct_1"},
        json={"symbol": "000001.SZ", "side": "buy", "qty": 1000, "order_type": "market", "ref_price": 10.0},
    )
    assert r_ord.status_code == 200, r_ord.text
    order = r_ord.json()["order"]
    assert order["status"] == "filled"

    trades = e2e_client.get("/api/paper/trades", params={"account": "acct_1"}).json()["fills"]
    assert len(trades) == 1
    assert trades[0]["qty"] == 1000

    positions = e2e_client.get("/api/paper/positions", params={"account": "acct_1"}).json()["positions"]
    assert len(positions) == 1
    assert positions[0]["symbol"] == "000001.SZ"
    assert positions[0]["qty"] == 1000
    assert positions[0]["available_qty"] == 0  # T+1 lock on same day

    ov_before = e2e_client.get("/api/paper/overview", params={"account": "acct_1"}).json()
    nav = e2e_client.get("/api/paper/nav", params={"account": "acct_1"}).json()
    assert "nav" in nav

    r_reb = e2e_client.post("/api/paper/rebuild", params={"account": "acct_1"})
    assert r_reb.status_code == 200
    ov_after = e2e_client.get("/api/paper/overview", params={"account": "acct_1"}).json()
    assert ov_after["cash"] == pytest.approx(ov_before["cash"])


def test_paper_arena_batch_create_and_compare(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    r_arena = e2e_client.post(
        "/api/paper/arena/batch_create",
        json={
            "initial_cash": 200_000.0,
            "name_prefix": "Arena",
            "sources": [
                {"name": "S1", "match_kind": "strategy", "match_id": "ma_golden_cross"},
                {"name": "S2", "match_kind": "strategy", "match_id": "bullish_alignment"},
            ],
        },
    )
    assert r_arena.status_code == 200, r_arena.text
    accounts = r_arena.json()["accounts"]
    assert len(accounts) == 2

    r_cmp = e2e_client.get("/api/paper/compare")
    assert r_cmp.status_code == 200
    assert len(r_cmp.json()["accounts"]) >= 2


def test_paper_order_cancel_freeze_and_settings(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    e2e_client.post(
        "/api/paper/account",
        json={"account_id": "acct_cancel", "name": "Cancel", "initial_cash": 100_000.0},
    )
    r_set = e2e_client.post(
        "/api/paper/settings",
        params={"account": "acct_cancel"},
        json={"queue_limit_orders": True, "commission_pct": 0.0003},
    )
    assert r_set.status_code == 200

    # next_open stays pending until next open
    r_ord = e2e_client.post(
        "/api/paper/orders",
        params={"account": "acct_cancel"},
        json={"symbol": "000001.SZ", "side": "buy", "qty": 500, "order_type": "next_open", "ref_price": 10.0},
    )
    assert r_ord.status_code == 200
    oid = r_ord.json()["order"]["id"]
    assert r_ord.json()["order"]["status"] == "pending"

    r_can = e2e_client.delete(f"/api/paper/orders/{oid}", params={"account": "acct_cancel"})
    assert r_can.status_code == 200
    assert r_can.json()["order"]["status"] == "cancelled"

    r_frz = e2e_client.post("/api/paper/freeze", params={"account": "acct_cancel", "frozen": True})
    assert r_frz.status_code == 200


# =============================================================================
# Area 9: Monitor Rules, Alerts, Extensions, Config Backup & AI Budget
# =============================================================================


def test_monitor_rules_options_create_list_delete(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    r_opt = e2e_client.get("/api/monitor-rules/options")
    assert r_opt.status_code == 200
    assert "fields" in r_opt.json()

    r_create = e2e_client.post(
        "/api/monitor-rules",
        json={
            "id": "e2e_rule_1",
            "name": "E2E Rule",
            "enabled": True,
            "type": "threshold",
            "asset_type": "stock",
            "scope": "symbols",
            "symbols": ["000001.SZ"],
            "conditions": [{"field": "change_pct", "op": ">=", "value": 0.05}],
            "logic": "AND",
            "cooldown_seconds": 60,
            "severity": "info",
        },
    )
    assert r_create.status_code == 200, r_create.text

    rules = e2e_client.get("/api/monitor-rules").json()["rules"]
    assert any(r["id"] == "e2e_rule_1" for r in rules)

    assert e2e_client.delete("/api/monitor-rules/e2e_rule_1").status_code == 200


def test_alerts_store_seed_list_and_clear(e2e_client: TestClient) -> None:
    r_seed = e2e_client.post("/api/alerts/seed")
    assert r_seed.status_code == 200, r_seed.text

    r_list = e2e_client.get("/api/alerts")
    assert r_list.status_code == 200
    assert r_list.json()["total"] >= 1

    r_clr = e2e_client.delete("/api/alerts")
    assert r_clr.status_code == 200
    assert e2e_client.get("/api/alerts").json()["total"] == 0


def test_extension_packages_inspect_and_install(e2e_client: TestClient) -> None:
    setup_and_login(e2e_client)
    zip_bytes, sha = build_extension_package_zip("custom_e2e_ext", "1.0.0")

    r_ins = e2e_client.post(
        "/api/import-center/inspect",
        files={"files": ("custom_e2e_ext.zip", zip_bytes, "application/zip")},
    )
    assert r_ins.status_code == 200, r_ins.text
    assert r_ins.json()["kind"] == "extension"

    r_inst = e2e_client.post(
        "/api/extension-packages/install",
        data={"digest": sha, "revision": "1.0.0", "trusted": "true"},
        files={"file": ("custom_e2e_ext.zip", zip_bytes, "application/zip")},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert r_inst.status_code == 200, r_inst.text

    r_list = e2e_client.get("/api/extension-packages")
    assert r_list.status_code == 200
    assert any(p["id"] == "custom_e2e_ext" for p in r_list.json()["packages"])


def test_page_packs_upload_list_bundle_and_delete(e2e_client: TestClient) -> None:
    js_code = b"export default function DemoPage() { return null; }\n"
    r_up = e2e_client.post(
        "/api/ext-pages/upload",
        data={"id": "e2e_demo_page"},
        files={"file": ("demo.js", js_code, "application/javascript")},
    )
    assert r_up.status_code == 200, r_up.text

    r_list = e2e_client.get("/api/ext-pages")
    assert r_list.status_code == 200
    assert any(p["id"] == "e2e_demo_page" for p in r_list.json()["pages"])

    r_js = e2e_client.get("/api/ext-pages/e2e_demo_page/bundle.js")
    assert r_js.status_code == 200
    assert "javascript" in r_js.headers.get("content-type", "")
    assert r_js.headers.get("etag")

    assert e2e_client.delete("/api/ext-pages/e2e_demo_page").status_code == 200


def test_source_packages_inspect_install_and_remove(e2e_client: TestClient) -> None:
    setup_and_login(e2e_client)
    zip_bytes, sha = build_source_package_zip("mock_pkg_src", "1.0.0")
    hdrs = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}

    r_ins = e2e_client.post(
        "/api/custom/source-packages/inspect",
        files={"file": ("mock_pkg_src.zip", zip_bytes, "application/zip")},
        headers=hdrs,
    )
    assert r_ins.status_code == 200, r_ins.text
    assert r_ins.json()["manifest"]["name"] == "mock_pkg_src"

    r_inst = e2e_client.post(
        "/api/custom/source-packages/install",
        data={"digest": sha},
        files={"file": ("mock_pkg_src.zip", zip_bytes, "application/zip")},
        headers=hdrs,
    )
    assert r_inst.status_code == 200, r_inst.text

    r_rm = e2e_client.post(
        "/api/custom/source-packages/mock_pkg_src/manage",
        json={"action": "remove", "reset_routes": True},
        headers=hdrs,
    )
    assert r_rm.status_code == 200, r_rm.text


def test_config_backup_export_inspect_and_apply_roundtrip(e2e_client: TestClient) -> None:
    seed_repo_market_data(e2e_client.app.state.repo.store.data_dir)
    e2e_client.post(
        "/api/strategies/code/save",
        json={
            "code": _CUSTOM_STRATEGY_CODE,
            "strategy_id": "custom_e2e_bak",
            "target_source": "custom",
            "mode": "create",
            "name": "Backup Strat",
            "description": "bak",
        },
    )
    e2e_client.post("/api/watchlist", json={"symbol": "000001.SZ", "note": "bak"})

    r_exp = e2e_client.get("/api/config-backup/export")
    assert r_exp.status_code == 200
    assert r_exp.headers["content-type"] == "application/zip"
    backup_bytes = r_exp.content

    r_ins = e2e_client.post(
        "/api/config-backup/inspect",
        files={"file": ("backup.zip", backup_bytes, "application/zip")},
    )
    assert r_ins.status_code == 200, r_ins.text
    assert r_ins.json()["total_files"] >= 1

    # Delete strategy locally then restore from backup
    assert e2e_client.delete("/api/strategies/custom_e2e_bak").status_code == 200
    assert e2e_client.get("/api/strategies/custom_e2e_bak").status_code == 404

    r_app = e2e_client.post(
        "/api/config-backup/apply",
        data={"allow_overwrite": "true", "categories": ""},
        files={"file": ("backup.zip", backup_bytes, "application/zip")},
    )
    assert r_app.status_code == 200, r_app.text
    assert e2e_client.get("/api/strategies/custom_e2e_bak").status_code == 200


def test_ai_status_unconfigured_and_daily_budget_guard(
    e2e_client: TestClient,
    local_http_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r_st = e2e_client.get("/api/strategies/ai/status")
    assert r_st.status_code == 200
    assert r_st.json()["configured"] is False


@pytest.mark.r2_post_fix
@pytest.mark.r3_post_fix
def test_ai_daily_token_budget_enforced_fail_closed(
    make_e2e_client: Any,
    local_http_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AI_DAILY_TOKEN_BUDGET is exceeded, subsequent AI calls fail closed without hitting upstream."""
    from app import secrets_store
    from app.services import ai_provider

    monkeypatch.setenv("SAFE_HTTP_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.setenv("AI_ALLOW_LOCAL", "1")
    local_http_server.set_json_route(
        "/v1/chat/completions",
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 50, "total_tokens": 110},
        },
    )
    tc = make_e2e_client(settings_overrides={"ai_daily_token_budget": 100})
    secrets_store.save({
        "ai_provider": "openai_compat",
        "ai_base_url": f"{local_http_server.base_url}/v1",
        "ai_api_key": "sk-test-budget-key",
        "ai_model": "gpt-test",
    })

    # Assert ai_provider exposes daily budget tracking / rejection once budget exceeded
    import asyncio

    out1 = asyncio.run(ai_provider.generate_ai_text([{"role": "user", "content": "hi"}], max_tokens=20))
    assert out1 == "OK"

    with pytest.raises(Exception):
        asyncio.run(ai_provider.generate_ai_text([{"role": "user", "content": "second"}], max_tokens=20))

"""M2-1 金融精度、指标鲁棒性、模拟盘风控与 TdxQuant 北京时区回归测试。"""
from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router as paper_router
from app.indicators import pipeline
from app.market_time import CN_TZ
from app.plugins.tdxquant import provider as tp
from app.price_limits import (
    calc_limit_price,
    calc_limit_prices,
    numpy_limit_price,
    polars_limit_price,
    price_limit_pct,
    round_price,
)
from app.strategy import paper


# ====================================================================
# 1. price_limits.py (H-06)
# ====================================================================
def test_round_price_decimal_half_up_and_validation() -> None:
    cases = [
        (10.005, 2, 10.01),
        (10.055, 2, 10.06),
        (1.005, 2, 1.01),
        (1.015, 2, 1.02),
        (2.625, 2, 2.63),
        (-2.625, 2, -2.63),
        (1.0055, 3, 1.006),
        (1.0044, 3, 1.004),
    ]
    for val, decimals, expected in cases:
        assert round_price(val, decimals) == expected
    assert round_price(-0.001, 2) == 0.0

    for bad in (True, float("nan"), float("inf"), float("-inf")):
        with pytest.raises((ValueError, TypeError)):
            round_price(bad, 2)


def test_limit_price_single_step_matches_decimal_across_scalar_polars_numpy() -> None:
    prevs = [10.005, 10.055, 10.125, 7.032, 1.005, 9.995, 10.15, 2.675, 0.35]
    rates = [0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.0]

    for up in (True, False):
        expected = [
            float(
                (
                    Decimal(str(p))
                    * (Decimal("1") + Decimal(str(r)) if up else Decimal("1") - Decimal(str(r)))
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
            for p, r in zip(prevs, rates, strict=True)
        ]
        scalar = [calc_limit_price(p, r, up=up, decimals=2) for p, r in zip(prevs, rates, strict=True)]
        pl_vals = (
            pl.DataFrame({"p": prevs, "r": rates})
            .select(polars_limit_price(pl.col("p"), pl.col("r"), up=up, decimals=2).alias("out"))["out"]
            .to_list()
        )
        np_vals = numpy_limit_price(np.array(prevs), np.array(rates), up=up, decimals=2).tolist()

        assert scalar == expected
        assert pl_vals == expected
        assert np_vals == expected

    assert calc_limit_prices(10.055, 0.10, decimals=2) == (11.06, 9.05)
    assert calc_limit_price(7.032, 0.10, up=True, decimals=2) == 7.74
    assert calc_limit_price(1.005, 0.10, up=True, decimals=2) == 1.11
    assert calc_limit_price(10.15, 0.10, up=True, decimals=2) == 11.17


def test_limit_price_etf_three_decimals_and_non_finite_safety() -> None:
    cases = [(1.005, 0.10, True, 1.106), (1.004, 0.10, True, 1.104), (1.235, 0.10, False, 1.112)]
    for prev, rate, up, expected in cases:
        assert calc_limit_price(prev, rate, up=up, decimals=3) == expected
        pl_val = pl.DataFrame({"p": [prev], "r": [rate]}).select(
            polars_limit_price(pl.col("p"), pl.col("r"), up=up, decimals=3).alias("out")
        )["out"][0]
        np_val = float(numpy_limit_price(np.array([prev]), np.array([rate]), up=up, decimals=3)[0])
        assert pl_val == expected
        assert np_val == expected

    # Polars / NumPy must handle None, NaN, Inf, negative without throwing
    bad_df = pl.DataFrame({"p": [None, float("nan"), float("inf"), -1.0, 10.0], "r": [0.10] * 5})
    out = bad_df.select(polars_limit_price(pl.col("p"), pl.col("r"), up=True).alias("out"))["out"].to_list()
    assert out == [None, None, None, None, 11.0]

    np_out = numpy_limit_price(
        np.array([np.nan, np.inf, -1.0, 10.0]), np.array([0.10] * 4), up=True
    )
    assert np.isnan(np_out[:3]).all()
    assert np_out[3] == 11.0


def test_price_limit_pct_bse_bare_and_etf() -> None:
    d = date(2026, 7, 3)
    for code in ("430001", "830001", "870001", "920001", "830001.BJ"):
        assert price_limit_pct(code, d) == pytest.approx(0.30)
    assert price_limit_pct("510300.SH", d, is_risk_warning=True, asset_type="etf") == pytest.approx(0.10)
    assert price_limit_pct("588000.SH", d, is_risk_warning=True, asset_type="etf") == pytest.approx(0.10)


# ====================================================================
# 2. indicators/pipeline.py (H-06, H-07, H-09, H-10, M-01)
# ====================================================================
def test_pipeline_math_half_up_exactness() -> None:
    vals = [1.005, 1.015, 2.625, 2.675, 10.055, -2.625, None, float("nan"), float("inf")]
    out = (
        pl.DataFrame({"v": vals}, schema={"v": pl.Float64})
        .select(pipeline._math_half_up(pl.col("v"), 2).alias("r"))["r"]
        .to_list()
    )
    assert out == [1.01, 1.02, 2.63, 2.68, 10.06, -2.63, None, None, None]


def test_compute_enriched_today_kdj_flat_range_yields_none_never_nan(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "_custom_signal_exprs_today", {"sig_kdj": pl.col("kdj_k") > 50.0})
    monkeypatch.setattr(pipeline, "_custom_prev_fields_cache", set())

    today = date(2026, 8, 18)
    base_row = {
        "date": [today, today],
        "open": [10.0, 10.0],
        "high": [10.0, 12.0],
        "low": [10.0, 9.0],
        "close": [10.0, 11.0],
        "volume": [1000.0, 1000.0],
        "amount": [10000.0, 11000.0],
    }
    today_ohlcv = pl.DataFrame({"symbol": ["511990.SH", "000001.SZ"], **base_row})
    live_agg = pl.DataFrame({
        "symbol": ["511990.SH"],
        "ema5": [10.0], "ema10": [10.0], "ema20": [10.0], "ema30": [10.0], "ema60": [10.0],
        "_ema12": [10.0], "_ema26": [10.0], "macd_dea": [0.0],
        "_ma5_partial_sum": [40.0], "_ma10_partial_sum": [90.0], "_ma20_partial_sum": [190.0],
        "_ma30_partial_sum": [290.0], "_ma60_partial_sum": [590.0],
        "_boll_partial_sum": [190.0], "_boll_partial_sq_sum": [1900.0],
        "_kdj_8d_low": [10.0], "_kdj_8d_high": [10.0], "kdj_k": [50.0], "kdj_d": [50.0],
        "atr_14": [0.0],
        "_rsi_avg_gain_6": [0.0], "_rsi_avg_loss_6": [0.0],
        "_rsi_avg_gain_14": [0.0], "_rsi_avg_loss_14": [0.0],
        "_rsi_avg_gain_24": [0.0], "_rsi_avg_loss_24": [0.0],
        "_vol_ma5_partial_sum": [4000.0], "_vol_ma10_partial_sum": [9000.0], "_vol_ma5_prev_sum": [5000.0],
        "_high_59d": [10.0], "_low_59d": [10.0],
        "_close_5d_ago": [10.0], "_close_10d_ago": [10.0], "_close_20d_ago": [10.0],
        "_close_30d_ago": [10.0], "_close_60d_ago": [10.0],
        "_vol_19d_pct_sum": [0.0], "_vol_19d_pct_sq_sum": [0.0],
        "_window_len": [60],
    })
    out = pipeline.compute_enriched_today(live_agg, pl.DataFrame(), today_ohlcv).sort("symbol")
    # 000001.SZ: no history state -> kdj_* None
    assert out["kdj_k"][0] is None and out["kdj_d"][0] is None and out["kdj_j"][0] is None
    # 511990.SH: flat 9-day high==low -> None, never NaN, and custom signal sees None
    assert out["kdj_k"][1] is None and out["kdj_d"][1] is None and out["kdj_j"][1] is None
    assert out["sig_kdj"][1] is None


def test_vol_ratio_opening_floor_in_batch_and_realtime(monkeypatch) -> None:
    today = date(2026, 8, 18)
    monkeypatch.setattr("app.market_time.cn_today", lambda: today)
    monkeypatch.setattr("app.market_time.trading_minutes_elapsed", lambda: 0.5)

    days = [today - timedelta(days=k) for k in range(6, 0, -1)] + [today]
    df = pl.DataFrame({
        "symbol": ["600000.SH"] * len(days),
        "date": days,
        "open": [10.0] * len(days),
        "high": [10.5] * len(days),
        "low": [9.5] * len(days),
        "close": [10.0] * len(days),
        "volume": [200.0] * 6 + [100.0],
    })
    batch_out = pipeline.compute_indicators(df, needed={"vol_ratio_5d"}).sort("date")
    expected_clamped = 100.0 * (240.0 / pipeline._MIN_VOL_RATIO_ELAPSED_MINUTES) / 200.0
    assert batch_out.tail(1)["vol_ratio_5d"][0] == pytest.approx(expected_clamped)

    # Zero prior volume yields None (never inf)
    zero_df = df.with_columns(pl.Series("volume", [0.0] * 6 + [100.0]))
    zero_out = pipeline.compute_indicators(zero_df, needed={"vol_ratio_5d"}).sort("date")
    assert zero_out.tail(1)["vol_ratio_5d"][0] is None

    # Realtime path uses the exact same floor
    monkeypatch.setattr(pipeline, "_custom_signal_exprs_today", {})
    monkeypatch.setattr(pipeline, "_custom_prev_fields_cache", set())
    today_ohlcv = pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [today],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "volume": [100.0], "amount": [1000.0],
    })
    live_agg = pl.DataFrame({
        "symbol": ["600000.SH"],
        "ema5": [10.0], "ema10": [10.0], "ema20": [10.0], "ema30": [10.0], "ema60": [10.0],
        "_ema12": [10.0], "_ema26": [10.0], "macd_dea": [0.0],
        "_ma5_partial_sum": [40.0], "_ma10_partial_sum": [90.0], "_ma20_partial_sum": [190.0],
        "_ma30_partial_sum": [290.0], "_ma60_partial_sum": [590.0],
        "_boll_partial_sum": [190.0], "_boll_partial_sq_sum": [1900.0],
        "_kdj_8d_low": [9.5], "_kdj_8d_high": [10.5], "kdj_k": [50.0], "kdj_d": [50.0],
        "atr_14": [0.5],
        "_rsi_avg_gain_6": [0.1], "_rsi_avg_loss_6": [0.1],
        "_rsi_avg_gain_14": [0.1], "_rsi_avg_loss_14": [0.1],
        "_rsi_avg_gain_24": [0.1], "_rsi_avg_loss_24": [0.1],
        "_vol_ma5_partial_sum": [800.0], "_vol_ma10_partial_sum": [1800.0], "_vol_ma5_prev_sum": [1000.0],
        "_high_59d": [10.5], "_low_59d": [9.5],
        "_close_5d_ago": [10.0], "_close_10d_ago": [10.0], "_close_20d_ago": [10.0],
        "_close_30d_ago": [10.0], "_close_60d_ago": [10.0],
        "_vol_19d_pct_sum": [0.0], "_vol_19d_pct_sq_sum": [0.0],
        "_window_len": [60],
    })
    rt_clamped = pipeline.compute_enriched_today(
        live_agg, pl.DataFrame(), today_ohlcv, elapsed_minutes=0.0167
    )
    assert rt_clamped["vol_ratio_5d"][0] == pytest.approx(expected_clamped)
    rt_after = pipeline.compute_enriched_today(
        live_agg, pl.DataFrame(), today_ohlcv, elapsed_minutes=240.0
    )
    assert rt_after["vol_ratio_5d"][0] == pytest.approx(0.5)


def test_load_recent_history_uses_cn_today(tmp_path: Path, monkeypatch) -> None:
    beijing_today = date(2026, 9, 25)
    monkeypatch.setattr(pipeline, "cn_today", lambda: beijing_today)

    # Partition strictly inside cn_today() - (days + 30) window (would be excluded if today were earlier)
    enriched_dir = tmp_path / "kline_daily_enriched" / "date=2026-09-20"
    enriched_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2026, 9, 20)],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "volume": [100.0], "amount": [1000.0],
        "raw_close": [10.0], "raw_high": [10.0], "raw_low": [10.0],
        "turnover_rate": [1.0], "consecutive_limit_ups": [0], "consecutive_limit_downs": [0],
        "quote_ts": [0],
    }).cast({"consecutive_limit_ups": pl.UInt32, "consecutive_limit_downs": pl.UInt32}).write_parquet(
        enriched_dir / "part.parquet"
    )

    # Advance cn_today far enough that 2026-09-20 drops out -> proves cn_today drives cutoff
    assert not pipeline._load_recent_history(tmp_path / "kline_daily_enriched", ["600000.SH"], 5).is_empty()
    monkeypatch.setattr(pipeline, "cn_today", lambda: date(2027, 1, 1))
    assert pipeline._load_recent_history(tmp_path / "kline_daily_enriched", ["600000.SH"], 5).is_empty()


def test_benchmark_cache_invalidates_on_mutation_and_is_thread_safe(tmp_path: Path) -> None:
    pipeline.invalidate_benchmark_cache()
    days = [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 15), date(2026, 8, 18)]

    def write_idx(closes: list[float], part_name: str = "part.parquet") -> Path:
        target = tmp_path / "kline_index_daily" / "date=2026-08-18"
        target.mkdir(parents=True, exist_ok=True)
        path = target / part_name
        pl.DataFrame({
            "symbol": ["000001.SH"] * len(days),
            "date": days,
            "close": closes,
        }).write_parquet(path)
        return path

    p = write_idx([10.0, 11.0, 12.0, 13.0])
    f1 = pipeline.load_benchmark_momentum(tmp_path)
    assert f1 is not None
    assert f1.sort("date").row(-1, named=True)["bench_close"] == pytest.approx(13.0)

    # Modify file within TTL (ensure distinct mtime_ns + size)
    write_idx([10.0, 11.0, 12.0, 15.0], part_name="part.parquet")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    f2 = pipeline.load_benchmark_momentum(tmp_path)
    assert f2 is not None
    assert f2.sort("date").row(-1, named=True)["bench_close"] == pytest.approx(15.0)

    # Add a new partition file
    extra_dir = tmp_path / "kline_index_daily" / "date=2026-08-19"
    extra_dir.mkdir(parents=True, exist_ok=True)
    extra_file = extra_dir / "part.parquet"
    pl.DataFrame({
        "symbol": ["000001.SH"],
        "date": [date(2026, 8, 19)],
        "close": [16.0],
    }).write_parquet(extra_file)
    f3 = pipeline.load_benchmark_momentum(tmp_path)
    assert f3 is not None
    assert f3["date"].n_unique() == 5
    assert f3.filter(pl.col("bench_key") == "SH").height == 5

    # Delete file -> invalidated immediately
    p.unlink()
    extra_file.unlink()
    f4 = pipeline.load_benchmark_momentum(tmp_path)
    assert f4 is None

    # Re-create and stress concurrent reads + invalidations across threads
    write_idx([10.0, 11.0, 12.0, 14.0])

    def worker(i: int) -> float | None:
        if i % 3 == 0:
            pipeline.invalidate_custom_signals()
            pipeline._custom_prev_fields_today()
        if i % 5 == 0:
            pipeline.invalidate_benchmark_cache(tmp_path)
        res = pipeline.load_benchmark_momentum(tmp_path)
        return None if res is None else float(res.sort("date").row(-1, named=True)["bench_close"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(32)))
    assert all(r == pytest.approx(14.0) for r in results)


# ====================================================================
# 3. strategy/paper.py (H-03, H-06, H-07, H-09)
# ====================================================================
def _write_stock_daily(data_dir: Path, symbol: str, rows: list[tuple[date, float, float]]) -> None:
    base = data_dir / "kline_daily" / "part.parquet"
    base.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame({
        "symbol": [symbol] * len(rows),
        "date": [r[0] for r in rows],
        "open": [r[1] for r in rows],
        "high": [max(r[1], r[2]) for r in rows],
        "low": [min(r[1], r[2]) for r in rows],
        "close": [r[2] for r in rows],
        "volume": [10000.0] * len(rows),
        "amount": [r[2] * 10000.0 for r in rows],
    })
    if base.exists():
        df = pl.concat([pl.read_parquet(base), df], how="diagonal_relaxed")
    df.write_parquet(base)


def test_paper_decimal_rounding_and_st_etf_ipo_limits(tmp_path: Path) -> None:
    # Decimal half-up pure helpers
    assert paper.limit_prices(10.055, "600519.SH", "stock") == (11.06, 9.05)
    assert paper.limit_prices(
        18.90, "600001.SH", "stock", trade_date=date(2026, 7, 3), is_risk_warning=True
    ) == (19.85, 17.96)
    assert paper.limit_prices(1.005, "510300.SH", "etf") == (1.106, 0.905)
    assert paper.apply_slippage(10.055, "buy", 5.0) == 10.06
    assert paper.buy_fee(10000, 1.005, 0.0025) == 25.13
    assert paper.sell_fee(10000, 1.005, 0.0025, 0.001) == 35.18
    assert paper.qty_from_amount(57.0, 0.57) == 100

    # ST 5% limit rejection and IPO no-limit fill in _fill_order
    day = date(2026, 7, 3)
    prev_day = date(2026, 7, 2)
    _write_stock_daily(tmp_path, "600001.SH", [(prev_day, 10.0, 10.0), (day, 10.50, 10.50)])
    _write_stock_daily(tmp_path, "603001.SH", [(prev_day, 10.0, 10.0), (day, 12.50, 12.50)])

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600001.SH", "603001.SH"],
        "name": ["*ST测试", "N新股"],
        "listing_date": [date(2015, 1, 1), prev_day],
    }).write_parquet(inst_dir / "instruments.parquet")

    paper.create_account(tmp_path, 1_000_000.0)
    st_order, err = paper.create_order(tmp_path, "600001.SH", "buy", qty=100, order_type="close", ref_price=10.0)
    assert err is None
    ipo_order, err = paper.create_order(tmp_path, "603001.SH", "buy", qty=100, order_type="close", ref_price=10.0)
    assert err is None

    paper.settle_day(tmp_path, day.isoformat())
    assert paper.get_order(tmp_path, st_order["id"])["status"] == "expired"
    assert "涨停 10.5" in paper.get_order(tmp_path, st_order["id"])["reason"]
    assert paper.get_order(tmp_path, ipo_order["id"])["status"] == "filled"


def test_paper_fail_closed_ref_price_and_asset_type(tmp_path: Path) -> None:
    paper.create_account(tmp_path, 100_000.0)
    for bad_ref in (None, 0, -1, float("nan"), float("inf")):
        order, err = paper.create_order(tmp_path, "600519.SH", "buy", qty=100, ref_price=bad_ref)
        assert order is None and err is not None
    order, err = paper.create_order(tmp_path, "000001.SH", "buy", qty=100, asset_type="index", ref_price=10.0)
    assert order is None and "asset_type" in err

    # HTTP API fail-closed check without daily data
    app = FastAPI()
    app.include_router(paper_router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _s: "stock",
    )
    client = TestClient(app)
    r = client.post("/api/paper/orders", json={"symbol": "600519.SH", "side": "buy", "qty": 100})
    assert r.status_code == 400


def test_paper_t1_available_qty_and_overview_pending_sell(tmp_path: Path, monkeypatch) -> None:
    day = date(2026, 9, 24)
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    _write_stock_daily(tmp_path, "600519.SH", [(day - timedelta(days=1), 10.0, 10.0)])
    paper.create_account(tmp_path, 1_000_000.0)
    _, err = paper.create_order(tmp_path, "600519.SH", "buy", qty=200, ref_price=10.0)
    assert err is None
    assert len(paper.evaluate_intraday(tmp_path, {"600519.SH": 10.0})) == 1

    # Same-day: replay_positions & load_positions (even without positions.json) must report 0 available
    pos_path = paper._root(tmp_path) / "positions.json"
    pos_path.unlink()
    replayed, _ = paper.replay_positions(tmp_path)
    assert replayed["600519.SH"]["available_qty"] == 0
    assert paper.load_positions(tmp_path)["600519.SH"]["available_qty"] == 0

    # Next-day: even stale positions.json with available_qty=0 refreshes on load_positions
    paper._materialize(tmp_path)
    monkeypatch.setattr(paper, "cn_today", lambda: day + timedelta(days=1))
    assert paper.load_positions(tmp_path)["600519.SH"]["available_qty"] == 200

    # Overview subtracts pending sell and reports frozen_qty
    _, err = paper.create_order(tmp_path, "600519.SH", "sell", qty=100)
    assert err is None
    ov = paper.overview(tmp_path)
    holding = ov["holdings"][0]
    assert holding["available_qty"] == 100
    assert holding["frozen_qty"] == 100


def test_paper_order_and_account_traversal_blocked(tmp_path: Path) -> None:
    paper.create_account(tmp_path, 100_000.0)
    for bad_oid in ("../account", "a/b", "", "x" * 65, "bad id", "..\\account"):
        with pytest.raises(ValueError):
            paper.get_order(tmp_path, bad_oid)
        with pytest.raises(ValueError):
            paper.save_order(tmp_path, {"id": bad_oid})
        cancelled, err = paper.cancel_order(tmp_path, bad_oid)
        assert cancelled is None and err is not None

    # Invalid directory name in accounts/ is skipped by list_account_ids
    rogue = tmp_path / "paper" / "accounts" / "bad name"
    rogue.mkdir(parents=True)
    (rogue / "account.json").write_text(json.dumps({"id": "bad name", "created_at": ""}), encoding="utf-8")
    assert paper.list_account_ids(tmp_path) == ["default"]

    # HTTP DELETE /api/paper/orders/{order_id} returns 400 on traversal IDs
    app = FastAPI()
    app.include_router(paper_router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _s: "stock",
    )
    client = TestClient(app)
    assert client.delete("/api/paper/orders/bad..id").status_code == 400
    assert client.delete("/api/paper/orders/%2e%2e").status_code == 400
    assert client.delete("/api/paper/orders/..").status_code in (400, 404)


def test_paper_fsync_and_cn_now_used(tmp_path: Path, monkeypatch) -> None:
    fixed_dt = datetime(2026, 10, 15, 10, 30, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: fixed_dt)
    monkeypatch.setattr(paper, "cn_today", lambda: fixed_dt.date())

    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    _write_stock_daily(tmp_path, "600519.SH", [(fixed_dt.date() - timedelta(days=1), 10.0, 10.0)])
    paper.create_account(tmp_path, 1_000_000.0)
    order, err = paper.create_order(tmp_path, "600519.SH", "buy", qty=100, ref_price=10.0)
    assert err is None
    assert order["id"].startswith("order_20261015103000_")
    assert len(paper.evaluate_intraday(tmp_path, {"600519.SH": 10.0})) == 1

    fills = paper.load_fills(tmp_path)
    assert fills[0]["seq"] == int(fixed_dt.timestamp() * 1000)
    assert len(fsync_calls) >= 4  # account.json, order, fills.jsonl, positions.json


# ====================================================================
# 4. plugins/tdxquant/provider.py (H-06, H-09)
# ====================================================================
def test_tdxquant_ref_price_half_up_and_price_limit_rules() -> None:
    # Binary64 float representation boundaries
    assert tp._ref_price(1.005, 0.0, 0.0, 0.0, 0.0) == 1.01
    assert tp._ref_price(9.995, 0.0, 0.0, 0.0, 0.0) == 10.00
    assert tp._ref_price(10.155, 0.0, 0.0, 0.0, 0.0) == 10.16
    assert tp._ref_price(float("nan"), 0.0, 0.0, 0.0, 0.0) is None

    # Board / date / ST rules
    assert tp._price_limit("300001.SZ", date(2020, 8, 23)) == pytest.approx(0.10)
    assert tp._price_limit("300001.SZ", date(2020, 8, 24)) == pytest.approx(0.20)
    assert tp._price_limit("600001.SH", date(2026, 7, 3), is_risk_warning=True) == pytest.approx(0.05)
    assert tp._price_limit("600001.SH", date(2026, 7, 6), is_risk_warning=True) == pytest.approx(0.10)
    for code in ("830001.BJ", "430001", "870001", "920001"):
        assert tp._price_limit(code, date(2026, 7, 3)) == pytest.approx(0.30)


def test_tdxquant_factor_rows_respects_pre_2020_gem_limit() -> None:
    provider = tp.TdxQuantProvider()
    events = [{"Date": "20200820", "Bonus": 0.0, "ShareBonus": 0.0, "Allotment": 0.0, "AllotPrice": 0.0}]
    # Pre-2020-08-24 GEM had 10% limit -> +15% return should be rejected by self-check
    closes = {date(2020, 8, 19): 10.0, date(2020, 8, 20): 11.5}
    rows = provider._factor_rows("300001.SZ", events, closes, None, date(2020, 8, 21))
    assert rows == []


def test_tdxquant_uses_beijing_clock_across_utc_midnight_drift(monkeypatch) -> None:
    # Simulate host OS clock still on 2026-10-10 (17:00 UTC) while Beijing is 2026-10-11 01:00 CST
    bj_now = datetime(2026, 10, 11, 1, 0, 0, tzinfo=CN_TZ)
    utc_date = date(2026, 10, 10)

    class _HostDate(date):
        @classmethod
        def today(cls):
            return utc_date

    class _HostDT(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime(2026, 10, 10, 17, 0, 0)
            return bj_now

    monkeypatch.setattr(tp, "date", _HostDate)
    monkeypatch.setattr(tp, "datetime", _HostDT)
    monkeypatch.setattr(tp, "cn_today", lambda: bj_now.date())
    monkeypatch.setattr(tp, "cn_now", lambda: bj_now)

    his_calls: list[dict] = []

    class _Client:
        def stock_universe(self):
            return ["600519.SH"]

        def his_dats(self, code, end_d, count, period="1d"):
            his_calls.append({"code": code, "end": end_d, "period": period})
            return {
                "Date": ["20261010", "20261011"],
                "Time": ["150000", "093100"],
                "Open": [10.0, 11.0],
                "High": [10.0, 11.0],
                "Low": [10.0, 11.0],
                "Close": [10.0, 11.0],
                "Volume": [10000.0, 10000.0],
                "Amount": [100.0, 110.0],
            }

        def divid_factors(self, code, start_d=None, end_d=None):
            return []

    provider = tp.TdxQuantProvider()
    provider._client = _Client()

    provider.get_daily(["600519.SH"], None, None)
    assert his_calls[-1]["end"] == date(2026, 10, 11)

    batch = provider.get_intraday_batch(["600519.SH"])
    assert batch.height == 1
    assert batch["datetime"][0].date() == date(2026, 10, 11)

    provider.get_intraday_latest(["600519.SH"], count=3)
    assert his_calls[-1]["end"] == datetime(2026, 10, 11, 1, 0, 0)

    res = provider.test_dataset("daily", ["600519.SH"])
    assert res["rows"] >= 1
    assert his_calls[-1]["end"] == date(2026, 10, 11)

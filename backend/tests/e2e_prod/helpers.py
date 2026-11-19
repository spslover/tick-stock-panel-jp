"""Deterministic synthetic A-share fixtures, ZIP archive builders, and HTTP/SSE helpers."""
from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from fastapi.testclient import TestClient


def trading_days(n_days: int, end_date: date | None = None) -> list[date]:
    """Return `n_days` deterministic Mon-Fri trading days ending on or before `end_date`."""
    cur = end_date or date(2026, 4, 15)
    days: list[date] = []
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
    days.reverse()
    return days


def make_daily_df(
    symbols: list[str],
    *,
    n_days: int = 80,
    end_date: date | None = None,
    base_prices: dict[str, float] | None = None,
    daily_step_pct: dict[str, float] | None = None,
    zero_range_symbols: set[str] | None = None,
) -> pl.DataFrame:
    """Build deterministic A-share daily OHLCV Polars DataFrame."""
    days = trading_days(n_days, end_date=end_date)
    rows: list[dict[str, Any]] = []
    zero_range = zero_range_symbols or set()
    for sym_idx, sym in enumerate(symbols):
        price = (base_prices or {}).get(sym, 10.0 + sym_idx * 2.5)
        step = (daily_step_pct or {}).get(sym, 0.004)
        for day_idx, d in enumerate(days):
            if sym in zero_range:
                o = h = l_ = c = round(price, 2)
            else:
                # Slight deterministic trend + mild oscillation for realistic indicators
                delta = step + (0.008 if day_idx % 5 == 0 else -0.002)
                c = round(price * (1.0 + delta), 2)
                o = round(price * (1.0 + delta * 0.3), 2)
                h = round(max(o, c) * 1.012, 2)
                l_ = round(min(o, c) * 0.988, 2)
                price = c
            vol = float(1_000_000 + (day_idx % 7) * 120_000)
            amt = round(vol * c, 2)
            rows.append({
                "symbol": sym,
                "date": d,
                "open": float(o),
                "high": float(h),
                "low": float(l_),
                "close": float(c),
                "volume": vol,
                "amount": amt,
            })
    return pl.DataFrame(rows).with_columns(
        pl.col("date").cast(pl.Date),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("amount").cast(pl.Float64),
    )


def make_adj_factor_df(
    symbols: list[str],
    days: list[date],
    *,
    split_date: date | None = None,
    split_factor: float = 2.0,
) -> pl.DataFrame:
    """Build normalized adj_factor DataFrame (`symbol`, `trade_date`, `ex_factor`)."""
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        for d in days:
            factor = split_factor if (split_date is not None and d >= split_date) else 1.0
            rows.append({"symbol": sym, "trade_date": d, "ex_factor": float(factor)})
    return pl.DataFrame(rows).with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("ex_factor").cast(pl.Float64),
    )


def make_minute_df(
    symbols: list[str],
    trade_date: date,
    *,
    base_price: float = 10.0,
    bars_per_symbol: int = 240,
) -> pl.DataFrame:
    """Build Beijing wall-clock naive 1m minute bars for A-share sessions (09:31..11:30, 13:01..15:00)."""
    session_times: list[datetime] = []
    am_start = datetime.combine(trade_date, time(9, 31))
    for i in range(120):
        session_times.append(am_start + timedelta(minutes=i))
    pm_start = datetime.combine(trade_date, time(13, 1))
    for i in range(120):
        session_times.append(pm_start + timedelta(minutes=i))
    session_times = session_times[:bars_per_symbol]

    rows: list[dict[str, Any]] = []
    for sym_idx, sym in enumerate(symbols):
        p = base_price + sym_idx * 1.5
        for bar_idx, dt in enumerate(session_times):
            c = round(p * (1.0 + 0.0002 * ((bar_idx % 5) - 2)), 3)
            rows.append({
                "symbol": sym,
                "datetime": dt,
                "open": float(p),
                "high": float(round(max(p, c) + 0.02, 3)),
                "low": float(round(min(p, c) - 0.02, 3)),
                "close": float(c),
                "volume": float(50_000 + bar_idx * 100),
                "amount": float(round((50_000 + bar_idx * 100) * c, 2)),
            })
            p = c
    return pl.DataFrame(rows).with_columns(
        pl.col("datetime").cast(pl.Datetime("us")),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
        pl.col("amount").cast(pl.Float64),
    )


def make_instruments_df(
    entries: list[dict[str, Any]],
    *,
    asset_type: str = "stock",
    source: str = "mock",
) -> pl.DataFrame:
    """Build normalized instruments DataFrame."""
    rows = []
    for e in entries:
        sym = e["symbol"]
        code, _, exch = sym.partition(".")
        rows.append({
            "symbol": sym,
            "name": e.get("name", f"股票{code}"),
            "code": code,
            "exchange": e.get("exchange", exch),
            "asset_type": e.get("asset_type", asset_type),
            "source": e.get("source", source),
            "float_shares": float(e.get("float_shares", 100_000_000.0)),
            "total_shares": float(e.get("total_shares", 150_000_000.0)),
        })
    return pl.DataFrame(rows)


def make_depth5_snapshot(
    symbol: str,
    price: float,
    *,
    bid_vol_lots: int = 120,
    ask_vol_lots: int = 80,
    sealed_up: bool = False,
    sealed_down: bool = False,
    ts: float | None = None,
) -> dict[str, Any]:
    """Build five-level order book record in lots (手, 1 lot = 100 shares)."""
    bids = [round(price - 0.01 * i, 3) for i in range(5)]
    asks = [round(price + 0.01 * (i + 1), 3) for i in range(5)]
    bid_vols = [bid_vol_lots * (5 - i) for i in range(5)]
    ask_vols = [ask_vol_lots * (i + 1) for i in range(5)]
    if sealed_up:
        asks = [0.0] * 5
        ask_vols = [0] * 5
        bids = [price] + [round(price - 0.01 * i, 3) for i in range(1, 5)]
    elif sealed_down:
        bids = [0.0] * 5
        bid_vols = [0] * 5
        asks = [price] + [round(price + 0.01 * i, 3) for i in range(1, 5)]
    return {
        "symbol": symbol,
        "ts": ts or 1_760_000_000.0,
        "bids": bids,
        "asks": asks,
        "bid_volumes": bid_vols,
        "ask_volumes": ask_vols,
    }


def make_pit_financials(
    symbol: str,
    *,
    period_end: date,
    announce_date: date,
    eps: float = 1.25,
    bps: float = 8.5,
    roe: float = 0.14,
    net_profit: float = 500_000_000.0,
    revenue: float = 2_000_000_000.0,
    float_shares: float = 100_000_000.0,
) -> dict[str, pl.DataFrame]:
    """Build 5-table PIT financial DataFrames (`metrics`, `income`, `balance_sheet`, `cash_flow`, `shares`)."""
    base = {"symbol": symbol, "period_end": period_end, "announce_date": announce_date}
    return {
        "metrics": pl.DataFrame([{**base, "eps": eps, "bps": bps, "roe": roe, "pe_ttm": 15.0}]),
        "income": pl.DataFrame([{**base, "revenue": revenue, "net_profit": net_profit}]),
        "balance_sheet": pl.DataFrame([{**base, "total_assets": 8_000_000_000.0, "total_equity": 4_000_000_000.0}]),
        "cash_flow": pl.DataFrame([{**base, "operating_cash_flow": 600_000_000.0}]),
        "shares": pl.DataFrame([{**base, "total_shares": float_shares * 1.2, "float_shares": float_shares}]),
    }


def seed_repo_market_data(
    data_dir: Path,
    *,
    stock_symbols: list[str] | None = None,
    etf_symbols: list[str] | None = None,
    index_symbols: list[str] | None = None,
    n_days: int = 80,
    end_date: date | None = None,
    with_minute: bool = False,
    with_financials: bool = False,
    st_symbols: set[str] | None = None,
    zero_range_symbols: set[str] | None = None,
) -> date:
    """Persist synthetic daily/instruments/enriched/minute/financials data cleanly into `data_dir`."""
    from app.indicators.pipeline import compute_indicators
    from app.tickflow.repository import DataStore, KlineRepository

    stocks = stock_symbols if stock_symbols is not None else ["000001.SZ", "600000.SH", "300750.SZ", "688981.SH"]
    etfs = etf_symbols if etf_symbols is not None else ["510300.SH", "159915.SZ"]
    indices = index_symbols if index_symbols is not None else ["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"]
    st_set = st_symbols or set()

    store = DataStore(data_dir)
    repo = KlineRepository(store)
    days = trading_days(n_days, end_date=end_date)
    latest_day = days[-1]

    if stocks:
        inst_rows = [
            {
                "symbol": s,
                "name": f"*ST测试{i}" if s in st_set else f"测试股{i}",
                "float_shares": 100_000_000.0,
            }
            for i, s in enumerate(stocks)
        ]
        inst_df = make_instruments_df(inst_rows, asset_type="stock")
        (data_dir / "instruments").mkdir(parents=True, exist_ok=True)
        inst_df.write_parquet(data_dir / "instruments" / "part.parquet")

        daily_df = make_daily_df(
            stocks,
            n_days=n_days,
            end_date=latest_day,
            zero_range_symbols=zero_range_symbols,
        )
        repo.append_daily(daily_df)
        enriched_df = compute_indicators(daily_df)
        repo.append_enriched(enriched_df)

        adj_df = make_adj_factor_df(stocks, days)
        (data_dir / "adj_factor").mkdir(parents=True, exist_ok=True)
        adj_df.write_parquet(data_dir / "adj_factor" / "part.parquet")

        if with_minute:
            min_df = make_minute_df(stocks, latest_day)
            min_dir = data_dir / "kline_minute" / f"date={latest_day.isoformat()}"
            min_dir.mkdir(parents=True, exist_ok=True)
            min_df.write_parquet(min_dir / "part.parquet")

        if with_financials:
            tables: dict[str, list[pl.DataFrame]] = {}
            ann = days[max(0, len(days) - 20)]
            period_end = ann - timedelta(days=30)
            for sym in stocks:
                pit = make_pit_financials(sym, period_end=period_end, announce_date=ann)
                for tbl, df in pit.items():
                    tables.setdefault(tbl, []).append(df)
            for tbl, dfs in tables.items():
                tdir = data_dir / "financials" / tbl
                tdir.mkdir(parents=True, exist_ok=True)
                pl.concat(dfs).write_parquet(tdir / "part.parquet")

    if etfs:
        etf_inst = make_instruments_df(
            [{"symbol": s, "name": f"ETF{i}"} for i, s in enumerate(etfs)],
            asset_type="etf",
        )
        repo.save_etf_instruments(etf_inst)
        etf_daily = make_daily_df(etfs, n_days=n_days, end_date=latest_day, base_prices={s: 2.5 for s in etfs})
        repo.append_etf_daily(etf_daily)
        etf_enriched = compute_indicators(etf_daily)
        repo.append_etf_enriched(etf_enriched)

        if with_minute:
            min_etf = make_minute_df(etfs, latest_day, base_price=2.5)
            min_dir = data_dir / "kline_etf_minute" / f"date={latest_day.isoformat()}"
            min_dir.mkdir(parents=True, exist_ok=True)
            min_etf.write_parquet(min_dir / "part.parquet")

    if indices:
        idx_inst = make_instruments_df(
            [{"symbol": s, "name": f"指数{i}"} for i, s in enumerate(indices)],
            asset_type="index",
        )
        repo.save_index_instruments(idx_inst)
        idx_daily = make_daily_df(indices, n_days=n_days, end_date=latest_day, base_prices={s: 3000.0 for s in indices})
        repo.append_index_daily(idx_daily)
        idx_enriched = compute_indicators(idx_daily)
        repo.append_index_enriched(idx_enriched)

    repo.rebuild_views()
    repo.refresh_cache(background=False)
    return latest_day


# ---------------------------------------------------------------------------
# ZIP Archive Builders
# ---------------------------------------------------------------------------

def build_zip_bytes(
    entries: dict[str, bytes | str],
    *,
    symlinks: dict[str, str] | None = None,
    lying_file_sizes: dict[str, int] | None = None,
    compress: bool = True,
) -> bytes:
    """Build ZIP archive bytes supporting raw path names, symlinks, and lying file_size headers."""
    buf = io.BytesIO()
    comp = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", compression=comp) as zf:
        for name, content in entries.items():
            raw = content.encode("utf-8") if isinstance(content, str) else content
            zi = zipfile.ZipInfo(filename=name, date_time=(2026, 4, 1, 12, 0, 0))
            zi.compress_type = comp
            zf.writestr(zi, raw)
        for name, target in (symlinks or {}).items():
            zi = zipfile.ZipInfo(filename=name, date_time=(2026, 4, 1, 12, 0, 0))
            zi.create_system = 3  # Unix
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(zi, target.encode("utf-8"))

    raw_zip = buf.getvalue()
    if not lying_file_sizes:
        return raw_zip

    # Patch uncompressed file_size in both local file header and central directory for bomb testing
    # using zipfile re-read/re-write of ZipInfo header bytes when needed.
    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
        infos = zf.infolist()
        for info in infos:
            if info.filename in lying_file_sizes:
                # Reconstruct is non-trivial with standard zipfile, so update file_size field directly in raw bytes
                # by matching local & central headers via CRC+orig size.
                orig_size = info.file_size
                fake_size = lying_file_sizes[info.filename]
                orig_le = orig_size.to_bytes(4, "little")
                fake_le = fake_size.to_bytes(4, "little")
                crc_le = info.CRC.to_bytes(4, "little")
                comp_le = info.compress_size.to_bytes(4, "little")
                sig_pair = crc_le + comp_le + orig_le
                patched = crc_le + comp_le + fake_le
                raw_zip = raw_zip.replace(sig_pair, patched)
    return raw_zip


def build_config_backup_zip(
    entries: dict[str, bytes | str],
    *,
    manifest_override: dict[str, Any] | None = None,
    lying_file_sizes: dict[str, int] | None = None,
) -> bytes:
    """Build a valid tick-config-backup v1 ZIP archive."""
    manifest = {
        "format": "tick-config-backup",
        "version": 1,
        "app_version": "0.3.37",
        "created_at": "2026-04-15T10:00:00+08:00",
    }
    if manifest_override:
        manifest.update(manifest_override)
    all_entries: dict[str, bytes | str] = {"manifest.json": json.dumps(manifest)}
    all_entries.update(entries)
    return build_zip_bytes(all_entries, lying_file_sizes=lying_file_sizes)


def build_zip_bomb_bytes(
    entry_name: str = "strategies/custom/custom_bomb.py",
    *,
    uncompressed_size: int = 12 * 1024 * 1024,
) -> bytes:
    """Build a tiny compressed ZIP whose payload inflates to `uncompressed_size` bytes."""
    payload = b"# " + (b"A" * (uncompressed_size - 2))
    return build_config_backup_zip({entry_name: payload})


def build_extension_package_zip(
    ext_id: str = "custom_demo_ext",
    version: str = "1.0.0",
    *,
    backend_code: str = "def setup(app):\n    return None\n",
    extra_entries: dict[str, bytes | str] | None = None,
    lying_file_sizes: dict[str, int] | None = None,
) -> tuple[bytes, str]:
    """Build a tick-extension-package ZIP and return `(zip_bytes, sha256_hex)`."""
    manifest = (
        "format: tick-extension-package\n"
        "format_version: 1\n"
        "extension_api_version: 1\n"
        f"id: {ext_id}\n"
        f"version: {version}\n"
        "name: Demo Extension\n"
        "backend: backend_mod.py\n"
    )
    entries: dict[str, bytes | str] = {
        "extension.yaml": manifest,
        "backend_mod.py": backend_code,
    }
    if extra_entries:
        entries.update(extra_entries)
    raw = build_zip_bytes(entries, lying_file_sizes=lying_file_sizes)
    return raw, hashlib.sha256(raw).hexdigest()


def build_source_package_zip(
    name: str = "mock_pkg_src",
    version: str = "1.0.0",
    *,
    extra_entries: dict[str, bytes | str] | None = None,
    lying_file_sizes: dict[str, int] | None = None,
) -> tuple[bytes, str]:
    """Build a custom data-source plugin package ZIP and return `(zip_bytes, sha256_hex)`."""
    manifest = (
        "plugin_api_version: 1\n"
        f"name: {name}\n"
        f"version: {version}\n"
        "display_name: Mock Package Source\n"
        "entry: mod:MockPluginProvider\n"
        "datasets:\n"
        "  - daily\n"
    )
    provider_code = (
        "from dataclasses import dataclass\n"
        "import polars as pl\n"
        "@dataclass(frozen=True)\n"
        "class _Caps:\n"
        "    instruments: bool = True\n"
        "    daily: bool = True\n"
        "    adj_factor: bool = False\n"
        "    minute: bool = False\n"
        "    realtime: bool = False\n"
        "    depth5: bool = False\n"
        "    financial: bool = False\n"
        "class MockPluginProvider:\n"
        f"    name = {name!r}\n"
        "    capabilities = _Caps()\n"
        "    def get_instruments(self, asset_type='stock'): return pl.DataFrame()\n"
        "    def get_daily(self, symbols, start_time=None, end_time=None, asset_type='stock'): return pl.DataFrame()\n"
    )
    entries: dict[str, bytes | str] = {
        "plugin.yaml": manifest,
        "mod.py": provider_code,
    }
    if extra_entries:
        entries.update(extra_entries)
    raw = build_zip_bytes(entries, lying_file_sizes=lying_file_sizes)
    return raw, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Auth, Token & SSE Helpers
# ---------------------------------------------------------------------------

def setup_and_login(client: TestClient, password: str = "StrongPass123!") -> str:
    """Ensure panel password is set and log in, returning the `tf_session` cookie value."""
    status = client.get("/api/auth/status").json()
    if not status["configured"]:
        r_setup = client.post("/api/auth/setup", json={"password": password})
        assert r_setup.status_code == 200, r_setup.text
    r_login = client.post("/api/auth/login", json={"password": password})
    assert r_login.status_code == 200, r_login.text
    cookie = r_login.cookies.get("tf_session") or client.cookies.get("tf_session") or ""
    assert cookie, "Expected tf_session cookie after login"
    return cookie


def create_api_token(client: TestClient, name: str, scopes: list[str]) -> tuple[dict[str, Any], str]:
    """Create a scoped Open API token using an authenticated UI session."""
    r = client.post("/api/settings/api-tokens", json={"name": name, "scopes": scopes})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["token"], body["plaintext"]


def parse_sse_frames(lines_iter: Any, *, max_frames: int = 5) -> list[dict[str, Any]]:
    """Parse SSE text lines into structured `{id, event, data}` frames."""
    frames: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for raw_line in lines_iter:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.rstrip("\r\n")
        if not line:
            if cur:
                frames.append(cur)
                cur = {}
                if len(frames) >= max_frames:
                    break
            continue
        if line.startswith(":"):
            continue
        if line.startswith("id:"):
            cur["id"] = line[3:].strip()
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            raw_data = line[5:].strip()
            try:
                cur["data"] = json.loads(raw_data)
            except json.JSONDecodeError:
                cur["data"] = raw_data
    if cur and len(frames) < max_frames:
        frames.append(cur)
    return frames

"""Tests for JP & US global market data normalization, indicators, levels, and caching."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from app.services import global_market_data as gmd


def _make_chart_payload(
    symbol: str,
    *,
    tz: str = "Asia/Tokyo",
    currency: str = "JPY",
    bars: int = 90,
    start_price: float = 3000.0,
) -> dict:
    base_dt = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
    timestamps = []
    opens, highs, lows, closes, vols = [], [], [], [], []
    price = start_price
    for i in range(bars):
        dt = base_dt + timedelta(days=i)
        timestamps.append(int(dt.timestamp()))
        o = price
        c = price + (12.0 if i % 3 != 0 else -8.0)
        h = max(o, c) + 15.0
        low_v = min(o, c) - 10.0
        opens.append(round(o, 2))
        highs.append(round(h, 2))
        lows.append(round(low_v, 2))
        closes.append(round(c, 2))
        vols.append(1_000_000 + i * 10_000)
        price = c

    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "shortName": f"Test {symbol}",
                        "longName": f"Test Company {symbol}",
                        "currency": currency,
                        "exchangeName": "JPX" if symbol.endswith(".T") else "NMS",
                        "exchangeTimezoneName": tz,
                        "regularMarketPrice": closes[-1],
                        "previousClose": closes[-2],
                        "fiftyTwoWeekHigh": max(highs),
                        "fiftyTwoWeekLow": min(lows),
                        "regularMarketVolume": vols[-1],
                    },
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": vols,
                            }
                        ],
                    },
                }
            ],
            "error": None,
        }
    }


class _FakeProvider:
    def __init__(self, *, fail_symbols: set[str] | None = None) -> None:
        self.fail_symbols = fail_symbols or set()
        self.chart_calls: list[str] = []
        self.search_calls: list[str] = []

    async def fetch_chart(self, symbol: str, range_str: str = "1y", interval: str = "1d") -> dict:
        self.chart_calls.append(symbol)
        if symbol in self.fail_symbols:
            raise gmd.GlobalMarketDataError(f"upstream failed for {symbol}")
        is_jp = symbol.endswith(".T") or symbol == "^N225" or symbol == "JPY=X"
        return _make_chart_payload(
            symbol,
            tz="Asia/Tokyo" if is_jp else "America/New_York",
            currency="JPY" if is_jp else "USD",
            start_price=3000.0 if is_jp else 150.0,
        )

    async def search(self, query: str, quotes_count: int = 12, news_count: int = 8) -> dict:
        self.search_calls.append(query)
        return {
            "quotes": [
                {
                    "symbol": "7203.T",
                    "shortname": "TOYOTA MOTOR CORP",
                    "exchange": "JPX",
                    "quoteType": "EQUITY",
                    "sectorDisp": "Consumer Cyclical",
                    "industryDisp": "Auto Manufacturers",
                },
                {
                    "symbol": "PLTR",
                    "shortname": "Palantir Technologies Inc.",
                    "exchange": "NMS",
                    "quoteType": "EQUITY",
                    "sectorDisp": "Technology",
                    "industryDisp": "Software - Infrastructure",
                },
            ],
            "news": [
                {
                    "title": f"Market update for {query}",
                    "publisher": "Wire",
                    "link": "https://example.com/news/1",
                    "providerPublishTime": 1760000000,
                }
            ],
        }


def test_symbol_normalization_jp_and_us() -> None:
    assert gmd.normalize_global_symbol("JP", "7203") == "7203.T"
    assert gmd.normalize_global_symbol("JP", "7203.jp") == "7203.T"
    assert gmd.normalize_global_symbol("JP", "285a") == "285A.T"
    assert gmd.normalize_global_symbol("JP", "^N225") == "^N225"
    assert gmd.normalize_global_symbol("US", "nvda") == "NVDA"
    assert gmd.normalize_global_symbol("US", "BRK.B") == "BRK-B"
    assert gmd.normalize_global_symbol("US", "AAPL.us") == "AAPL"
    with pytest.raises(ValueError):
        gmd.normalize_global_symbol("US", "")
    with pytest.raises(ValueError):
        gmd.normalize_global_symbol("JP", "bad symbol!")
    with pytest.raises(ValueError):
        gmd.normalize_market("CN")


def test_parse_chart_computes_percentage_change_and_indicators() -> None:
    payload = _make_chart_payload("7203.T", bars=90, start_price=3000.0)
    df, meta = gmd.parse_chart_payload("7203.T", "JP", payload)
    assert df.height == 90
    assert meta["currency"] == "JPY"
    expected_pct = round((meta["price"] / meta["prev_close"] - 1.0) * 100.0, 2)
    assert meta["change_pct"] == expected_pct

    enriched = gmd.enrich_global_df(df)
    for col in ("ma5", "ma20", "ma60", "macd_dif", "rsi_14", "boll_upper", "atr_14", "vol_ratio_5d"):
        assert col in enriched.columns
    # Verify vol_ratio_5d is unscaled daily volume ratio (>0)
    last_vr = float(enriched.tail(1)["vol_ratio_5d"][0])
    assert 0.5 < last_vr < 2.0


@pytest.mark.asyncio
async def test_market_overview_separates_benchmarks_sectors_and_leaders() -> None:
    prov = _FakeProvider()
    jp = await gmd.get_market_overview("JP", provider=prov)
    assert jp["market"] == "JP"
    assert len(jp["benchmarks"]) == 4
    assert len(jp["sectors"]) == 6
    assert len(jp["leaders"]) == 6
    assert jp["news"][0]["title"] == "Market update for Nikkei 225"
    assert jp["news"][0]["scope"] == "market"

    us = await gmd.get_market_overview("US", provider=prov)
    assert us["market"] == "US"
    assert any(b["symbol"] == "^GSPC" for b in us["benchmarks"])
    assert any(s["symbol"] == "XLK" for s in us["sectors"])


@pytest.mark.asyncio
async def test_market_overview_fails_closed_when_all_upstream_calls_fail() -> None:
    all_jp = {
        item["symbol"]
        for group in ("benchmarks", "sectors", "stocks")
        for item in gmd.MARKET_CATALOG["JP"][group]
    }
    prov = _FakeProvider(fail_symbols=all_jp)
    with pytest.raises(gmd.GlobalMarketDataError):
        await gmd.get_market_overview("JP", provider=prov)


@pytest.mark.asyncio
async def test_stock_detail_returns_levels_series_and_sector() -> None:
    prov = _FakeProvider()
    detail = await gmd.get_stock_detail("JP", "7203", provider=prov)
    assert detail["symbol"] == "7203.T"
    assert detail["quote"]["sector"] == "Consumer Cyclical"
    assert detail["quote"]["currency"] == "JPY"
    assert len(detail["rows"]) == 90
    assert "sr" in detail["levels"]
    assert "pivot" in detail["levels"]
    assert "boll" in detail["series"]
    assert len(detail["dates"]) == 90


class _NewsPolicyProvider(_FakeProvider):
    """Mirror upstream behaviour: verbose queries and JP tickers carry no headlines."""

    _LONG_NAMES: ClassVar[dict[str, str]] = {
        "7203.T": "TOYOTA MOTOR CORP",
        "6758.T": "SONY GROUP CORPORATION",
    }

    async def fetch_chart(self, symbol: str, range_str: str = "1y", interval: str = "1d") -> dict:
        payload = await super().fetch_chart(symbol, range_str, interval)
        name = self._LONG_NAMES.get(symbol)
        if name:
            payload["chart"]["result"][0]["meta"]["longName"] = name
            payload["chart"]["result"][0]["meta"]["shortName"] = name
        return payload

    async def search(self, query: str, quotes_count: int = 12, news_count: int = 8) -> dict:
        self.search_calls.append(query)
        base = await super().search(query, quotes_count, news_count)
        lowered = query.lower()
        if lowered in ("nikkei 225", "japan", "s&p 500", "nasdaq"):
            base["news"] = [
                {
                    "title": f"Market wrap for {query}",
                    "publisher": "Wire",
                    "link": "https://example.com/market",
                    "providerPublishTime": 1760000000,
                    "relatedTickers": ["^N225"],
                }
            ]
        elif lowered == "toyota motor":
            base["news"] = [
                {
                    "title": "Toyota Motor lifts guidance on hybrid demand",
                    "publisher": "MT Newswires",
                    "link": "https://example.com/toyota",
                    "providerPublishTime": 1760000100,
                    "relatedTickers": ["7203.T"],
                }
            ]
        else:
            base["news"] = []
        return base


@pytest.mark.asyncio
async def test_market_overview_uses_short_news_query_and_returns_headlines() -> None:
    prov = _NewsPolicyProvider()
    jp = await gmd.get_market_overview("JP", provider=prov)

    assert jp["news"], "market overview must carry headlines"
    assert jp["news"][0]["scope"] == "market"
    assert "Nikkei 225" in prov.search_calls
    # Verbose keyword queries hit an empty upstream result set.
    assert not [q for q in prov.search_calls if "Tokyo Stock Exchange" in q]


@pytest.mark.asyncio
async def test_stock_detail_falls_back_to_company_name_headlines() -> None:
    prov = _NewsPolicyProvider()
    detail = await gmd.get_stock_detail("JP", "7203", provider=prov)

    assert detail["news"], "JP ticker must still resolve headlines via company name"
    assert detail["news"][0]["scope"] == "ticker"
    assert "Toyota" in detail["news"][0]["title"]
    assert "TOYOTA MOTOR" in prov.search_calls


@pytest.mark.asyncio
async def test_stock_detail_degrades_to_market_scope_headlines() -> None:
    prov = _NewsPolicyProvider()
    detail = await gmd.get_stock_detail("JP", "6758", provider=prov)

    assert detail["news"], "unmatched ticker must degrade to market context"
    assert all(item["scope"] == "market" for item in detail["news"])


@pytest.mark.asyncio
async def test_search_filters_by_market() -> None:
    prov = _FakeProvider()
    jp_items = await gmd.search_symbols("JP", "toyota", provider=prov)
    assert any(i["symbol"] == "7203.T" for i in jp_items)
    assert all(i["symbol"] != "PLTR" for i in jp_items)

    us_items = await gmd.search_symbols("US", "palantir", provider=prov)
    assert any(i["symbol"] == "PLTR" for i in us_items)
    assert all(not i["symbol"].endswith(".T") for i in us_items)


@pytest.mark.asyncio
async def test_cache_reuses_default_provider_results(monkeypatch) -> None:
    gmd.clear_cache()
    prov = _FakeProvider()
    monkeypatch.setattr(gmd, "_default_provider", prov)

    await gmd.get_stock_detail("US", "NVDA")
    first_count = len(prov.chart_calls)
    await gmd.get_stock_detail("US", "nvda")
    assert len(prov.chart_calls) == first_count
    gmd.clear_cache()

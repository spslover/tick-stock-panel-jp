"""Japanese & US equity market data client, normalizer, and indicator builder.

Provides an explicit provider abstraction (`GlobalMarketProvider`) with
`YahooFinanceGlobalProvider` as the default implementation over `httpx`.
Fetched daily OHLCV bars are normalized into Polars DataFrames using the
exchange's local trading date (`Asia/Tokyo` for JP, `America/New_York` for US)
and enriched via the repository's shared `compute_indicators`, `compute_levels`,
and `build_level_series` functions.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from app.indicators.levels import build_level_series, compute_levels, summarize_levels
from app.indicators.pipeline import compute_indicators

logger = logging.getLogger(__name__)

GlobalMarket = Literal["JP", "US"]

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,24}$")
_JP_TSE_CODE_RE = re.compile(r"^[0-9]{3}[0-9A-Z]$")

_EXCHANGE_TZ: dict[GlobalMarket, str] = {
    "JP": "Asia/Tokyo",
    "US": "America/New_York",
}

_DEFAULT_CURRENCY: dict[GlobalMarket, str] = {
    "JP": "JPY",
    "US": "USD",
}

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Yahoo 搜索只对简短查询返回新闻：长关键词串会命中空结果集，
# 因此按市场维护候选查询，逐个回退到第一个有新闻的结果。
_MARKET_NEWS_QUERIES: dict[GlobalMarket, tuple[str, ...]] = {
    "JP": ("Nikkei 225", "Japan"),
    "US": ("S&P 500", "Nasdaq"),
}

# 公司全称里的通用法律形式/主体词，生成公司名查询时剔除，
# 避免把 "Corp" 之类的词当成品牌名去做标题相关性匹配。
_COMPANY_SUFFIX_TOKENS = {
    "corp", "corporation", "inc", "incorporated", "co", "company",
    "ltd", "limited", "plc", "llc", "ag", "sa", "nv", "kk",
    "group", "holding", "holdings", "partners",
}

# Indicator columns computed for global stocks; vol_ratio_5d is computed
# separately without A-share intraday elapsed-minute scaling.
_GLOBAL_INDICATOR_COLS: set[str] = {
    "ma5",
    "ma10",
    "ma20",
    "ma30",
    "ma60",
    "ema5",
    "ema10",
    "ema20",
    "ema60",
    "macd_dif",
    "macd_dea",
    "macd_hist",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "rsi_6",
    "rsi_14",
    "rsi_24",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "atr_14",
    "change_pct",
    "amplitude",
}

# Curated market catalogs (benchmarks, sectors, bellwether equities).
MARKET_CATALOG: dict[GlobalMarket, dict[str, list[dict[str, str]]]] = {
    "JP": {
        "benchmarks": [
            {"symbol": "^N225", "name_zh": "日经 225", "name_en": "Nikkei 225", "name_ja": "日経平均株価", "category": "index"},
            {"symbol": "1306.T", "name_zh": "东证指数 ETF", "name_en": "TOPIX ETF", "name_ja": "TOPIX連動ETF", "category": "index"},
            {"symbol": "2516.T", "name_zh": "东证成长 250", "name_en": "TSE Growth 250", "name_ja": "東証グロース250", "category": "index"},
            {"symbol": "JPY=X", "name_zh": "美元 / 日元", "name_en": "USD / JPY", "name_ja": "ドル円", "category": "macro"},
        ],
        "sectors": [
            {"symbol": "1625.T", "name_zh": "电器精密", "name_en": "Electric Appliances", "name_ja": "電機・精密", "category": "sector"},
            {"symbol": "1631.T", "name_zh": "银行", "name_en": "Banks", "name_ja": "銀行", "category": "sector"},
            {"symbol": "1622.T", "name_zh": "汽车运输", "name_en": "Automobiles", "name_ja": "自動車・輸送機", "category": "sector"},
            {"symbol": "1629.T", "name_zh": "商社批发", "name_en": "Trading Houses", "name_ja": "商社・卸売", "category": "sector"},
            {"symbol": "1626.T", "name_zh": "IT与服务", "name_en": "IT & Services", "name_ja": "情報通信・サービス", "category": "sector"},
            {"symbol": "1621.T", "name_zh": "医药品", "name_en": "Pharmaceuticals", "name_ja": "医薬品", "category": "sector"},
        ],
        "stocks": [
            {"symbol": "7203.T", "name_zh": "丰田汽车", "name_en": "Toyota Motor", "name_ja": "トヨタ自動車", "category": "stock"},
            {"symbol": "6758.T", "name_zh": "索尼集团", "name_en": "Sony Group", "name_ja": "ソニーグループ", "category": "stock"},
            {"symbol": "8035.T", "name_zh": "东京电子", "name_en": "Tokyo Electron", "name_ja": "東京エレクトロン", "category": "stock"},
            {"symbol": "9984.T", "name_zh": "软银集团", "name_en": "SoftBank Group", "name_ja": "ソフトバンクグループ", "category": "stock"},
            {"symbol": "8306.T", "name_zh": "三菱UFJ", "name_en": "MUFG", "name_ja": "三菱UFJ FG", "category": "stock"},
            {"symbol": "6861.T", "name_zh": "基恩士", "name_en": "Keyence", "name_ja": "キーエンス", "category": "stock"},
            {"symbol": "9983.T", "name_zh": "迅销 (优衣库)", "name_en": "Fast Retailing", "name_ja": "ファーストリテイリング", "category": "stock"},
            {"symbol": "6501.T", "name_zh": "日立制作所", "name_en": "Hitachi", "name_ja": "日立製作所", "category": "stock"},
            {"symbol": "7974.T", "name_zh": "任天堂", "name_en": "Nintendo", "name_ja": "任天堂", "category": "stock"},
            {"symbol": "4063.T", "name_zh": "信越化学", "name_en": "Shin-Etsu Chemical", "name_ja": "信越化学工業", "category": "stock"},
            {"symbol": "6857.T", "name_zh": "爱德万测试", "name_en": "Advantest", "name_ja": "アドバンテスト", "category": "stock"},
            {"symbol": "9432.T", "name_zh": "日本电信电话", "name_en": "NTT", "name_ja": "NTT", "category": "stock"},
        ],
    },
    "US": {
        "benchmarks": [
            {"symbol": "^GSPC", "name_zh": "标普 500", "name_en": "S&P 500", "name_ja": "S&P 500", "category": "index"},
            {"symbol": "^IXIC", "name_zh": "纳斯达克综合", "name_en": "Nasdaq Composite", "name_ja": "NASDAQ総合", "category": "index"},
            {"symbol": "^DJI", "name_zh": "道琼斯工业", "name_en": "Dow Jones", "name_ja": "NYダウ", "category": "index"},
            {"symbol": "^RUT", "name_zh": "罗素 2000", "name_en": "Russell 2000", "name_ja": "ラッセル2000", "category": "index"},
            {"symbol": "^VIX", "name_zh": "VIX 恐慌指数", "name_en": "CBOE VIX", "name_ja": "VIX指数", "category": "macro"},
            {"symbol": "^TNX", "name_zh": "美国 10 年期国债收益率", "name_en": "US 10Y Yield", "name_ja": "米10年国債利回り", "category": "macro"},
        ],
        "sectors": [
            {"symbol": "XLK", "name_zh": "科技板块", "name_en": "Technology", "name_ja": "テクノロジー", "category": "sector"},
            {"symbol": "SOXX", "name_zh": "半导体", "name_en": "Semiconductors", "name_ja": "半導体", "category": "sector"},
            {"symbol": "XLF", "name_zh": "金融板块", "name_en": "Financials", "name_ja": "金融", "category": "sector"},
            {"symbol": "XLV", "name_zh": "医疗健康", "name_en": "Health Care", "name_ja": "ヘルスケア", "category": "sector"},
            {"symbol": "XLE", "name_zh": "能源板块", "name_en": "Energy", "name_ja": "エネルギー", "category": "sector"},
            {"symbol": "XLI", "name_zh": "工业板块", "name_en": "Industrials", "name_ja": "資本財", "category": "sector"},
            {"symbol": "XLY", "name_zh": "可选消费", "name_en": "Consumer Disc.", "name_ja": "一般消費財", "category": "sector"},
            {"symbol": "XLP", "name_zh": "必选消费", "name_en": "Consumer Staples", "name_ja": "生活必需品", "category": "sector"},
        ],
        "stocks": [
            {"symbol": "NVDA", "name_zh": "英伟达", "name_en": "NVIDIA", "name_ja": "エヌビディア", "category": "stock"},
            {"symbol": "AAPL", "name_zh": "苹果", "name_en": "Apple", "name_ja": "アップル", "category": "stock"},
            {"symbol": "MSFT", "name_zh": "微软", "name_en": "Microsoft", "name_ja": "マイクロソフト", "category": "stock"},
            {"symbol": "GOOGL", "name_zh": "谷歌 Alphabet", "name_en": "Alphabet", "name_ja": "アルファベット", "category": "stock"},
            {"symbol": "AMZN", "name_zh": "亚马逊", "name_en": "Amazon", "name_ja": "アマゾン", "category": "stock"},
            {"symbol": "META", "name_zh": "Meta", "name_en": "Meta Platforms", "name_ja": "メタ", "category": "stock"},
            {"symbol": "TSLA", "name_zh": "特斯拉", "name_en": "Tesla", "name_ja": "テスラ", "category": "stock"},
            {"symbol": "AVGO", "name_zh": "博通", "name_en": "Broadcom", "name_ja": "ブロードコム", "category": "stock"},
            {"symbol": "TSM", "name_zh": "台积电 ADR", "name_en": "TSMC ADR", "name_ja": "TSMC ADR", "category": "stock"},
            {"symbol": "BRK-B", "name_zh": "伯克希尔 B", "name_en": "Berkshire Hathaway", "name_ja": "バークシャーB", "category": "stock"},
            {"symbol": "JPM", "name_zh": "摩根大通", "name_en": "JPMorgan Chase", "name_ja": "JPモルガン", "category": "stock"},
            {"symbol": "LLY", "name_zh": "礼来", "name_en": "Eli Lilly", "name_ja": "イーライリリー", "category": "stock"},
        ],
    },
}


class GlobalMarketDataError(RuntimeError):
    """Raised when an upstream global market data fetch fails or returns unusable data."""


class GlobalMarketProvider(Protocol):
    async def fetch_chart(self, symbol: str, range_str: str = "1y", interval: str = "1d") -> dict[str, Any]:
        """Fetch raw OHLCV chart payload for a single symbol."""

    async def search(self, query: str, quotes_count: int = 12, news_count: int = 8) -> dict[str, Any]:
        """Search global equities and recent news headlines."""


class YahooFinanceGlobalProvider:
    """Default global market provider backed by Yahoo Finance JSON endpoints."""

    def __init__(self, timeout_seconds: float = 12.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def fetch_chart(
        self, symbol: str, range_str: str = "1y", interval: str = "1d",
    ) -> dict[str, Any]:
        url = _YAHOO_CHART_URL.format(symbol=quote(symbol, safe=""))
        params = {"range": range_str, "interval": interval, "includePrePost": "false"}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=True, headers=_HTTP_HEADERS,
            ) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise GlobalMarketDataError(f"Failed to fetch chart for {symbol}: {exc}") from exc
        if resp.status_code != 200:
            raise GlobalMarketDataError(
                f"Upstream chart request failed for {symbol} (HTTP {resp.status_code})"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GlobalMarketDataError(f"Invalid JSON chart payload for {symbol}") from exc

        chart = payload.get("chart") or {}
        if chart.get("error"):
            err = chart["error"]
            desc = err.get("description") if isinstance(err, dict) else str(err)
            raise GlobalMarketDataError(f"Upstream error for {symbol}: {desc}")
        results = chart.get("result") or []
        if not results:
            raise GlobalMarketDataError(f"No chart data returned for {symbol}")
        return payload

    async def search(
        self, query: str, quotes_count: int = 12, news_count: int = 8,
    ) -> dict[str, Any]:
        params = {
            "q": query,
            "quotesCount": quotes_count,
            "newsCount": news_count,
            "enableFuzzyQuery": "false",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=True, headers=_HTTP_HEADERS,
            ) as client:
                resp = await client.get(_YAHOO_SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            raise GlobalMarketDataError(f"Search request failed: {exc}") from exc
        if resp.status_code != 200:
            raise GlobalMarketDataError(f"Upstream search failed (HTTP {resp.status_code})")
        try:
            return resp.json()
        except ValueError as exc:
            raise GlobalMarketDataError("Invalid JSON search response") from exc


def normalize_market(raw: str) -> GlobalMarket:
    val = str(raw or "").strip().upper()
    if val in ("JP", "US"):
        return val  # type: ignore[return-value]
    raise ValueError(f"Unsupported market: {raw!r} (expected JP or US)")


def normalize_global_symbol(market: GlobalMarket, raw: str) -> str:
    """Normalize user ticker input to Yahoo Finance convention for JP or US."""
    text = str(raw or "").strip().upper()
    if not text or not _SYMBOL_RE.fullmatch(text):
        raise ValueError(f"Invalid symbol: {raw!r}")

    if text.startswith("^") or text.endswith("=X"):
        return text

    if market == "JP":
        if text.endswith(".T"):
            return text
        if text.endswith(".JP") or text.endswith(".TYO"):
            return f"{text.rsplit('.', 1)[0]}.T"
        if _JP_TSE_CODE_RE.fullmatch(text):
            return f"{text}.T"
        return text

    # US market
    if text.endswith(".US"):
        text = text[:-3]
    if "." in text and not text.endswith(".T"):
        text = text.replace(".", "-")
    return text


def catalog_entry_map(market: GlobalMarket) -> dict[str, dict[str, str]]:
    cat = MARKET_CATALOG[market]
    out: dict[str, dict[str, str]] = {}
    for group in ("benchmarks", "sectors", "stocks"):
        for item in cat[group]:
            out[item["symbol"]] = item
    return out


def _finite(val: Any) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _safe_zoneinfo(tz_name: str | None, market: GlobalMarket) -> ZoneInfo:
    for candidate in (tz_name, _EXCHANGE_TZ[market], "UTC"):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except Exception:  # noqa: BLE001
                continue
    return ZoneInfo("UTC")


def parse_chart_payload(
    symbol: str,
    market: GlobalMarket,
    payload: dict[str, Any],
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Parse Yahoo Finance chart JSON into a sorted Polars OHLCV DataFrame + quote meta."""
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise GlobalMarketDataError(f"Empty chart result for {symbol}")

    res = results[0]
    meta = res.get("meta") or {}
    timestamps = res.get("timestamp") or []
    indicators = res.get("indicators") or {}
    quotes = (indicators.get("quote") or [{}])[0] or {}
    adj_list = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []

    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []

    tz = _safe_zoneinfo(meta.get("exchangeTimezoneName"), market)
    by_date: dict[str, dict[str, Any]] = {}

    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        c = _finite(closes[i] if i < len(closes) else None)
        if c is None or c <= 0:
            continue
        o = _finite(opens[i] if i < len(opens) else None) or c
        h = _finite(highs[i] if i < len(highs) else None) or max(o, c)
        low_v = _finite(lows[i] if i < len(lows) else None) or min(o, c)
        vol = _finite(volumes[i] if i < len(volumes) else None)
        adj = _finite(adj_list[i] if i < len(adj_list) else None)

        # Keep bar OHLC aligned when adjusted close differs materially (splits/dividends)
        if adj is not None and adj > 0 and c > 0:
            factor = adj / c
            if math.isfinite(factor) and 0.01 <= factor <= 100.0 and abs(factor - 1.0) > 1e-6:
                o = o * factor
                h = h * factor
                low_v = low_v * factor
                c = adj

        dt = datetime.fromtimestamp(int(ts), tz=tz).date().isoformat()
        by_date[dt] = {
            "symbol": symbol,
            "date": dt,
            "open": round(o, 4),
            "high": round(max(h, o, c), 4),
            "low": round(min(low_v, o, c), 4),
            "close": round(c, 4),
            "volume": float(vol) if vol is not None and vol >= 0 else 0.0,
        }

    if not by_date:
        raise GlobalMarketDataError(f"No valid OHLCV bars for {symbol}")

    rows = [by_date[k] for k in sorted(by_date.keys())]
    df = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date("%Y-%m-%d"))

    last_bar = rows[-1]
    prev_bar = rows[-2] if len(rows) >= 2 else None
    reg_price = _finite(meta.get("regularMarketPrice")) or last_bar["close"]
    prev_close = (
        _finite(meta.get("previousClose"))
        or (prev_bar["close"] if prev_bar else None)
        or _finite(meta.get("chartPreviousClose"))
    )

    change = None
    change_pct = None
    # Note: GlobalQuoteItem.change_pct uses percentage points (1.25 = +1.25%),
    # matching IndexQuote semantics in the rest of the repository.
    if reg_price is not None and prev_close is not None and prev_close > 0:
        change = round(reg_price - prev_close, 4)
        change_pct = round((reg_price / prev_close - 1.0) * 100.0, 2)
    else:
        meta_pct = _finite(meta.get("regularMarketChangePercent"))
        if meta_pct is not None:
            change_pct = round(meta_pct, 2)

    quote_meta = {
        "symbol": str(meta.get("symbol") or symbol),
        "name": str(meta.get("shortName") or meta.get("longName") or symbol),
        "long_name": str(meta.get("longName") or meta.get("shortName") or symbol),
        "exchange": str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
        "currency": str(meta.get("currency") or _DEFAULT_CURRENCY[market]),
        "price": round(reg_price, 4) if reg_price is not None else None,
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "change": change,
        "change_pct": change_pct,
        "high_52w": _finite(meta.get("fiftyTwoWeekHigh")),
        "low_52w": _finite(meta.get("fiftyTwoWeekLow")),
        "day_high": _finite(meta.get("regularMarketDayHigh")) or last_bar["high"],
        "day_low": _finite(meta.get("regularMarketDayLow")) or last_bar["low"],
        "volume": _finite(meta.get("regularMarketVolume")) or last_bar["volume"],
        "as_of": last_bar["date"],
    }
    return df, quote_meta


def enrich_global_df(df: pl.DataFrame) -> pl.DataFrame:
    """Compute technical indicators on global OHLCV bars without A-share intraday scaling."""
    if df.is_empty():
        return df
    enriched = compute_indicators(df, needed=set(_GLOBAL_INDICATOR_COLS), assume_sorted=True)
    # Full-day 5-day volume ratio (skipping A-share intraday elapsed-minute scaling)
    enriched = enriched.with_columns(
        pl.when(pl.col("volume").shift(1).rolling_mean(5).over("symbol") > 0)
        .then(pl.col("volume") / pl.col("volume").shift(1).rolling_mean(5).over("symbol"))
        .otherwise(None)
        .alias("vol_ratio_5d"),
    )
    return enriched


def _df_to_kline_rows(df: pl.DataFrame) -> list[dict[str, Any]]:
    """Serialize enriched Polars DataFrame into KlineRow-compatible JSON dicts."""
    keep = [
        "symbol", "date", "open", "high", "low", "close", "volume", "change_pct",
        "ma5", "ma10", "ma20", "ma60",
        "macd_dif", "macd_dea", "macd_hist",
        "kdj_k", "kdj_d", "kdj_j",
        "rsi_6", "rsi_14", "rsi_24",
        "boll_upper", "boll_mid", "boll_lower",
        "atr_14", "vol_ratio_5d", "amplitude",
    ]
    cols = [c for c in keep if c in df.columns]
    rows: list[dict[str, Any]] = []
    for rec in df.select(cols).to_dicts():
        clean: dict[str, Any] = {}
        for k, v in rec.items():
            if k == "date":
                clean[k] = v.isoformat() if hasattr(v, "isoformat") else str(v)
            elif isinstance(v, float):
                clean[k] = round(v, 4) if math.isfinite(v) else None
            else:
                clean[k] = v
        rows.append(clean)
    return rows


# ================================================================
# TTL Cache & Service Functions
# ================================================================

_CACHE_LOCK = asyncio.Lock()
_CACHE: dict[tuple[str, ...], tuple[float, Any]] = {}

_OVERVIEW_TTL_S = 60.0
_STOCK_TTL_S = 180.0
_SEARCH_TTL_S = 180.0

_default_provider: GlobalMarketProvider = YahooFinanceGlobalProvider()


def clear_cache() -> None:
    """Clear in-memory global market cache (used in tests)."""
    _CACHE.clear()


async def _cache_get(key: tuple[str, ...], ttl: float) -> Any | None:
    async with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        ts, val = entry
        if time.monotonic() - ts > ttl:
            _CACHE.pop(key, None)
            return None
        return val


async def _cache_set(key: tuple[str, ...], value: Any) -> None:
    async with _CACHE_LOCK:
        if len(_CACHE) > 256:
            # Drop oldest entries to bound memory
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:64]
            for k, _ in oldest:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.monotonic(), value)


async def _fetch_quote_summary_item(
    provider: GlobalMarketProvider,
    market: GlobalMarket,
    entry: dict[str, str],
) -> dict[str, Any] | None:
    try:
        payload = await provider.fetch_chart(entry["symbol"], range_str="1mo", interval="1d")
        _, meta = parse_chart_payload(entry["symbol"], market, payload)
        return {
            **entry,
            "price": meta["price"],
            "prev_close": meta["prev_close"],
            "change": meta["change"],
            "change_pct": meta["change_pct"],
            "currency": meta["currency"],
            "exchange": meta["exchange"],
            "as_of": meta["as_of"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("global quote fetch failed for %s: %s", entry["symbol"], exc)
        return None


def _clean_news(
    raw_news: list[dict[str, Any]] | None,
    *,
    scope: str = "market",
) -> list[dict[str, Any]]:
    """Normalize upstream headlines and tag their scope.

    scope="ticker" means the headline was matched to the requested symbol or
    company; scope="market" means it is broader market context that must not
    be attributed to a specific ticker.
    """
    out: list[dict[str, Any]] = []
    for item in raw_news or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        ts = item.get("providerPublishTime")
        published = None
        if isinstance(ts, (int, float)) and ts > 0:
            published = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC")).isoformat()
        related = item.get("relatedTickers")
        out.append({
            "title": title,
            "publisher": str(item.get("publisher") or ""),
            "link": str(item.get("link") or ""),
            "published_at": published,
            "scope": scope,
            "related_tickers": [str(t) for t in related] if isinstance(related, list) else [],
        })
    return out[:8]


def _company_query(name: str) -> str:
    """Build a searchable company name from an upstream long name."""
    tokens = [tok for tok in re.split(r"[\s,]+", str(name or "").strip()) if tok]
    kept = [tok for tok in tokens if tok.strip(".,").lower() not in _COMPANY_SUFFIX_TOKENS]
    return " ".join(kept[:3]).strip()


def _brand_token(company_query: str) -> str:
    """Pick the most distinctive token of a company name for headline matching."""
    tokens = [tok.strip(".,").lower() for tok in company_query.split() if len(tok.strip(".,")) >= 3]
    if not tokens:
        return ""
    return max(tokens, key=len)


def _matches_brand(item: dict[str, Any], symbol: str, brand: str) -> bool:
    """Check whether a headline is actually about the requested symbol/company."""
    tickers = {str(t).upper() for t in item.get("related_tickers") or []}
    if symbol.upper() in tickers:
        return True
    if not brand:
        return False
    return brand in str(item.get("title") or "").lower()


async def _fetch_market_news(
    provider: GlobalMarketProvider,
    market: GlobalMarket,
) -> list[dict[str, Any]]:
    """Fetch market-level headlines, trying short queries until upstream returns results."""
    for query in _MARKET_NEWS_QUERIES[market]:
        try:
            payload = await provider.search(query, quotes_count=0, news_count=8)
        except Exception as exc:  # noqa: BLE001
            logger.warning("global market news search failed (%s, %s): %s", market, query, exc)
            continue
        items = _clean_news(
            payload.get("news") if isinstance(payload, dict) else None,
            scope="market",
        )
        if items:
            return items
    return []


async def _fetch_ticker_news(
    provider: GlobalMarketProvider,
    market: GlobalMarket,
    symbol: str,
    quote_meta: dict[str, Any],
    search_res: Any,
) -> list[dict[str, Any]]:
    """Resolve ticker-specific headlines, falling back to company name then market context.

    Yahoo returns no headlines for most Japanese tickers (e.g. 7203.T), so the
    company name is used as a second query. Anything that still cannot be tied
    to the ticker degrades to market-scope context instead of being dropped
    silently or, worse, being attributed to the company.
    """
    raw = search_res.get("news") if isinstance(search_res, dict) else None
    items = _clean_news(raw, scope="ticker")
    company_query = _company_query(quote_meta.get("long_name") or quote_meta.get("name") or "")
    brand = _brand_token(company_query)

    if brand:
        items = [it for it in items if _matches_brand(it, symbol, brand)]
    if items:
        return items

    if company_query:
        try:
            payload = await provider.search(company_query, quotes_count=0, news_count=6)
        except Exception as exc:  # noqa: BLE001
            logger.warning("global company news search failed for %s: %s", symbol, exc)
            payload = None
        if isinstance(payload, dict):
            company_items = [
                it for it in _clean_news(payload.get("news"), scope="ticker")
                if _matches_brand(it, symbol, brand)
            ]
            if company_items:
                return company_items

    return await _fetch_market_news(provider, market)


async def get_market_overview(
    market_raw: str,
    *,
    provider: GlobalMarketProvider | None = None,
) -> dict[str, Any]:
    """Return benchmark indices, sector ETFs, representative stocks, and news for JP or US."""
    market = normalize_market(market_raw)
    prov = provider or _default_provider

    if provider is None:
        cached = await _cache_get(("overview", market), _OVERVIEW_TTL_S)
        if cached is not None:
            return cached

    cat = MARKET_CATALOG[market]
    all_entries = [*cat["benchmarks"], *cat["sectors"], *cat["stocks"][:6]]

    quote_results = await asyncio.gather(
        *[_fetch_quote_summary_item(prov, market, e) for e in all_entries],
        return_exceptions=True,
    )

    benchmarks: list[dict[str, Any]] = []
    sectors: list[dict[str, Any]] = []
    leaders: list[dict[str, Any]] = []

    n_bench = len(cat["benchmarks"])
    n_sect = len(cat["sectors"])

    for idx, item in enumerate(quote_results):
        if not isinstance(item, dict):
            continue
        if idx < n_bench:
            benchmarks.append(item)
        elif idx < n_bench + n_sect:
            sectors.append(item)
        else:
            leaders.append(item)

    if not benchmarks and not sectors:
        raise GlobalMarketDataError(f"Unable to load {market} market quotes from data provider")

    sectors.sort(key=lambda x: x.get("change_pct") if x.get("change_pct") is not None else -999.0, reverse=True)
    news = await _fetch_market_news(prov, market)
    as_of_dates = [b["as_of"] for b in benchmarks if b.get("as_of")]
    as_of = max(as_of_dates) if as_of_dates else None

    payload = {
        "market": market,
        "as_of": as_of,
        "benchmarks": benchmarks,
        "sectors": sectors,
        "leaders": leaders,
        "news": news,
        "catalog_stocks": cat["stocks"],
    }
    if provider is None:
        await _cache_set(("overview", market), payload)
    return payload


async def get_stock_detail(
    market_raw: str,
    symbol_raw: str,
    *,
    days: int = 250,
    provider: GlobalMarketProvider | None = None,
) -> dict[str, Any]:
    """Fetch 1y daily K-lines, compute indicators & key price levels, and attach recent news."""
    market = normalize_market(market_raw)
    symbol = normalize_global_symbol(market, symbol_raw)
    prov = provider or _default_provider

    if provider is None:
        cached = await _cache_get(("stock", market, symbol, str(days)), _STOCK_TTL_S)
        if cached is not None:
            return cached

    range_str = "2y" if days > 250 else "1y"
    chart_task = prov.fetch_chart(symbol, range_str=range_str, interval="1d")
    search_task = prov.search(symbol, quotes_count=3, news_count=6)
    chart_res, search_res = await asyncio.gather(chart_task, search_task, return_exceptions=True)

    if isinstance(chart_res, Exception):
        raise chart_res

    df, quote_meta = parse_chart_payload(symbol, market, chart_res)
    enriched = enrich_global_df(df).tail(days)
    levels = compute_levels(enriched)
    close = float(enriched.tail(1)["close"][0]) if "close" in enriched.columns and not enriched.is_empty() else None
    series = build_level_series(enriched)
    dates = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in enriched["date"].to_list()]
    rows = _df_to_kline_rows(enriched)

    sector = ""
    industry = ""
    if isinstance(search_res, dict):
        for q in search_res.get("quotes") or []:
            if str(q.get("symbol") or "").upper() == symbol:
                sector = str(q.get("sectorDisp") or q.get("sector") or "")
                industry = str(q.get("industryDisp") or q.get("industry") or "")
                break

    news = await _fetch_ticker_news(prov, market, symbol, quote_meta, search_res)

    cat_entry = catalog_entry_map(market).get(symbol)
    result = {
        "market": market,
        "symbol": symbol,
        "quote": {
            **quote_meta,
            "sector": sector,
            "industry": industry,
            "name_zh": cat_entry["name_zh"] if cat_entry else quote_meta["name"],
            "name_en": cat_entry["name_en"] if cat_entry else quote_meta["name"],
            "name_ja": cat_entry["name_ja"] if cat_entry else quote_meta["name"],
        },
        "rows": rows,
        "levels": levels,
        "series": series,
        "dates": dates,
        "close": close,
        "summary": summarize_levels(levels, close),
        "news": news,
    }
    if provider is None:
        await _cache_set(("stock", market, symbol, str(days)), result)
    return result


def _matches_market_quote(market: GlobalMarket, item: dict[str, Any]) -> bool:
    sym = str(item.get("symbol") or "").upper()
    exch = str(item.get("exchange") or "").upper()
    qtype = str(item.get("quoteType") or "").upper()
    if qtype not in ("EQUITY", "ETF", "INDEX", ""):
        return False
    if market == "JP":
        return sym.endswith(".T") or exch in ("JPX", "OSA", "FKA", "SAP") or sym == "^N225"
    # US
    return (
        "." not in sym
        and exch in ("NMS", "NYQ", "ASE", "NGM", "NCM", "PCX", "BTS", "NASDAQ", "NYSE", "")
    )


async def search_symbols(
    market_raw: str,
    query: str,
    *,
    provider: GlobalMarketProvider | None = None,
) -> list[dict[str, str]]:
    """Search symbols for JP or US, prioritizing curated catalog matches + live search."""
    market = normalize_market(market_raw)
    q = str(query or "").strip()
    if not q:
        return MARKET_CATALOG[market]["stocks"]

    prov = provider or _default_provider
    q_lower = q.lower()

    if provider is None:
        cached = await _cache_get(("search", market, q_lower), _SEARCH_TTL_S)
        if cached is not None:
            return cached

    matches: list[dict[str, str]] = []
    seen: set[str] = set()
    cat = MARKET_CATALOG[market]
    for group in ("stocks", "sectors", "benchmarks"):
        for item in cat[group]:
            hay = " ".join(
                [item["symbol"], item["name_zh"], item["name_en"], item["name_ja"]]
            ).lower()
            if q_lower in hay:
                matches.append(item)
                seen.add(item["symbol"])

    # Also try normalized direct ticker + Yahoo search
    search_q = q
    if market == "JP" and _JP_TSE_CODE_RE.fullmatch(q.upper()):
        search_q = f"{q.upper()}.T"

    try:
        res = await prov.search(search_q, quotes_count=12, news_count=0)
        for item in res.get("quotes") or []:
            if not _matches_market_quote(market, item):
                continue
            sym = str(item.get("symbol") or "").upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            name = str(item.get("shortname") or item.get("longname") or sym)
            matches.append({
                "symbol": sym,
                "name_zh": name,
                "name_en": name,
                "name_ja": name,
                "category": str(item.get("quoteType") or "EQUITY").lower(),
                "exchange": str(item.get("exchDisp") or item.get("exchange") or ""),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("global symbol search error (%s, %s): %s", market, q, exc)

    out = matches[:15]
    if provider is None:
        await _cache_set(("search", market, q_lower), out)
    return out

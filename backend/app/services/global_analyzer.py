"""AI analysis service for Japanese (JP) and US (US) equity markets and stocks.

Streams NDJSON events (`meta`, `delta`, `error`, `done`) via `stream_ai_text`
using real fetched market/stock OHLCV, technical indicators, key price levels,
and recent news headlines. Generates objective, non-advisory reports in
Simplified Chinese (`zh-CN`), English (`en`), or Japanese (`ja`).
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from app.services.ai_provider import sanitize_focus
from app.services.global_market_data import (
    GlobalMarket,
    GlobalMarketDataError,
    GlobalMarketProvider,
    get_market_overview,
    get_stock_detail,
    normalize_market,
)

logger = logging.getLogger(__name__)

SupportedLocale = Literal["zh-CN", "en", "ja"]
_KLINE_PROMPT_TAIL = 60


def normalize_locale(raw: str | None) -> SupportedLocale:
    val = str(raw or "").strip()
    if val in ("en", "ja", "zh-CN"):
        return val  # type: ignore[return-value]
    if val.lower().startswith("ja"):
        return "ja"
    if val.lower().startswith("en"):
        return "en"
    return "zh-CN"


def _localized_focus_block(focus: str, locale: SupportedLocale) -> str:
    text = sanitize_focus(focus)
    if not text:
        return ""
    if locale == "en":
        return (
            "## User Focus (Must Address First)\n"
            f"User question: {text}\n"
            "Begin your report with `### 0. 🔎 Focus Response` using 2-4 data-backed bullet points. "
            "If the question asks for buy/sell advice, target prices, or position sizing, "
            "convert it into objective technical levels, risk factors, and conditional scenarios without giving trading instructions."
        )
    if locale == "ja":
        return (
            "## ユーザー注目ポイント（冒頭で必ず回答）\n"
            f"注目点: {text}\n"
            "レポートの最初に `### 0. 🔎 注目ポイントへの回答` を設け、具体的な数値に基づく2〜4点の客観的な分析を記述してください。"
            "売買判断・目標株価・ポジション助言を求める内容であっても、売買指示は出さず、客観的なテクニカル水準・リスク要因・条件別シナリオへ変換して回答してください。"
        )
    return (
        "## 用户关注重点（必须优先回应）\n"
        f"用户关注: {text}\n"
        "请在报告最前面先输出 `### 0. 🔎 关注重点回应`，用 2-4 条带具体数据的结论直接回应；"
        "若涉及买卖、仓位、目标价或预测，不得给出操作指令，请转换为客观技术/基本面状态、关键价位与风险情景。"
    )


def _language_rule(locale: SupportedLocale) -> str:
    if locale == "en":
        return "Write the entire report in clear, institutional-grade **English**."
    if locale == "ja":
        return "レポート全体は自然な金融・証券アナリスト向けの**日本語**で出力してください。"
    return "请使用清晰、专业的**简体中文**输出整份报告。"


def _market_system_prompt(market: GlobalMarket, locale: SupportedLocale) -> str:
    market_desc = (
        "Japanese equity market (Tokyo Stock Exchange: Nikkei 225, TOPIX, TSE Growth 250, USD/JPY FX sensitivity, BOJ policy & sector rotation)"
        if market == "JP"
        else "US equity market (NYSE/NASDAQ: S&P 500, Nasdaq Composite, Dow Jones, Russell 2000 breadth, CBOE VIX, US 10Y Treasury yield & GICS sector rotation)"
    )

    if locale == "en":
        return f"""You are a senior institutional equity strategist specializing in the {market_desc}.
Produce an **objective, neutral, data-driven market analysis report** with **NO buy/sell/position instructions**.

## Compliance Rules
- NEVER output trading instructions such as buy, sell, accumulate, trim, overweight, underweight, stop-loss, take-profit, or target price recommendations.
- Describe market structure, index divergence, macro/FX/rate context, sector rotation, and risk factors objectively as a neutral financial analyst.

## Output Format (Markdown only, no JSON/code blocks)
### 1. 🎯 Market Regime Snapshot
1-2 sentences summarizing the core market theme, ending with `[Market Tone: Bullish / Neutral / Bearish]`.
### 2. 📊 Benchmarks & Macro Context
Analyze major indices, breadth (large-cap vs small/growth), and macro anchors ({'USD/JPY & domestic vs exporter tilt' if market == 'JP' else 'VIX volatility regime & US 10Y yield'}).
### 3. 🔄 Sector Rotation & Leadership
Analyze leading vs lagging sector ETFs and representative bellwethers, highlighting capital concentration or defensive rotation.
### 4. 📰 News & Macro Catalysts
Synthesize the provided headlines into key macro/policy/earnings drivers without inventing unprovided news.
### 5. 📌 Key Signals to Watch & Risk Factors
List objective technical/macro indicators and structural risks to monitor next.
End with: `> ⚠️ AI-generated market analysis based on public market data for informational purposes only. Not investment advice.`

{_language_rule(locale)}"""

    if locale == "ja":
        return f"""あなたは{market_desc}を専門とするシニア株式ストラテジストです。
提供された市場データとニュースに基づき、**売買指示を一切含まない客観的・中立的なマーケット分析レポート**を作成してください。

## 厳守事項（コンプライアンス）
- 「買い」「売り」「押し目買い」「利確」「損切り」「推奨銘柄」「目標株価」などの売買指示・投資助言は**絶対に出力しないでください**。
- 指数間の強弱、為替・金利などのマクロ環境、セクターローテーション、リスク要因を中立的な立場で事実ベースに記述してください。

## 出力構成（Markdown のみ、JSON・コードブロック不可）
### 1. 🎯 市場サマリー（1〜2文）
本日の市場テーマを要約し、末尾に `【地合い: 強気 / 中立 / 弱気】` を付記してください。
### 2. 📊 主要指数・マクロ環境
主要指数の動向、大型株と中小型/グロース株の広がり、マクロ指標（{'ドル円為替・日銀金融政策/金利感応度' if market == 'JP' else 'VIX恐怖指数・米10年債利回り・金融環境'}）を数値付きで解説してください。
### 3. 🔄 セクターローテーションと主力銘柄
セクター別ETFや主要大型株の騰落から、資金の向かう先（リスクオン/ディフェンシブ/外需・内需）を分析してください。
### 4. 📰 ニュース・カタリスト整理
提供された最新ニュース見出しをもとに、市場に影響を与えている材料を整理してください（未提供の事実を捏造しないこと）。
### 5. 📌 今後の注目指標とリスク要因
今後注視すべきテクニカル節目やマクロ上のリスクを客観的に列挙してください。
末尾に付記：`> ⚠️ 本レポートは公開市場データに基づきAIが生成した客観的分析であり、投資助言や売買推奨を目的としたものではありません。`

{_language_rule(locale)}"""

    # Simplified Chinese
    market_cn = (
        "日本股市（东证 TSE：日经225、TOPIX、东证成长250、美元兑日元汇率联动、日银政策与行业轮动）"
        if market == "JP"
        else "美国股市（NYSE/NASDAQ：标普500、纳斯达克、道琼斯、罗素2000宽度、VIX恐慌指数、美债10Y收益率与行业轮动）"
    )
    return f"""你是一位拥有 15 年海外市场投研经验的资深策略分析师，专注于{market_cn}。
请基于提供的真实行情与新闻数据，产出一份**客观、中立、不包含任何买卖或操作建议**的市场分析报告。

## 核心红线（务必遵守）
- **绝对不输出**“买入/卖出/加仓/减仓/抄底/逃顶/止损/止盈/仓位建议/目标价”等任何交易指令或倾向性操作建议。
- 仅客观陈述指数结构、宏观利率/汇率背景、板块轮动特征、资金偏好与潜在风险。

## 输出规范（仅输出 Markdown 正文，不输出 JSON 或代码块）
### 1. 🎯 一句话定调（1-2 句）
概括当前市场核心主线与强弱特征，末尾用【市场状态: 偏强 / 中性 / 偏弱】归纳。
### 2. 📊 指数与宏观环境
对比主要指数表现、大盘与中小盘分化，并结合{'美元兑日元汇率、外需出口与内需分化及日银利率预期' if market == 'JP' else 'VIX 波动率水平与美债 10 年期收益率对估值的影响'}展开数据化分析。
### 3. 🔄 板块轮动与代表权重表现
基于行业 ETF 与代表性龙头股涨跌幅，梳理领涨/领跌主线与资金风格切换特征。
### 4. 📰 消息面与催化梳理
结合提供的最新新闻标题客观提炼市场催化与扰动因素；严禁编造未提供的新闻事实。
### 5. 📌 后续观察要点与风险提示
列出后续需重点跟踪的技术位、宏观数据或汇率/利率风险点。
末尾附一行：`> ⚠️ 本内容由 AI 基于公开行情数据生成，仅客观陈述市场状态，不构成任何投资建议或买卖指令。`

{_language_rule(locale)}"""


def _stock_system_prompt(market: GlobalMarket, locale: SupportedLocale) -> str:
    market_name = (
        "Japanese equities (TSE)" if market == "JP" else "US equities (NYSE/NASDAQ)"
    )

    if locale == "en":
        return f"""You are a senior technical and equity research analyst covering {market_name}.
Produce an **objective, data-driven single-stock analysis report** with **NO buy/sell/position instructions**.

## Compliance Rules
- NEVER output buy/sell/hold recommendations, entry zones, stop-loss orders, take-profit targets, or price targets.
- Cite concrete prices, indicator values, and support/resistance levels from the provided data.

## Output Format (Markdown only)
### 1. 🎯 Technical State Summary
1-2 sentences summarizing the current chart posture, ending with `[Technical State: Stabilizing / Rebounding / Consolidating / Pulling Back / Weakening]`.
### 2. 📈 Technical & Volume-Price Analysis
Analyze moving averages (MA5/10/20/60), trend structure, MACD, KDJ, RSI, Bollinger Bands, ATR volatility, and 5-day volume ratio with exact values.
### 3. 💰 Key Price Levels
List objective upper resistance levels and lower support levels based on the supplied levels summary (Volume Profile S/R, Pivot, 60d/250d extremes, gaps, Fibonacci).
### 4. 🏭 Market Context, Valuation Range & Catalysts
Review 52-week range positioning, currency/macro sensitivity ({'JPY FX & TSE export/domestic sensitivity' if market == 'JP' else 'USD macro/rate & sector sensitivity'}), sector/industry context, and the provided news headlines (do not invent unprovided financials or news). Respect the `[ticker]` / `[market]` scope tags and never attribute `[market]` headlines to this ticker.
### 5. ⚖️ Structural Risk Summary
Summarize upside/downside space asymmetry and key technical/macro risk triggers to monitor.
End with: `> ⚠️ AI-generated stock analysis based on public market data for informational purposes only. Not investment advice.`

{_language_rule(locale)}"""

    if locale == "ja":
        market_jp = "日本株（東証プライム・スタンダード・グロース）" if market == "JP" else "米国株（NYSE・NASDAQ）"
        return f"""あなたは{market_jp}を担当するシニア・テクニカル＆エクイティアナリストです。
提供された日足・テクニカル指標・意識される価格帯・ニュースに基づき、**売買指示を一切含まない客観的な銘柄分析レポート**を作成してください。

## 厳守事項（コンプライアンス）
- 「買い」「売り」「押し目」「損切りライン」「利確目標」「目標株価」などの売買指示や投資助言は**絶対に出力しないでください**。
- 提供された数値（終値、移動平均、MACD、RSI、サポート/レジスタンス）を正確に引用し、事実ベースで記述してください。

## 出力構成（Markdown のみ）
### 1. 🎯 テクニカル概況（1〜2文）
現在のチャート形状を簡潔に要約し、末尾に `【現状: 底固め / 反発 / 揉み合い / 調整 / 軟調】` を付記してください。
### 2. 📈 テクニカル・出来高分析
移動平均線（MA5/10/20/60）の並び順、MACD、KDJ、RSI、ボリンジャーバンド、ATR、5日出来高比率などを具体的な数値を引用して客観分析してください。
### 3. 💰 意識される重要価格帯
提供された価格帯データに基づき、上値抵抗線（レジスタンス）と下値支持線（サポート）を水準・根拠別に整理してください。
### 4. 🏭 市場位置づけ・52週レンジ・材料整理
52週高値/安値レンジ内の現在地、セクター・業種背景、為替/金利感応度、および提供された最新ニュース見出しを整理してください(未提供の財務数値やニュースを捏造しないこと)。スコープタグ(`[ticker]`/`[market]`)を尊重し、`[market]`の見出しを当該銘柄固有の材料として扱わないこと。
### 5. ⚖️ リスク要因と今後の注目点
上下価格帯までの距離の非対称性や、今後注目すべきテクニカル・外部環境リスクを整理してください。
末尾に付記：`> ⚠️ 本レポートは公開市場データに基づきAIが生成した客観的分析であり、投資助言や売買推奨を目的としたものではありません。`

{_language_rule(locale)}"""

    market_cn = "日本股市（东证 TSE）" if market == "JP" else "美国股市（NYSE/NASDAQ）"
    return f"""你是一位拥有 15 年海外市场一线研究经验的资深技术与权益分析师，专注于{market_cn}。
请基于提供的个股日 K、技术指标、关键价位与最新资讯，产出一份**客观、中立、不包含任何买卖或操作建议**的个股分析报告。

## 核心红线（务必遵守）
- **绝对不输出**“买入/卖出/加仓/减仓/止损/止盈/建议买入区间/目标价”等任何交易指令或倾向性措辞。
- 仅客观陈述技术状态、价位结构、量价特征、板块/汇率背景与潜在风险。

## 输出规范（仅输出 Markdown 正文，不输出 JSON 或代码块）
### 1. 🎯 一句话定调（1-2 句）
概括该标的当前技术状态，末尾用【当前状态: 企稳 / 反弹 / 震荡 / 调整 / 走弱】客观描述。
### 2. 📈 技术面与量价分析
结合 MA5/10/20/60 均线排列、MACD、KDJ、RSI、布林带、ATR 与 5 日量比，引用具体数值客观陈述。
### 3. 💰 关键价位结构
基于提供的关键价位摘要，客观列出上方压力位与下方支撑位及技术来源（成交密集区/枢轴点/前高前低/缺口等），不给出买卖区间。
### 4. 🏭 市场定位、52 周位置与消息面
结合 52 周高低点位置、货币/市场属性、所属行业及提供的最新新闻标题客观归纳驱动与催化(未提供财务报表数据时不得编造具体财务数字)。同时尊重新闻的范围标记,不得把 [market] 标题当作该标的的个股消息。
### 5. ⚖️ 综合研判与风险提示
客观评估当前价位距压力/支撑位的空间不对称性，并列出后续需关注的量价与外部风险信号。
末尾附一行：`> ⚠️ 本内容由 AI 基于公开行情数据生成，仅客观陈述技术状态，不构成任何投资建议或买卖指令。`

{_language_rule(locale)}"""


def _fmt_quote_line(item: dict[str, Any]) -> str:
    price = item.get("price")
    pct = item.get("change_pct")
    ccy = item.get("currency") or ""
    price_s = f"{price:.2f}" if isinstance(price, (int, float)) else "—"
    pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
    name = item.get("name_en") or item.get("symbol")
    return f"- {item.get('symbol')} ({name}): {price_s} {ccy} ({pct_s})"


def _build_market_user_prompt(
    overview: dict[str, Any],
    focus: str,
    locale: SupportedLocale,
) -> str:
    market = overview["market"]
    as_of = overview.get("as_of") or "latest"
    lines = [
        f"Market: {market}",
        f"As of trading date: {as_of}",
        "",
        "## Benchmarks & Macro Indicators",
        *(_fmt_quote_line(b) for b in overview.get("benchmarks") or []),
        "",
        "## Sector ETFs (Sorted by Change %)",
        *(_fmt_quote_line(s) for s in overview.get("sectors") or []),
        "",
        "## Representative Bellwether Stocks",
        *(_fmt_quote_line(item) for item in overview.get("leaders") or []),
        "",
        "## Recent Market News Headlines",
    ]
    news = overview.get("news") or []
    if news:
        for i, n in enumerate(news[:8], 1):
            pub = f" ({n['publisher']})" if n.get("publisher") else ""
            lines.append(f"{i}. {n['title']}{pub}")
    else:
        lines.append("(No external news headlines available in this snapshot.)")

    focus_block = _localized_focus_block(focus, locale)
    if focus_block:
        lines.extend(["", focus_block])
    return "\n".join(lines)


def _build_stock_user_prompt(
    detail: dict[str, Any],
    focus: str,
    locale: SupportedLocale,
) -> str:
    q = detail["quote"]
    rows = detail["rows"][-_KLINE_PROMPT_TAIL:]
    lines = [
        f"Market: {detail['market']}",
        f"Symbol: {detail['symbol']}",
        f"Name: {q.get('long_name') or q.get('name')}",
        f"Exchange: {q.get('exchange')} | Currency: {q.get('currency')} | As of: {q.get('as_of')}",
        f"Sector / Industry: {q.get('sector') or 'N/A'} / {q.get('industry') or 'N/A'}",
        f"Latest Price: {q.get('price')} ({q.get('change_pct')}% vs prev close {q.get('prev_close')})",
        f"52-Week Range: {q.get('low_52w')} - {q.get('high_52w')}",
        f"Key Levels Summary: {detail.get('summary')}",
        "",
        f"## Daily OHLCV + Indicators (Last {len(rows)} trading days, ascending)",
        "```json",
        json.dumps(rows, ensure_ascii=False),
        "```",
        "",
        "## Recent News Headlines",
    ]
    news = detail.get("news") or []
    if news:
        lines.append(_news_scope_legend(locale))
        for i, n in enumerate(news[:6], 1):
            pub = f" ({n['publisher']})" if n.get("publisher") else ""
            scope = n.get("scope") or "ticker"
            lines.append(f"{i}. [{scope}] {n['title']}{pub}")
    else:
        lines.append("(No external news headlines available for this ticker.)")

    focus_block = _localized_focus_block(focus, locale)
    if focus_block:
        lines.extend(["", focus_block])
    return "\n".join(lines)


def _news_scope_legend(locale: SupportedLocale) -> str:
    """Explain the news scope tags so the model never attributes market headlines to a ticker."""
    if locale == "en":
        return (
            "Scope tags: [ticker] = company-specific headlines; "
            "[market] = broader market context that may not relate to this ticker."
        )
    if locale == "ja":
        return (
            "スコープタグ:[ticker]=銘柄固有のニュース、"
            "[market]=市場全体の背景であり、当該銘柄と直接の関連がない場合があります。"
        )
    return (
        "范围标记:[ticker]=该标的相关资讯;"
        "[market]=大盘背景资讯,不一定与该标的相关。"
    )


def _market_summary_text(overview: dict[str, Any]) -> str:
    parts = []
    for b in (overview.get("benchmarks") or [])[:4]:
        pct = b.get("change_pct")
        pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
        parts.append(f"{b['symbol']} {pct_s}")
    return " · ".join(parts)


async def analyze_global_market_stream(
    market_raw: str,
    focus: str = "",
    locale_raw: str | None = None,
    *,
    provider: GlobalMarketProvider | None = None,
) -> AsyncIterator[str]:
    """Stream NDJSON AI market analysis for JP or US market."""
    locale = normalize_locale(locale_raw)
    try:
        market = normalize_market(market_raw)
        overview = await get_market_overview(market, provider=provider)
    except (ValueError, GlobalMarketDataError) as exc:
        yield json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
        return

    yield json.dumps({
        "type": "meta",
        "market": market,
        "kind": "market",
        "as_of": overview.get("as_of"),
        "summary": _market_summary_text(overview),
    }, ensure_ascii=False)

    try:
        from app.services.ai_provider import stream_ai_text

        got_content = False
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _market_system_prompt(market, locale)},
                {"role": "user", "content": _build_market_user_prompt(overview, focus, locale)},
            ],
            temperature=0.5,
            max_tokens=None,
            prefer_final_answer=True,
        ):
            got_content = True
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
    except Exception as exc:
        logger.exception("Global market AI analysis failed (%s): %s", market, exc)
        yield json.dumps({"type": "error", "message": f"AI analysis failed: {exc}"}, ensure_ascii=False)
        return

    if not got_content:
        yield json.dumps({"type": "error", "message": "AI returned empty content, please retry"}, ensure_ascii=False)
        return
    yield json.dumps({"type": "done"}, ensure_ascii=False)


async def analyze_global_stock_stream(
    market_raw: str,
    symbol_raw: str,
    focus: str = "",
    locale_raw: str | None = None,
    *,
    provider: GlobalMarketProvider | None = None,
) -> AsyncIterator[str]:
    """Stream NDJSON AI single-stock analysis for a JP or US equity."""
    locale = normalize_locale(locale_raw)
    try:
        market = normalize_market(market_raw)
        detail = await get_stock_detail(market, symbol_raw, days=250, provider=provider)
    except (ValueError, GlobalMarketDataError) as exc:
        yield json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
        return

    q = detail["quote"]
    yield json.dumps({
        "type": "meta",
        "market": market,
        "kind": "stock",
        "symbol": detail["symbol"],
        "name": q.get("name"),
        "currency": q.get("currency"),
        "close": detail.get("close"),
        "summary": detail.get("summary"),
        "levels": detail.get("levels"),
    }, ensure_ascii=False)

    try:
        from app.services.ai_provider import stream_ai_text

        got_content = False
        async for delta in stream_ai_text(
            [
                {"role": "system", "content": _stock_system_prompt(market, locale)},
                {"role": "user", "content": _build_stock_user_prompt(detail, focus, locale)},
            ],
            temperature=0.5,
            max_tokens=None,
            prefer_final_answer=True,
        ):
            got_content = True
            yield json.dumps({"type": "delta", "content": delta}, ensure_ascii=False)
    except Exception as exc:
        logger.exception("Global stock AI analysis failed (%s %s): %s", market, symbol_raw, exc)
        yield json.dumps({"type": "error", "message": f"AI analysis failed: {exc}"}, ensure_ascii=False)
        return

    if not got_content:
        yield json.dumps({"type": "error", "message": "AI returned empty content, please retry"}, ensure_ascii=False)
        return
    yield json.dumps({"type": "done"}, ensure_ascii=False)

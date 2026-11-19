"""FastAPI routes for Japanese & US stock market AI analysis."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import global_reports
from app.services.global_analyzer import (
    analyze_global_market_stream,
    analyze_global_stock_stream,
)
from app.services.global_market_data import (
    GlobalMarketDataError,
    get_market_overview,
    get_stock_detail,
    search_symbols,
)
from app.services.ndjson_heartbeat import with_heartbeat

router = APIRouter(prefix="/api/global-analysis", tags=["global-analysis"])


class MarketAnalyzeRequest(BaseModel):
    market: str
    focus: str = ""
    locale: str = "zh-CN"


class StockAnalyzeRequest(BaseModel):
    market: str
    symbol: str
    focus: str = ""
    locale: str = "zh-CN"


class SaveGlobalReportRequest(BaseModel):
    market: str
    kind: str = "stock"
    symbol: str = ""
    name: str = ""
    focus: str = ""
    locale: str = "zh-CN"
    content: str
    summary: str = ""
    currency: str = ""
    close: float | None = None
    levels: dict | None = None


@router.get("/overview")
async def get_overview(market: str = Query("JP", description="JP or US")) -> dict:
    try:
        return await get_market_overview(market)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GlobalMarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/search")
async def search_global(
    market: str = Query("JP", description="JP or US"),
    q: str = Query("", description="Ticker or company keyword"),
) -> dict:
    try:
        items = await search_symbols(market, q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"market": market.upper(), "items": items}


@router.get("/stock")
async def get_stock(
    market: str = Query("JP", description="JP or US"),
    symbol: str = Query(..., description="Ticker symbol, e.g. 7203.T or NVDA"),
    days: int = Query(250, ge=30, le=500),
) -> dict:
    try:
        return await get_stock_detail(market, symbol, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GlobalMarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/analyze/market")
async def analyze_market(req: MarketAnalyzeRequest):
    async def stream_gen():
        async for chunk in with_heartbeat(
            analyze_global_market_stream(req.market, req.focus, req.locale),
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/analyze/stock")
async def analyze_stock(req: StockAnalyzeRequest):
    if not req.symbol.strip():
        raise HTTPException(status_code=400, detail="symbol is required")

    async def stream_gen():
        async for chunk in with_heartbeat(
            analyze_global_stock_stream(req.market, req.symbol, req.focus, req.locale),
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/reports")
def list_reports() -> dict:
    return {"reports": global_reports.list_reports()}


@router.post("/reports")
def save_report(req: SaveGlobalReportRequest) -> dict:
    report = global_reports.save_report(req.model_dump())
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(report_id: str) -> dict:
    return {"ok": global_reports.delete_report(report_id)}

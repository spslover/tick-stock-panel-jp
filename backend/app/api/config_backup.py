"""配置备份 API — 导出下载 / 导入预览 / 导入应用。

薄层: 参数接收 → service (config_backup.py) → 按 reload_hints 刷新进程内
引擎与数据源状态。路径白名单、内容校验、原子写、脱敏均在 service 层。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.data_providers.custom import loader as custom_loader
from app.services import config_backup
from app.strategy import monitor_rules

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config-backup", tags=["config-backup"])

# B008: FastAPI 要求参数默认值为 File(...) 调用, 提为模块级单例复用。
_UPLOAD_FILE = File(...)


def _category_param(raw: str) -> frozenset[str] | None:
    """categories 逗号串 → 类别集合。空 = 不传 = 全量; 非法键由 service 丢弃。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


@router.get("/export")
def export_backup(categories: str = "") -> Response:
    """导出用户配置为 ZIP 下载。categories 逗号分隔类别键, 不传导出全部 (不含密钥);
    显式包含 "secrets" 才打包明文密钥。"""
    data, filename = config_backup.build_backup(categories=_category_param(categories))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _read_upload(file: UploadFile) -> bytes:
    max_bytes = config_backup.MAX_ARCHIVE_BYTES
    limit_mb = max_bytes // 1024 // 1024
    try:
        file_size = getattr(file, "size", None)
        if isinstance(file_size, int) and file_size > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"备份文件超过 {limit_mb}MB 上限",
            )
        chunks: list[bytes] = []
        total = 0
        chunk_size = 64 * 1024
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"备份文件超过 {limit_mb}MB 上限",
                )
            chunks.append(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="空文件")
        return b"".join(chunks)
    finally:
        await file.close()


@router.post("/inspect")
async def inspect_backup(file: UploadFile = _UPLOAD_FILE) -> dict:
    """解析备份给出预览 (类别/数量/冲突), 不写任何文件。"""
    data = await _read_upload(file)
    try:
        return await run_in_threadpool(config_backup.inspect_archive, data)
    except config_backup.ConfigBackupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/apply")
async def apply_backup(
    request: Request,
    file: UploadFile = _UPLOAD_FILE,
    allow_overwrite: bool = Form(False),
    categories: str = Form(""),
) -> dict:
    data = await _read_upload(file)
    return await run_in_threadpool(_apply_backup, request, data, allow_overwrite, categories)


def _apply_backup(request: Request, data: bytes, allow_overwrite: bool, categories: str) -> dict:
    try:
        result = config_backup.apply_archive(
            data,
            allow_overwrite=allow_overwrite,
            categories=_category_param(categories),
        )
    except config_backup.ConfigBackupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _reload_states(request, result)
    return result


def _reload_states(request: Request, result: dict) -> None:
    """按导入结果刷新进程内状态、缓存与会话 (与 monitor_rules API 同模式)。"""
    hints = result.get("reload_hints") if "reload_hints" in result else result
    if not isinstance(hints, dict):
        hints = {}
    imported_cats = {
        c.get("key")
        for c in (result.get("categories") or [])
        if isinstance(c, dict) and c.get("imported", 0) > 0
    }
    secrets_merged = bool(result.get("secrets_merged"))
    preferences_merged = bool(result.get("preferences_merged"))

    app_state = getattr(getattr(request, "app", None), "state", None)
    engine = getattr(app_state, "strategy_engine", None)
    monitor_engine = getattr(app_state, "monitor_engine", None)
    repo = getattr(app_state, "repo", None)
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)

    if hints.get("strategies") and engine is not None:
        engine.reload()
    if hints.get("monitor_rules") and monitor_engine is not None and data_dir is not None:
        rules = monitor_rules.load_all(data_dir)
        monitor_engine.set_rules(rules)
    if (hints.get("strategies") or hints.get("monitor_rules")) and (
        monitor_engine is not None and engine is not None and data_dir is not None
    ):
        try:
            from app.strategy import paper_watch

            paper_watch.sync(data_dir, monitor_engine, engine)
        except Exception as e:  # noqa: BLE001
            logger.warning("config backup paper_watch sync failed (ignored): %s", e)

    if hints.get("data_sources") or secrets_merged or "secrets" in imported_cats:
        custom_loader.load_all()

    if hints.get("watchlist") or "watchlist" in imported_cats:
        from app.services import watchlist

        with watchlist._LOCK:
            watchlist._REVISION += 1

    if "auth" in imported_cats:
        from app.services import auth as auth_service

        auth_service.invalidate_all_sessions()

    if secrets_merged or "secrets" in imported_cats:
        from app import secrets_store
        from app.tickflow import client as tf_client

        secrets_store._cache = None
        secrets_store._cache_sig = None
        tf_client.reset_clients()

    if preferences_merged or "preferences" in imported_cats:
        from app.services import preferences

        preferences._invalidate_cache()

    if "history" in imported_cats:
        from app.custom.assistant import sessions as assistant_sessions

        assistant_sessions._cache = None
        assistant_sessions._cache_sig = None

    if "custom_factors" in imported_cats and data_dir is not None:
        try:
            from app.factors.store import load_runtime_registry

            load_runtime_registry(data_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("config backup custom_factors reload failed (ignored): %s", e)

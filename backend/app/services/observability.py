"""可观测性、审计留痕与受管后台任务注册表。

职责:
1. Request-ID 关联: 生成/透传/清洗 X-Request-ID, 通过 contextvars 贯穿请求链路。
2. 轻量 Prometheus 指标注册表: 统计 HTTP 请求计数、延迟直方图、在途请求数与进程 RSS/运行时长,
   输出标准 text/plain; version=0.0.4 文本格式, 不引入额外第三方依赖。
3. 安全审计日志 (audit.jsonl): 线程安全追加写 <data_dir>/logs/audit.jsonl, 强制脱敏
   密码/令牌/密钥字段, 禁止明文凭据落盘。
4. 受管异步后台任务 (spawn_background_task): 维护强引用集合避免 RUF006 悬空任务被 GC 静默取消,
   并记录未捕获异常。
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# ── 1. Request-ID 关联 ──────────────────────────────────────────────────────

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REQUEST_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def generate_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(raw: str | None) -> str:
    """校验并清洗客户端传入的 X-Request-ID, 防止日志注入/CRLF 注入。"""
    if raw:
        candidate = raw.strip()
        if _REQUEST_ID_RE.match(candidate):
            return candidate
    return generate_request_id()


def get_request_id() -> str:
    return _REQUEST_ID_CTX.get()


def set_request_id(request_id: str) -> contextvars.Token[str]:
    return _REQUEST_ID_CTX.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    _REQUEST_ID_CTX.reset(token)


# ── 2. 轻量 Prometheus 指标 ────────────────────────────────────────────────

_LATENCY_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _normalize_path_group(path: str) -> str:
    """将动态路径归并到低基数标签, 防止高基数内存膨胀。"""
    if path in ("/health", "/health/live", "/health/ready", "/metrics", "/api/health"):
        return path
    if path.startswith("/api/"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3:
            return f"/{parts[0]}/{parts[1]}/{parts[2]}"
        if len(parts) == 2:
            return f"/{parts[0]}/{parts[1]}"
        return "/api"
    return "/static"


def _escape_label(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._in_flight = 0
        self._req_counts: dict[tuple[str, str, str], int] = {}
        self._dur_buckets: dict[tuple[str, str, float], int] = {}
        self._dur_sum: dict[tuple[str, str], float] = {}
        self._dur_count: dict[tuple[str, str], int] = {}
        self._audit_events: dict[tuple[str, str], int] = {}

    def on_request_start(self) -> None:
        with self._lock:
            self._in_flight += 1

    def on_request_end(self, method: str, path: str, status_code: int, duration_s: float) -> None:
        group = _normalize_path_group(path)
        m = method.upper()
        status_cls = f"{status_code // 100}xx"
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            ckey = (m, group, status_cls)
            self._req_counts[ckey] = self._req_counts.get(ckey, 0) + 1
            dkey = (m, group)
            self._dur_sum[dkey] = self._dur_sum.get(dkey, 0.0) + max(0.0, duration_s)
            self._dur_count[dkey] = self._dur_count.get(dkey, 0) + 1
            for b in _LATENCY_BUCKETS:
                if duration_s <= b:
                    bkey = (m, group, b)
                    self._dur_buckets[bkey] = self._dur_buckets.get(bkey, 0) + 1

    def on_audit_event(self, action: str, outcome: str) -> None:
        with self._lock:
            key = (action, outcome)
            self._audit_events[key] = self._audit_events.get(key, 0) + 1

    def render_prometheus(self) -> str:
        lines: list[str] = []
        uptime_s = max(0.0, time.monotonic() - self._started_at)
        rss_bytes = 0
        try:
            import psutil

            rss_bytes = int(psutil.Process().memory_info().rss)
        except Exception:  # noqa: BLE001
            rss_bytes = 0

        lines.append("# HELP process_resident_memory_bytes Resident memory size in bytes.")
        lines.append("# TYPE process_resident_memory_bytes gauge")
        lines.append(f"process_resident_memory_bytes {rss_bytes}")

        lines.append("# HELP tsp_process_uptime_seconds Process uptime in seconds.")
        lines.append("# TYPE tsp_process_uptime_seconds gauge")
        lines.append(f"tsp_process_uptime_seconds {uptime_s:.3f}")

        with self._lock:
            in_flight = self._in_flight
            req_counts = dict(self._req_counts)
            dur_buckets = dict(self._dur_buckets)
            dur_sum = dict(self._dur_sum)
            dur_count = dict(self._dur_count)
            audit_events = dict(self._audit_events)

        lines.append("# HELP tsp_http_in_flight_requests Current in-flight HTTP requests.")
        lines.append("# TYPE tsp_http_in_flight_requests gauge")
        lines.append(f"tsp_http_in_flight_requests {in_flight}")

        lines.append("# HELP tsp_http_requests_total Total HTTP requests processed.")
        lines.append("# TYPE tsp_http_requests_total counter")
        for (m, group, status_cls), count in sorted(req_counts.items()):
            lines.append(
                f'tsp_http_requests_total{{method="{_escape_label(m)}",'
                f'path_group="{_escape_label(group)}",status="{_escape_label(status_cls)}"}} {count}'
            )

        lines.append("# HELP tsp_http_request_duration_seconds HTTP request duration histogram.")
        lines.append("# TYPE tsp_http_request_duration_seconds histogram")
        for (m, group), total_cnt in sorted(dur_count.items()):
            lbl = f'method="{_escape_label(m)}",path_group="{_escape_label(group)}"'
            for b in _LATENCY_BUCKETS:
                bcnt = dur_buckets.get((m, group, b), 0)
                lines.append(f'tsp_http_request_duration_seconds_bucket{{{lbl},le="{b}"}} {bcnt}')
            lines.append(f'tsp_http_request_duration_seconds_bucket{{{lbl},le="+Inf"}} {total_cnt}')
            lines.append(f"tsp_http_request_duration_seconds_sum{{{lbl}}} {dur_sum.get((m, group), 0.0):.6f}")
            lines.append(f"tsp_http_request_duration_seconds_count{{{lbl}}} {total_cnt}")

        if audit_events:
            lines.append("# HELP tsp_audit_events_total Total security audit events recorded.")
            lines.append("# TYPE tsp_audit_events_total counter")
            for (action, outcome), cnt in sorted(audit_events.items()):
                lines.append(
                    f'tsp_audit_events_total{{action="{_escape_label(action)}",'
                    f'outcome="{_escape_label(outcome)}"}} {cnt}'
                )

        lines.append("")
        return "\n".join(lines)


metrics_registry = _MetricsRegistry()


# ── 3. 安全审计留痕 (audit.jsonl) ───────────────────────────────────────────

_AUDIT_LOCK = threading.Lock()
_SENSITIVE_KEY_RE = re.compile(r"(password|secret|plaintext|token_hash|cookie|authorization|api_key)", re.I)
_SAFE_META_KEYS = frozenset({"token_id", "token_name", "scopes", "categories", "allow_overwrite"})
_TOKEN_VALUE_RE = re.compile(r"(tsp_[A-Za-z0-9_-]{12,}|Bearer\s+\S+)")


def _redact_value(val: Any) -> Any:
    if isinstance(val, str):
        return _TOKEN_VALUE_RE.sub("[REDACTED]", val)
    if isinstance(val, dict):
        return _redact_dict(val)
    if isinstance(val, list):
        return [_redact_value(item) for item in val]
    return val


def _redact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in _SAFE_META_KEYS:
            out[k] = _redact_value(v)
        elif _SENSITIVE_KEY_RE.search(k):
            out[k] = "[REDACTED]"
        else:
            out[k] = _redact_value(v)
    return out


def audit_log_path(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else Path(settings.data_dir)
    return base / "logs" / "audit.jsonl"


def record_audit_event(
    action: str,
    *,
    actor: str = "session",
    target: str = "",
    outcome: str = "ok",
    client_ip: str = "",
    detail: dict[str, Any] | None = None,
    data_dir: Path | None = None,
) -> None:
    """追加写入一条结构化脱敏安全审计日志, 失败不阻断业务但记录告警。"""
    entry: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "request_id": get_request_id(),
        "action": action,
        "actor": actor,
        "target": _redact_value(target),
        "outcome": outcome,
        "client_ip": client_ip,
    }
    if detail:
        entry["detail"] = _redact_dict(detail)

    metrics_registry.on_audit_event(action, outcome)

    try:
        path = audit_log_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK, open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to append audit event action=%s: %s", action, exc)


# ── 4. 受管异步后台任务 (消除 RUF006 悬空任务) ───────────────────────────────

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _on_background_task_done(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:  # noqa: BLE001
        return
    if exc is not None:
        logger.error("unhandled exception in background task %s: %r", task.get_name(), exc)


def spawn_background_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """创建并追踪异步后台任务, 防止任务对象在完成前被 GC 回收 (RUF006)。"""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_background_task_done)
    return task


def active_background_task_count() -> int:
    return len(_BACKGROUND_TASKS)

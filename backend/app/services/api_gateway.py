"""API 开放网关 — Token 请求的网络门禁、scope 校验与限流 (docs/open-platform-plan.md §4-5)。

Token 通道有两道闸, 认证中间件按序调用:
  1. token_network_verdict(client_ip) — 网络门禁: 默认仅本机/内网可调,
     公网来源直接 403 (不消耗限流额度, 也不向公网探测者泄露 Token 有效性)。
  2. evaluate(data_dir, method, path, plaintext) — 验 Token → scope → 限流,
     得到放行 / 401 / 403 / 429 的裁决与响应头。UI 会话完全不经过本模块。

规则表按顺序匹配 (方法 + 路径前缀); 未命中 = 不对外开放 (403)。
刻意不放进规则表的: SSE 流端点 (EventSource 无法带 Authorization 头,
V2 用短期票据解决)、watchlist/监控规则管理、数据同步、设置 —— 保持最小开放面。
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from app.services import api_tokens

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

_LOCAL_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)

# (method, 路径前缀, scope) — method 用 "*" 表示任意; 顺序敏感, 具体规则在前
_RULES: list[tuple[str, str, str]] = [
    # read:market — 行情读取
    ("GET", "/api/kline/instruments/search", "read:market"),
    ("GET", "/api/kline/daily", "read:market"),
    ("GET", "/api/kline/minute", "read:market"),
    ("GET", "/api/kline/minute-range", "read:market"),
    ("GET", "/api/intraday/status", "read:market"),
    ("GET", "/api/intraday/indices", "read:market"),
    ("GET", "/api/index/daily", "read:market"),
    ("GET", "/api/index/minute", "read:market"),
    ("GET", "/api/overview/market", "read:market"),
    ("GET", "/api/screener/market-snapshot", "read:market"),
    # read:ext — 扩展数据读取 (Key 状态子路径除外, 属管理面)
    ("GET", "/api/ext-data/schema-all", "read:ext"),
    ("GET", "/api/ext-data/schema/", "read:ext"),
    ("GET", "/api/ext-data/", "read:ext"),
    ("GET", "/api/ext-data", "read:ext"),
    # read:analysis — 策略/回测结果/环境/告警 读取
    ("GET", "/api/strategies", "read:analysis"),
    ("GET", "/api/screener/strategies", "read:analysis"),
    ("GET", "/api/screener/cached", "read:analysis"),
    ("GET", "/api/screener/limit-ladder", "read:analysis"),
    ("GET", "/api/backtest/status", "read:analysis"),
    ("GET", "/api/backtest/candidates", "read:analysis"),
    ("GET", "/api/backtest/factor/columns", "read:analysis"),
    ("GET", "/api/regime", "read:analysis"),
    ("GET", "/api/alerts", "read:analysis"),
    # run:backtest — 触发计算任务
    ("POST", "/api/screener/run", "run:backtest"),
    ("POST", "/api/screener/run_preset", "run:backtest"),
    ("POST", "/api/screener/run_all", "run:backtest"),
    ("POST", "/api/backtest/run", "run:backtest"),
    ("POST", "/api/backtest/factor/run", "run:backtest"),
    ("POST", "/api/backtest/factor/batch", "run:backtest"),
    ("POST", "/api/backtest/strategy/run", "run:backtest"),
    # paper:trade — 模拟盘全部 (读+写一体, 单一 scope 简化心智)
    ("*", "/api/paper", "paper:trade"),
    # events — 换取 SSE 短期票据: 任意有效 Token 即可 (票据只继承已有 scope,
    # 不放大权限, 故此处用 "*" 表示"仅需认证", 不做 scope 校验)
    ("POST", "/api/events/ticket", "*"),
    # SSE 流本体: 网关只做标记 (契约可见); 真正凭证是 query 里的票据,
    # 由端点内校验 (EventSource 带不了 Authorization 头)
    ("GET", "/api/events", "*"),
]

# 后缀规则表 — 路径中段含动态段 (如 config_id), 前缀表表达不了"以 X 结尾"语义。
# 限定在 /api/ext-data/ 下, 防止误伤其他路由的巧合后缀。
_SUFFIX_RULES: list[tuple[str, str, str]] = [
    # write:ext — 程序化行数据写入 (结构配置/上传/拉取等管理端点仍不开放)
    ("POST", "/ingest", "write:ext"),
]

_RATE_WINDOW_S = 60.0


def rate_limit_per_min() -> int:
    """每 Token 每分钟上限 (env OPEN_API_RATE_LIMIT_PER_MIN, 默认 120, 范围 1~10000)。"""
    try:
        v = int(os.environ.get("OPEN_API_RATE_LIMIT_PER_MIN", "120"))
        return v if 1 <= v <= 10000 else 120
    except ValueError:
        return 120


# ── 网络门禁: Token 通道默认仅本机/内网 (auth.setup 端点的同源判据) ──

def parse_ip(
    raw: str | None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """解析单个 IP 字符串 (支持 [ipv6]:port, ipv4:port, %zone_id, ::ffff:x.x.x.x)."""
    if not raw or not isinstance(raw, str):
        return None
    token = raw.strip()
    if not token:
        return None
    if token.startswith("["):
        end = token.find("]")
        if end == -1:
            return None
        token = token[1:end]
    elif token.count(":") == 1 and "." in token:
        token = token.split(":", 1)[0]
    if "%" in token:
        token = token.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(token)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


@functools.lru_cache(maxsize=16)
def compile_trusted_cidrs(
    raw: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """解析可信反代 CIDR 列表 (无效条目记录警告并跳过, 不放开)."""
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("忽略无效 TRUSTED_PROXY_CIDRS 项: %r", item)
    return tuple(nets)


def is_trusted_proxy_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | str | None,
    cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] | None = None,
) -> bool:
    """判断给定 IP 是否命中可信反向代理 CIDR 列表。"""
    addr = parse_ip(ip_obj) if isinstance(ip_obj, str) or ip_obj is None else ip_obj
    if addr is None:
        return False
    if cidrs is None:
        from app.config import settings

        cidrs = compile_trusted_cidrs(settings.trusted_proxy_cidrs)
    return any(addr.version == net.version and addr in net for net in cidrs)


def resolve_client_ip(
    direct_ip: str | None,
    xff_header: str | None,
    trusted_cidrs: str | None = None,
) -> str:
    """基于可信反代 CIDR 从右向左剥离 X-Forwarded-For 还原客户端 IP。

    仅当直连 peer 属于 TRUSTED_PROXY_CIDRS 时才采信 X-Forwarded-For；
    从最右侧逐跳剥离可信反代 IP，遇到第一个非可信有效 IP 即返回。
    若最右侧/非可信跳格式非法，fail-closed 返回 "unknown"。
    """
    direct_raw = (direct_ip or "").strip()
    if not direct_raw:
        return "unknown"
    direct_addr = parse_ip(direct_raw)
    if direct_addr is None:
        return direct_raw
    if trusted_cidrs is None:
        from app.config import settings

        trusted_cidrs = settings.trusted_proxy_cidrs
    cidrs = compile_trusted_cidrs(trusted_cidrs)
    if not is_trusted_proxy_ip(direct_addr, cidrs):
        return str(direct_addr)
    if not xff_header or not xff_header.strip():
        return str(direct_addr)

    last_trusted: str | None = None
    for raw_hop in reversed(xff_header.split(",")):
        hop = parse_ip(raw_hop)
        if hop is None:
            return "unknown"
        if is_trusted_proxy_ip(hop, cidrs):
            last_trusted = str(hop)
            continue
        return str(hop)
    return last_trusted or str(direct_addr)


def extract_client_ip(request: Request) -> str:
    """从 FastAPI Request 提取经可信反代校验后的真实客户端 IP。"""
    direct = request.client.host if request.client else ""
    headers = request.headers
    if hasattr(headers, "getlist"):
        xff_list = [v for v in headers.getlist("x-forwarded-for") if v is not None]
        xff = ",".join(xff_list) if xff_list else None
    else:
        xff = headers.get("x-forwarded-for")
    return resolve_client_ip(direct, xff)


def is_local_network(host: str | None) -> bool:
    """是否本机或内网地址: localhost / 127.0.0.0/8 / ::1 / RFC1918 / ULA.

    显式枚举本地网段 (禁用 .is_private, 避免把 CGNAT 100.64.0.0/10
    或云元数据链路本地 169.254.0.0/16 误判为内网)。
    """
    if not host or not isinstance(host, str):
        return False
    stripped = host.strip()
    if not stripped:
        return False
    if stripped.lower() == "localhost":
        return True
    addr = parse_ip(stripped)
    if addr is None:
        return False
    return any(addr.version == net.version and addr in net for net in _LOCAL_NETWORKS)


def token_channel_local_only() -> bool:
    """Token 通道是否仅限本机/内网调用 (env API_TOKEN_LOCAL_ONLY, 默认开)。

    设 0/false/off/no 显式放开 — 供公网 VPS 部署的远程脚本/门户等场景自主选择,
    开放后的数据再分发合规责任由部署者承担(见 docs/configuration.md)。
    """
    return (os.environ.get("API_TOKEN_LOCAL_ONLY", "1").strip().lower()
            not in ("0", "false", "off", "no"))


def token_network_verdict(client_ip: str | None) -> dict | None:
    """Token 通道网络门禁裁决: 公网来源且默认策略未放开 → 403 verdict; 放行 → None。

    返回 None 时调用方继续走 evaluate(); 在 evaluate 之前拦截故不消耗限流额度,
    且公网探测者拿不到 401/403(scope) 差异, 无法探测 Token 有效性。
    """
    if not token_channel_local_only() or is_local_network(client_ip):
        return None
    return {
        "status": 403,
        "detail": (
            "API Token 通道默认仅接受本机/内网请求; 远程调用请经 SSH 隧道"
            "或使用浏览器密码会话。确需公网开放: 设置环境变量"
            " API_TOKEN_LOCAL_ONLY=0 (数据再分发合规责任由部署者承担)"
        ),
        "headers": {},
    }


def required_scope(method: str, path: str) -> str | None:
    """路径+方法 → 所需 scope; None = 该端点不对外开放。

    返回 "*" 表示仅需有效 Token (不做特定 scope 校验)。
    """
    if not isinstance(path, str) or not path or not path.startswith("/"):
        return None
    lower_path = path.lower()
    if (
        "\\" in path
        or "//" in path
        or "\x00" in path
        or "?" in path
        or "#" in path
        or "%2f" in lower_path
        or "%5c" in lower_path
        or "%00" in lower_path
    ):
        return None
    for seg in path.split("/"):
        decoded = seg
        for _ in range(3):
            next_decoded = unquote(decoded)
            if (
                decoded in (".", "..")
                or next_decoded in (".", "..")
                or "/" in next_decoded
                or "\\" in next_decoded
                or "\x00" in next_decoded
            ):
                return None
            if next_decoded == decoded:
                break
            decoded = next_decoded
    norm_method = (method or "").upper()
    norm_path = path.rstrip("/") or "/"
    for m, prefix, scope in _RULES:
        if (m == "*" or m == norm_method) and (
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
        ):
            # read:ext 域内的拉取 Key 状态子路径属管理面 (脱敏也不外露)
            if scope == "read:ext" and norm_path.endswith(("/api-key", "/config/export")):
                return None
            return scope
    if path.startswith("/api/ext-data/"):
        for m, suffix, scope in _SUFFIX_RULES:
            if (m == "*" or m == norm_method) and norm_path.endswith(suffix):
                return scope
    return None


# ── 限流: 进程内有界 LRU+TTL 滑动窗口 (每 token_id 一个 deque) ──
_MAX_BUCKETS: int = 4096
_BUCKETS: OrderedDict[str, deque[float]] = OrderedDict()
_BUCKET_LOCK = threading.RLock()


def clear_rate_limit_buckets() -> None:
    """清空限流桶 (供测试隔离使用)。"""
    with _BUCKET_LOCK:
        _BUCKETS.clear()


def _allow(key: str, limit: int) -> tuple[bool, int, int]:
    """返回 (是否放行, 剩余额度, 重试等待秒)。"""
    now = time.monotonic()
    with _BUCKET_LOCK:
        # 从 LRU 头部渐进清理已过期的桶
        for _ in range(32):
            if not _BUCKETS:
                break
            oldest_key, oldest_q = next(iter(_BUCKETS.items()))
            if oldest_key == key:
                break
            if not oldest_q or now - oldest_q[-1] >= _RATE_WINDOW_S:
                _BUCKETS.popitem(last=False)
            else:
                break

        if key in _BUCKETS:
            q = _BUCKETS[key]
            _BUCKETS.move_to_end(key)
        else:
            if len(_BUCKETS) >= _MAX_BUCKETS:
                stale_keys = [
                    bk for bk, bq in _BUCKETS.items()
                    if not bq or now - bq[-1] >= _RATE_WINDOW_S
                ]
                for bk in stale_keys:
                    _BUCKETS.pop(bk, None)
                while len(_BUCKETS) >= _MAX_BUCKETS:
                    _BUCKETS.popitem(last=False)
            q = deque()
            _BUCKETS[key] = q

        while q and now - q[0] >= _RATE_WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(_RATE_WINDOW_S - (now - q[0])) + 1) if q else int(_RATE_WINDOW_S)
            return False, 0, retry
        q.append(now)
        return True, limit - len(q), 0


def evaluate(data_dir: Path, method: str, path: str, plaintext: str) -> dict:
    """Token 请求裁决: {status: None=放行 | 401/403/429, detail, headers}。"""
    record = api_tokens.verify_token(data_dir, plaintext)
    if record is None:
        return {"status": 401, "detail": "API Token 无效或已吊销", "headers": {}}

    scope = required_scope(method, path)
    if scope is None:
        return {
            "status": 403,
            "detail": f"该端点 ({method} {path}) 未对外开放; 开放清单见 /api/openapi.json?tier=a",
            "headers": {},
        }
    raw_scopes = record.get("scopes")
    scopes = (
        [s for s in raw_scopes if isinstance(s, str)]
        if isinstance(raw_scopes, (list, tuple, set))
        else []
    )
    if scope == "*":
        pass  # 仅需有效 Token (票据签发): 已通过认证, 不校验具体 scope
    elif scope not in scopes:
        return {
            "status": 403,
            "detail": f"Token 缺少所需 scope: {scope} (持有: {scopes})",
            "headers": {},
        }

    bucket_key = record.get("id")
    if not isinstance(bucket_key, str) or not bucket_key:
        return {"status": 401, "detail": "API Token 无效或已吊销", "headers": {}}

    limit = rate_limit_per_min()
    ok, remaining, retry = _allow(bucket_key, limit)
    if not ok:
        return {
            "status": 429,
            "detail": f"请求超出限额 ({limit}/分钟), {retry} 秒后重试",
            "headers": {"Retry-After": str(retry)},
        }
    return {
        "status": None,
        "detail": "",
        "headers": {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
        },
    }

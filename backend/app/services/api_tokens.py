"""API Token 域模块 — 外部调用方的第二认证通道 (与访问密码并行)。

设计 (docs/open-platform-plan.md §4.1):
  - 存储: data/user_data/api_tokens.json (0600), 只存 SHA-256 哈希;
    明文 Token (tsp_ + 32B hex) 仅在创建响应中出现一次。
  - scope: 固定五档, 见 SCOPES; 管理接口无对应 scope, Token 永远进不去。
  - last_used_at 节流落盘 (≥60s 才写一次), 避免热路径每请求写文件。
进程内写互斥沿用域模块惯例 (PAPER_LOCK 模式); 读取无锁 (json 小文件)。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 对外开放的 scope 全集 (admin 刻意不存在 — 管理面永不开放给 Token)
SCOPES = (
    "read:market",
    "read:ext",
    "write:ext",
    "read:analysis",
    "run:backtest",
    "paper:trade",
)

TOKEN_PREFIX = "tsp_"

_LOCK = threading.RLock()
# last_used_at 落盘节流: (token_id, 上次落盘时间); 内存即可, 重启丢失无害
_last_persist: dict[str, datetime] = {}
# mtime 签名缓存: resolved path -> ((st_mtime_ns, st_size, st_ino), tokens)
_TOKEN_CACHE: dict[str, tuple[tuple[int, int, int], list[dict]]] = {}
_MAX_CACHE_DIRS = 16


def _tokens_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "api_tokens.json"


def _cache_key(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))


def _stat_sig(p: Path) -> tuple[int, int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _store_cache_locked(key: str, sig: tuple[int, int, int], tokens: list[dict]) -> None:
    if key not in _TOKEN_CACHE and len(_TOKEN_CACHE) >= _MAX_CACHE_DIRS:
        _TOKEN_CACHE.pop(next(iter(_TOKEN_CACHE)))
    _TOKEN_CACHE[key] = (sig, tokens)


def clear_cache() -> None:
    """清理内存中的 Token 缓存与节流记录 (供测试/维护使用)。"""
    with _LOCK:
        _TOKEN_CACHE.clear()
        _last_persist.clear()


def _load_locked(data_dir: Path) -> list[dict]:
    p = _tokens_path(data_dir)
    key = _cache_key(p)
    sig = _stat_sig(p)
    if sig is None:
        _TOKEN_CACHE.pop(key, None)
        return []
    cached = _TOKEN_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw_tokens = raw.get("tokens", []) if isinstance(raw, dict) else []
        if not isinstance(raw_tokens, list):
            logger.warning("api_tokens.json tokens 字段非列表 (视为无 Token)")
            _TOKEN_CACHE.pop(key, None)
            return []
        tokens = [t for t in raw_tokens if isinstance(t, dict)]
    except (OSError, ValueError) as e:
        logger.warning("api_tokens.json 解析失败 (视为无 Token): %s", e)
        _TOKEN_CACHE.pop(key, None)
        return []
    _store_cache_locked(key, sig, tokens)
    return tokens


def load_tokens(data_dir: Path) -> list[dict]:
    with _LOCK:
        return copy.deepcopy(_load_locked(data_dir))


def _save_locked(data_dir: Path, tokens: list[dict]) -> None:
    p = _tokens_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        p,
        json.dumps({"tokens": tokens}, ensure_ascii=False, indent=2),
        mode=0o600,
    )
    # Windows 权限模型不同, 尽力而为
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)
    key = _cache_key(p)
    sig = _stat_sig(p)
    if sig is not None:
        _store_cache_locked(key, sig, copy.deepcopy(tokens))
    else:
        _TOKEN_CACHE.pop(key, None)


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _sanitize_scopes(raw: object) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [s for s in raw if isinstance(s, str)]
    return []


def list_tokens(data_dir: Path) -> list[dict]:
    """管理列表视图: 去掉哈希, 附加 scope 有效性 (未知 scope 标 invalid)。"""
    with _LOCK:
        out = []
        for t in _load_locked(data_dir):
            token_id = t.get("id")
            if not isinstance(token_id, str) or not token_id:
                continue
            name = t.get("name")
            created_at = t.get("created_at")
            last_used_at = t.get("last_used_at")
            out.append({
                "id": token_id,
                "name": name if isinstance(name, str) else "",
                "scopes": _sanitize_scopes(t.get("scopes")),
                "created_at": created_at if isinstance(created_at, str) else "",
                "last_used_at": last_used_at if isinstance(last_used_at, str) else None,
                "revoked": bool(t.get("revoked")),
            })
        return out


def create_token(data_dir: Path, name: str, scopes: list[str]) -> tuple[dict, str]:
    """创建 Token。返回 (记录视图, 明文) — 明文只此一次, 调用方负责展示。"""
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"未知 scope: {bad} (可用: {list(SCOPES)})")
    plaintext = TOKEN_PREFIX + secrets.token_hex(32)
    record = {
        "id": f"tok_{secrets.token_hex(4)}",
        "name": name.strip() or "未命名",
        "token_hash": _hash(plaintext),
        "scopes": sorted(set(scopes)),
        "created_at": cn_now().isoformat(timespec="seconds"),
        "last_used_at": None,
        "revoked": False,
    }
    with _LOCK:
        tokens = copy.deepcopy(_load_locked(data_dir))
        tokens.append(record)
        _save_locked(data_dir, tokens)
    view = {
        k: (list(v) if isinstance(v, list) else v)
        for k, v in record.items()
        if k != "token_hash"
    }
    return view, plaintext


def revoke_token(data_dir: Path, token_id: str) -> bool:
    if not isinstance(token_id, str) or not token_id:
        return False
    with _LOCK:
        tokens = copy.deepcopy(_load_locked(data_dir))
        hit = False
        for t in tokens:
            if t.get("id") == token_id:
                t["revoked"] = True
                hit = True
        if hit:
            _save_locked(data_dir, tokens)
            _last_persist.pop(token_id, None)
        return hit


def verify_token(data_dir: Path, plaintext: str) -> dict | None:
    """明文 → 有效记录 (revoked/不匹配/哈希对不上 → None)。

    命中时节流更新 last_used_at (内存立即, 落盘 ≥60s 一次)。
    """
    if not isinstance(plaintext, str) or not plaintext.startswith(TOKEN_PREFIX):
        return None
    h = _hash(plaintext)
    with _LOCK:
        tokens = _load_locked(data_dir)
        matched: dict | None = None
        for t in tokens:
            stored = t.get("token_hash")
            hit = isinstance(stored, str) and stored.isascii() and hmac.compare_digest(stored, h)
            if hit and matched is None:
                matched = t
        if matched is None or matched.get("revoked"):
            return None
        token_id = matched.get("id")
        if not isinstance(token_id, str) or not token_id:
            return None
        result = copy.deepcopy(matched)
        result["scopes"] = _sanitize_scopes(result.get("scopes"))
        now = cn_now()
        if _should_persist_locked(token_id, now):
            ts = now.isoformat(timespec="seconds")
            result["last_used_at"] = ts
            _touch_locked(data_dir, token_id, ts)
        return result


def _should_persist_locked(token_id: str, now: datetime) -> bool:
    last = _last_persist.get(token_id)
    if last is None or (now - last).total_seconds() >= 60:
        _last_persist[token_id] = now
        return True
    return False


def _touch_locked(data_dir: Path, token_id: str, ts: str) -> None:
    try:
        tokens = copy.deepcopy(_load_locked(data_dir))
        for t in tokens:
            if t.get("id") == token_id:
                t["last_used_at"] = ts
        _save_locked(data_dir, tokens)
    except Exception as e:  # noqa: BLE001
        logger.warning("api_tokens last_used_at 落盘失败 (忽略): %s", e)

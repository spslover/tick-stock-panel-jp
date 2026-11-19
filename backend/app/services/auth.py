"""访问密码认证 — 单用户, 自托管场景。

设计:
  - 密码用 PBKDF2-HMAC-SHA256 哈希(标准库 hashlib, 无新依赖), 加随机 salt。
    即使 auth.json 泄露, 也无法逆向出明文密码。
  - 会话用随机 token(token_urlsafe), 内存 + 文件双存(支持多进程/重启不丢失)。
  - 存储: data/user_data/auth.json (chmod 0600), 仿 secrets_store 模式。

安全要点:
  - 设密码接口必须限制本机/内网(见 auth router), 防黑客抢占域名抢先设密码。
  - 登录限流: 错5次锁5分钟(见 auth router 内存计数)。
  - 单密码, 不做多用户(避免重构全项目数据层)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as _secrets
import threading
import time
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# PBKDF2 参数(NIST 推荐, 单次校验 ~100ms, 兼顾安全与响应)
_PBKDF2_ITER = 200_000
_SALT_LEN = 16
_TOKEN_BYTES = 32

# 会话有效期: 30 天(自托管单用户, 长一点减少重登频率)
SESSION_TTL = 30 * 24 * 3600

_lock = threading.RLock()
# 内存中的有效会话: { token: expire_ts }。进程重启后从磁盘恢复。
_sessions: dict[str, float] = {}

# 「是否已设密码」缓存: 每个 /api/ 请求都要判定, auth_middleware 原先每次 read_text
# 磁盘 (阻塞事件循环)。此处懒加载缓存, set_password 后失效重算 (仍返回最新真值)。
_configured_cache: bool | None = None

_MAX_FAILS = 5
_LOCK_SECONDS = 300
MAX_LOCKOUT_ENTRIES = 1024
# 持久化失败计数: { ip: (fail_count, lock_until_ts, updated_at_ts) }
_fail_counter: dict[str, tuple[int, float, float]] = {}
_fail_loaded_path: Path | None = None


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _lockouts_path() -> Path:
    from app.config import settings
    return settings.data_dir / "user_data" / "auth_lockouts.json"


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("auth.json malformed: %s", e)
    return {}


def _save(data: dict) -> None:
    p = _path()
    atomic_write_text(
        p, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600,
    )


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。salt 为 None 时生成新 salt。"""
    if salt is None:
        salt = os.urandom(_SALT_LEN)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return salt.hex(), dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """恒定时间比较, 防时序攻击。"""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return _secrets.compare_digest(actual, expected)


# ================================================================
# 密码管理
# ================================================================

def invalidate_runtime_caches() -> None:
    """auth.json 被外部写入后 (配置备份导入) 失效进程内缓存。

    密码判定缓存 + 会话内存态都基于旧文件; 导入覆盖后必须重读,
    否则新设备恢复的密码/会话在本进程内不可见。
    """
    global _configured_cache
    with _lock:
        _configured_cache = None
        _restore_sessions()


def invalidate_all_sessions() -> int:
    """清空全部内存与持久化会话并重置配置缓存 (备份恢复/安全重置时强制重登)。"""
    global _configured_cache
    with _lock:
        count = len(_sessions)
        _sessions.clear()
        _configured_cache = None
        d = _load()
        if d.get("sessions"):
            d["sessions"] = {}
            _save(d)
        return count


def is_configured() -> bool:
    """是否已设置访问密码 (带缓存)。

    热路径 (auth_middleware 每请求调用): 命中缓存则不碰磁盘, 不阻塞事件循环。
    首次或 set_password 失效后, 懒加载重读一次 auth.json, 保证返回最新真值。
    """
    global _configured_cache
    with _lock:
        if _configured_cache is None:
            d = _load()
            _configured_cache = bool(d.get("password_hash"))
        return _configured_cache


def set_password(password: str) -> None:
    """设置/修改访问密码。清空所有现有会话(强制重新登录)。"""
    global _configured_cache
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    salt_hex, hash_hex = _hash_password(password)
    with _lock:
        _sessions.clear()  # 改密码 = 旧会话全部失效
        _save({
            "password_hash": hash_hex,
            "password_salt": salt_hex,
            "updated_at": int(time.time()),
            "sessions": {},  # 清空持久化会话
        })
        _configured_cache = None  # 失效缓存, 下次 is_configured 重读最新真值
    logger.info("access password set")


def bootstrap_from_env() -> bool:
    """首次初始化: 若环境变量 AUTH_PASSWORD 已配置且尚未设过密码, 则用它设密码。

    公网服务器部署场景: 避免每次都要 SSH 端口转发才能设首个密码。
    明文密码只在内存/配置中, 经 set_password() 哈希后写入 auth.json (chmod 0600)。
    一旦设置成功, 后续重启不再覆盖 (用户改密码走 UI, 不受环境变量影响)。

    Returns:
        True 表示本次用环境变量初始化了密码; False 表示无需初始化。
    """
    from app.config import _ENV_FILE, settings

    pwd = (settings.auth_password or "").strip()
    # Compose 会对 env_file 中未加单引号的 $VAR 做插值。Docker 部署时同时
    # 只读挂载原始 .env,首次初始化密码直接按 dotenv 语义读取,避免特殊字符被截断。
    if _ENV_FILE.is_file():
        from dotenv import dotenv_values

        raw_pwd = dotenv_values(_ENV_FILE, encoding="utf-8", interpolate=False).get("AUTH_PASSWORD")
        if isinstance(raw_pwd, str) and raw_pwd.strip():
            pwd = raw_pwd.strip()
    if not pwd:
        return False
    if is_configured():
        # 已设过密码, 不覆盖 (避免环境变量反复重置用户在 UI 改的密码)
        return False
    try:
        set_password(pwd)
        logger.info("access password bootstrapped from AUTH_PASSWORD env (one-time)")
        return True
    except ValueError as e:
        # 密码不合规 (< 6 位), 记日志但不阻断启动
        logger.warning("AUTH_PASSWORD bootstrap skipped: %s", e)
        return False


def verify_and_create_session(password: str) -> str | None:
    """验证密码, 成功则创建会话并返回 token, 失败返回 None。"""
    with _lock:
        d = _load()
        password_hash = d.get("password_hash")
        password_salt = d.get("password_salt", "")
    if not password_hash or not isinstance(password_hash, str):
        return None
    if not _verify_password(password, str(password_salt), password_hash):
        return None
    token = _secrets.token_urlsafe(_TOKEN_BYTES)
    expire = time.time() + SESSION_TTL
    with _lock:
        latest = _load()
        if latest.get("password_hash") != password_hash:
            return None
        _sessions[token] = expire
        _persist_sessions_locked()
    return token


def revoke_session(token: str) -> None:
    """注销会话(登出)。"""
    with _lock:
        _sessions.pop(token, None)
        _persist_sessions_locked()


def is_valid_session(token: str) -> bool:
    """检查会话是否有效(存在且未过期)。过期则清理。"""
    if not token:
        return False
    with _lock:
        expire = _sessions.get(token)
        if expire is None:
            return False
        if time.time() > expire:
            _sessions.pop(token, None)
            _persist_sessions_locked()
            return False
        return True


def _persist_sessions_locked() -> None:
    """把当前内存会话写回 auth.json(需持锁调用)。"""
    d = _load()
    d["sessions"] = {t: exp for t, exp in _sessions.items()}
    _save(d)


def _restore_sessions() -> None:
    """启动时从 auth.json 恢复未过期会话(支持进程重启不丢登录态)。"""
    with _lock:
        _sessions.clear()
        d = _load()
        now = time.time()
        saved = d.get("sessions") or {}
        if isinstance(saved, dict):
            for token, expire in saved.items():
                if (
                    isinstance(token, str)
                    and isinstance(expire, (int, float))
                    and expire > now
                ):
                    _sessions[token] = float(expire)
            if len(_sessions) != len(saved):
                # 有过期会话被清理, 落盘一次
                _persist_sessions_locked()


# ================================================================
# 登录失败限流 (RLock + 有界持久化)
# ================================================================

def _ensure_lockouts_loaded_locked() -> None:
    global _fail_loaded_path
    p = _lockouts_path()
    if _fail_loaded_path == p:
        if not p.exists() and _fail_counter:
            _fail_counter.clear()
        return
    _fail_counter.clear()
    _fail_loaded_path = p
    if not p.exists():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if isinstance(entries, dict):
            now = time.time()
            for ip, rec in entries.items():
                if not isinstance(ip, str) or not isinstance(rec, dict):
                    continue
                try:
                    count = int(rec.get("count", 0))
                    until = float(rec.get("until", 0.0))
                    updated_at = float(rec.get("updated_at", now))
                except (TypeError, ValueError):
                    continue
                if until > now or (until == 0.0 and now - updated_at <= _LOCK_SECONDS):
                    _fail_counter[ip] = (count, until, updated_at)
    except Exception as e:  # noqa: BLE001
        logger.warning("auth_lockouts.json malformed: %s", e)


def _save_lockouts_locked() -> None:
    p = _lockouts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "entries": {
            ip: {"count": c, "until": u, "updated_at": ts}
            for ip, (c, u, ts) in _fail_counter.items()
        }
    }
    atomic_write_text(
        p, json.dumps(payload, indent=2, ensure_ascii=False), mode=0o600,
    )


def _prune_lockouts_locked(now: float, lock_seconds: int = _LOCK_SECONDS) -> bool:
    stale = [
        ip
        for ip, (_, until, updated_at) in _fail_counter.items()
        if (until > 0.0 and until <= now) or (until == 0.0 and now - updated_at > lock_seconds)
    ]
    for ip in stale:
        _fail_counter.pop(ip, None)
    evicted = bool(stale)
    if len(_fail_counter) > MAX_LOCKOUT_ENTRIES:
        oldest = sorted(_fail_counter.items(), key=lambda item: item[1][2])
        for ip, _ in oldest[: len(_fail_counter) - MAX_LOCKOUT_ENTRIES]:
            _fail_counter.pop(ip, None)
        evicted = True
    return evicted


def check_login_rate_limit(ip: str) -> int | None:
    """检查登录失败限流; 若处于锁定期返回剩余等待秒数, 否则返回 None。"""
    with _lock:
        _ensure_lockouts_loaded_locked()
        rec = _fail_counter.get(ip)
        if rec is None:
            return None
        _, until, _ = rec
        now = time.time()
        if until > now:
            return max(1, int(until - now))
        if until and until <= now:
            _fail_counter.pop(ip, None)
            _save_lockouts_locked()
        return None


def record_login_fail(
    ip: str,
    *,
    max_fails: int = _MAX_FAILS,
    lock_seconds: int = _LOCK_SECONDS,
) -> tuple[int, float]:
    """记录一次登录失败, 达阈值则锁定。返回 (count, until)。"""
    with _lock:
        _ensure_lockouts_loaded_locked()
        now = time.time()
        _prune_lockouts_locked(now, lock_seconds=lock_seconds)
        count, until, _ = _fail_counter.get(ip, (0, 0.0, now))
        if until and until <= now:
            count = 0
            until = 0.0
        count += 1
        if count >= max_fails:
            until = now + lock_seconds
            logger.warning("auth login locked for %s after %d fails", ip, count)
        _fail_counter[ip] = (count, until, now)
        _prune_lockouts_locked(now, lock_seconds=lock_seconds)
        _save_lockouts_locked()
        return count, until


def clear_login_fails(ip: str) -> None:
    """登录/改密成功后清除该 IP 的失败计数。"""
    with _lock:
        _ensure_lockouts_loaded_locked()
        if _fail_counter.pop(ip, None) is not None:
            _save_lockouts_locked()


# 模块加载时恢复会话
try:
    _restore_sessions()
except Exception as e:  # noqa: BLE001
    logger.warning("restore sessions failed: %s", e)

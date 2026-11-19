"""访问认证 API。

端点:
  GET  /api/auth/status        — 是否已设密码、当前会话是否有效
  POST /api/auth/setup         — 首次设置密码(仅限本机/内网, 防公网抢占)
  POST /api/auth/login         — 登录(密码 → 会话 token, 含限流)
  POST /api/auth/logout        — 注销当前会话
  POST /api/auth/change-password — 改密码(需已登录)

安全:
  - setup 端点只接受本机/内网请求(request.client.host), 公网请求 403。
    否则黑客可比用户更早扫到域名, 抢先设密码, 反客为主。
  - login 限流: 同一来源 IP 连续失败 5 次, 锁 5 分钟(内存计数)。
  - 会话 token 通过 HttpOnly cookie 下发, 前端无需手动管理。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.services import api_gateway, auth
from app.services.api_gateway import is_local_network as _is_local_network

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tf_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 与 SESSION_TTL 一致

_MAX_FAILS = 5
_LOCK_SECONDS = 300


def _client_ip(request: Request) -> str:
    """取真实客户端 IP (仅对 TRUSTED_PROXY_CIDRS 从右向左剥离 X-Forwarded-For)."""
    return api_gateway.extract_client_ip(request)


def is_trusted_proxy_request(request: Request) -> bool:
    """判断直连 socket peer 是否命中 TRUSTED_PROXY_CIDRS."""
    direct = request.client.host if request.client else ""
    if not direct:
        return False
    ip_obj = api_gateway.parse_ip(direct)
    if ip_obj is None:
        return False
    return api_gateway.is_trusted_proxy_ip(ip_obj)


def _should_set_secure_cookie(request: Request) -> bool:
    """根据 AUTH_COOKIE_SECURE / HTTPS / 可信反代 X-Forwarded-Proto 决定 Secure 属性。"""
    mode = settings.auth_cookie_secure
    if isinstance(mode, bool):
        return mode
    if isinstance(mode, str):
        norm = mode.strip().lower()
        if norm in ("true", "1", "yes", "on"):
            return True
        if norm in ("false", "0", "no", "off"):
            return False
    if (request.url.scheme or "").lower() == "https":
        return True
    if is_trusted_proxy_request(request):
        headers = request.headers
        if hasattr(headers, "getlist"):
            xfp_list = [v for v in headers.getlist("x-forwarded-proto") if v]
            xfp = ",".join(xfp_list).strip()
        else:
            xfp = (headers.get("x-forwarded-proto") or "").strip()
        if xfp:
            rightmost = xfp.split(",")[-1].strip().lower()
            if rightmost == "https":
                return True
    return False


def _check_login_rate_limit(ip: str) -> None:
    """登录失败限流检查, 触发则抛 429。"""
    wait = auth.check_login_rate_limit(ip)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多, 请 {wait} 秒后重试",
        )


def _record_login_fail(ip: str) -> None:
    """记录一次登录失败, 达阈值则锁定。"""
    auth.record_login_fail(ip, max_fails=_MAX_FAILS, lock_seconds=_LOCK_SECONDS)


def _clear_login_fails(ip: str) -> None:
    """登录成功后清除该 IP 的失败计数。"""
    auth.clear_login_fails(ip)


# ================================================================
# 端点
# ================================================================

class PasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


@router.get("/status")
def auth_status(request: Request) -> dict:
    """认证状态: 是否已设密码 + 当前请求是否已登录。"""
    token = request.cookies.get(COOKIE_NAME)
    return {
        "configured": auth.is_configured(),
        "authenticated": bool(token and auth.is_valid_session(token)),
    }


@router.post("/setup")
def setup_password(req: PasswordIn, request: Request) -> dict:
    """首次设置访问密码。仅限本机/内网请求(防公网抢占)。

    若已设置过密码, 返回 409(改密码走 /change-password)。
    """
    # 关键: 限制只有服务器主人(本机/内网)能设密码
    client_ip = _client_ip(request)
    if not _is_local_network(client_ip):
        logger.warning("setup rejected from non-local ip: %s", client_ip)
        raise HTTPException(
            status_code=403,
            detail="首次设置密码仅允许本机或内网访问,请通过 SSH/本地浏览器操作",
        )

    if auth.is_configured():
        raise HTTPException(status_code=409, detail="密码已设置,如需修改请登录后使用改密码功能")

    auth.set_password(req.password)
    logger.info("access password set up from %s", client_ip)
    return {"ok": True, "configured": True}


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    """登录: 密码 → 会话 token(写 HttpOnly cookie)。含失败限流。"""
    ip = _client_ip(request)
    _check_login_rate_limit(ip)

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    token = auth.verify_and_create_session(req.password)
    if not token:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="密码错误")

    _clear_login_fails(ip)
    # HttpOnly: 防 XSS 窃取; SameSite=Lax: 防 CSRF; Path=/: 全站生效
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_should_set_secure_cookie(request),
    )
    return {"ok": True, "authenticated": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """注销当前会话。"""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_should_set_secure_cookie(request),
    )
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordIn, request: Request, response: Response) -> dict:
    """修改密码: 需验证旧密码, 成功后所有会话失效(含当前, 需重新登录)。"""
    token = request.cookies.get(COOKIE_NAME)
    if not (token and auth.is_valid_session(token)):
        raise HTTPException(status_code=401, detail="请先登录")

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="尚未设置访问密码")

    ip = _client_ip(request)
    _check_login_rate_limit(ip)

    # 验证旧密码
    new_token = auth.verify_and_create_session(req.old_password)
    if not new_token:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="旧密码错误")
    # 临时 token 用完即弃
    auth.revoke_session(new_token)
    _clear_login_fails(ip)

    # 改密码(set_password 会清空所有会话)
    auth.set_password(req.new_password)
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_should_set_secure_cookie(request),
    )
    return {"ok": True, "message": "密码已修改, 请重新登录"}

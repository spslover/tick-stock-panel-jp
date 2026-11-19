"""Centralized SSRF-safe outbound HTTP client and URL/secret sanitization helpers."""
from __future__ import annotations

import inspect
import ipaddress
import os
import re
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import SplitResult, parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import anyio
import httpcore
import httpcore._backends.anyio as _httpcore_anyio
import httpcore._backends.sync as _httpcore_sync
import httpx

# Capture original httpx symbols at import time so monkeypatched unit-test
# doubles can be detected and delegated cleanly while keeping URL syntax checks active.
_ORIG_ASYNC_CLIENT = httpx.AsyncClient
_ORIG_SYNC_CLIENT = httpx.Client
_ORIG_HTTPX_POST = httpx.post
_ORIG_HTTPX_GET = httpx.get

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_REDIRECT_HEADERS = frozenset({
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
})

_SECRET_PARAM_NAMES = frozenset({
    "token",
    "key",
    "api_key",
    "apikey",
    "access_token",
    "secret",
    "auth",
    "password",
    "pwd",
    "sign",
    "signature",
})

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_UNSPECIFIED_V4 = ipaddress.ip_network("0.0.0.0/8")
_ULA_V6 = ipaddress.ip_network("fc00::/7")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")
_NAT64_WKP_V6 = ipaddress.ip_network("64:ff9b::/96")

_UNICODE_LABEL_SEPARATORS = ("\u3002", "\uff0e", "\uff61")
_BAD_HOST_CHARS = frozenset("%@:/\\?#[]")

_URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+")
_BARE_QUERY_SECRET_TEMPLATE = r"(?i)([?&](?:{names})=)([^&#\s'\"]*)"


class UnsafeURLError(ValueError):
    """Raised when an outbound URL fails SSRF validation or redirect safety checks."""


SSRFError = UnsafeURLError


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_private_hosts_allowed() -> bool:
    """Whether admin explicitly opted in to allow private/LAN/local outbound targets."""
    return _is_truthy(os.getenv("SAFE_HTTP_ALLOW_PRIVATE_HOSTS"))


def is_ai_local_allowed() -> bool:
    """Whether local/private AI endpoints (e.g. Ollama) are explicitly permitted."""
    return is_private_hosts_allowed() or _is_truthy(os.getenv("AI_ALLOW_LOCAL"))


def _allowlist_entries() -> list[str]:
    raw = os.getenv("SAFE_HTTP_ALLOW_HOSTS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    cleaned = host.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    try:
        return ipaddress.ip_address(cleaned)
    except ValueError:
        pass
    # Catch obfuscated IPv4 notations (decimal integer, hex 0x7f000001, octal, shorthand 127.1).
    try:
        packed = socket.inet_aton(cleaned)
        return ipaddress.IPv4Address(packed)
    except OSError:
        return None


def _is_single_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_reserved
        or addr.is_multicast
    ):
        return True
    if isinstance(addr, ipaddress.IPv4Address):
        return (
            addr in _CGNAT_NET
            or addr in _LOOPBACK_V4
            or addr in _LINK_LOCAL_V4
            or addr in _UNSPECIFIED_V4
        )
    return addr in _ULA_V6 or addr in _LINK_LOCAL_V6


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address | str) -> bool:
    """Return True if *ip* is loopback, private, link-local, CGNAT, ULA, or non-global."""
    if isinstance(ip, str):
        parsed = _parse_ip_literal(ip)
        if parsed is None:
            return True
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = parsed
    else:
        addr = ip

    if _is_single_blocked_ip(addr):
        return True

    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None and _is_blocked_ip(addr.ipv4_mapped):
            return True
        if addr.sixtofour is not None and _is_blocked_ip(addr.sixtofour):
            return True
        if addr.teredo is not None:
            server_ip, client_ip = addr.teredo
            if _is_blocked_ip(server_ip) or _is_blocked_ip(client_ip):
                return True
        if addr in _NAT64_WKP_V6:
            embedded_v4 = ipaddress.IPv4Address(addr.packed[12:16])
            if _is_blocked_ip(embedded_v4):
                return True
    return False


def _normalize_host(host: str) -> str:
    """Normalize Unicode/UTS-46 label separators and IDNA hostnames to canonical ASCII."""
    cleaned = host.strip()
    if not cleaned or "%" in cleaned:
        raise UnsafeURLError(f"非法主机名: {host}")

    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
        if ":" not in cleaned:
            raise UnsafeURLError(f"非法主机名: {host}")

    if ":" in cleaned:
        try:
            addr = ipaddress.ip_address(cleaned)
        except ValueError as exc:
            raise UnsafeURLError(f"非法主机名: {host}") from exc
        if not isinstance(addr, ipaddress.IPv6Address):
            raise UnsafeURLError(f"非法主机名: {host}")
        return str(addr)

    for sep in _UNICODE_LABEL_SEPARATORS:
        cleaned = cleaned.replace(sep, ".")

    try:
        ascii_host = cleaned.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeURLError(f"URL 主机名 IDNA 编码非法: {host}") from exc

    stripped = ascii_host.rstrip(".")
    if (
        not stripped
        or ".." in stripped
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() or ch in _BAD_HOST_CHARS for ch in ascii_host)
    ):
        raise UnsafeURLError(f"非法主机名: {host}")
    return ascii_host


def _is_host_allowlisted(
    host: str,
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None,
) -> bool:
    try:
        host_norm = _normalize_host(host).rstrip(".")
    except UnsafeURLError:
        return False
    host_ip = ip if ip is not None else _parse_ip_literal(host_norm)
    for entry in _allowlist_entries():
        entry_l = entry.lower().rstrip(".")
        if "/" in entry:
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if host_ip is not None and host_ip.version == net.version and host_ip in net:
                return True
            continue
        try:
            entry_ip = ipaddress.ip_address(entry)
        except ValueError:
            try:
                entry_norm = _normalize_host(entry_l).rstrip(".")
            except UnsafeURLError:
                entry_norm = entry_l
            if host_norm == entry_norm:
                return True
        else:
            if host_ip is not None and host_ip == entry_ip:
                return True
    return False


def validate_url(
    url: str,
    *,
    allow_private: bool | None = None,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> SplitResult:
    """Validate URL syntax, scheme, absence of userinfo, and literal IP/localhost rules."""
    if not isinstance(url, str):
        raise UnsafeURLError("URL 必须是字符串")
    raw = url.strip()
    if not raw:
        raise UnsafeURLError("URL 不能为空")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in raw):
        raise UnsafeURLError("URL 包含非法空白或控制字符")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise UnsafeURLError("URL 格式非法") from exc

    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes:
        raise UnsafeURLError(f"不支持的 URL 协议: {scheme or '(空)'}")

    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UnsafeURLError("URL 不能包含用户名或密码")

    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL 端口非法") from exc
    if port is not None and not (1 <= port <= 65535):
        raise UnsafeURLError("URL 端口超出有效范围")

    host = parsed.hostname
    if not host or "%" in host:
        raise UnsafeURLError("URL 缺少合法主机名")

    host_ascii = _normalize_host(host)
    if scheme in ("http", "https"):
        try:
            httpx_host = httpx.URL(raw).host
        except Exception as exc:
            raise UnsafeURLError(f"URL 主机名格式非法: {host}") from exc
        if httpx_host:
            _normalize_host(httpx_host)

    host_norm = host_ascii.rstrip(".")
    allow_priv = is_private_hosts_allowed() if allow_private is None else (
        allow_private or is_private_hosts_allowed()
    )

    if (
        (host_norm == "localhost" or host_norm.endswith(".localhost"))
        and not (allow_priv or _is_host_allowlisted(host_norm))
    ):
        raise UnsafeURLError(f"禁止访问本地主机: {host}")

    literal_ip = _parse_ip_literal(host_norm)
    if (
        literal_ip is not None
        and _is_blocked_ip(literal_ip)
        and not (allow_priv or _is_host_allowlisted(host_norm, literal_ip))
    ):
        raise UnsafeURLError(f"禁止访问内网或保留 IP 地址: {host}")

    return parsed


def is_safe_url(
    url: str,
    *,
    allow_private: bool | None = None,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> bool:
    """Boolean wrapper around :func:`validate_url`."""
    try:
        validate_url(url, allow_private=allow_private, allowed_schemes=allowed_schemes)
    except UnsafeURLError:
        return False
    return True


def resolve_and_pin_host(
    host: str,
    port: int,
    *,
    allow_private: bool | None = None,
) -> list[str]:
    """Resolve *host* via getaddrinfo, fail closed if any IP is blocked, return pinned IPs."""
    host_norm = _normalize_host(host).rstrip(".")

    allow_priv = is_private_hosts_allowed() if allow_private is None else (
        allow_private or is_private_hosts_allowed()
    )

    if (
        (host_norm == "localhost" or host_norm.endswith(".localhost"))
        and not (allow_priv or _is_host_allowlisted(host_norm))
    ):
        raise UnsafeURLError(f"禁止访问本地主机: {host}")

    literal_ip = _parse_ip_literal(host_norm)
    if literal_ip is not None:
        if _is_blocked_ip(literal_ip) and not (allow_priv or _is_host_allowlisted(host_norm, literal_ip)):
            raise UnsafeURLError(f"禁止访问内网或保留 IP 地址: {host}")
        return [str(literal_ip)]

    try:
        infos = socket.getaddrinfo(host_norm, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise httpcore.ConnectError(f"DNS lookup failed for {host}: {exc}") from exc

    if not infos:
        raise UnsafeURLError(f"DNS 未返回可用地址: {host}")

    pinned: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise UnsafeURLError(f"DNS 返回非法 IP 地址: {raw_ip}") from exc
        if _is_blocked_ip(ip_obj) and not (allow_priv or _is_host_allowlisted(host_norm, ip_obj)):
            raise UnsafeURLError(f"域名解析到受限内网或保留 IP ({raw_ip}): {host}")
        ip_str = str(ip_obj)
        if ip_str not in seen:
            seen.add(ip_str)
            pinned.append(ip_str)

    if not pinned:
        raise UnsafeURLError(f"DNS 未返回合法 IP 地址: {host}")
    return pinned


class _SafeSyncBackend(_httpcore_sync.SyncBackend):
    def __init__(self, *, allow_private: bool | None = None) -> None:
        super().__init__()
        self._allow_private = allow_private

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        pinned_ips = resolve_and_pin_host(host, port, allow_private=self._allow_private)
        last_exc: Exception | None = None
        for pinned_ip in pinned_ips:
            try:
                return super().connect_tcp(
                    pinned_ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        raise UnsafeURLError("禁止访问 Unix Domain Socket")


class _SafeAsyncBackend(_httpcore_anyio.AnyIOBackend):
    def __init__(self, *, allow_private: bool | None = None) -> None:
        super().__init__()
        self._allow_private = allow_private

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        pinned_ips = await anyio.to_thread.run_sync(
            lambda: resolve_and_pin_host(host, port, allow_private=self._allow_private)
        )
        last_exc: Exception | None = None
        for pinned_ip in pinned_ips:
            try:
                return await super().connect_tcp(
                    pinned_ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeURLError("禁止访问 Unix Domain Socket")


class SafeHTTPTransport(httpx.HTTPTransport):
    """Sync httpx transport that pins validated DNS IPs before TCP connect."""

    def __init__(self, *, allow_private: bool | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pool._network_backend = _SafeSyncBackend(allow_private=allow_private)


class SafeAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Async httpx transport that pins validated DNS IPs before TCP connect."""

    def __init__(self, *, allow_private: bool | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pool._network_backend = _SafeAsyncBackend(allow_private=allow_private)


def _effective_port(parsed: SplitResult) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme.lower() == "https" else 80


def _canonical_origin_host(host: str | None) -> str:
    raw = (host or "").strip()
    if not raw:
        return ""
    try:
        return _normalize_host(raw).rstrip(".")
    except UnsafeURLError:
        return raw.lower().rstrip(".")


def _same_origin(a: SplitResult, b: SplitResult) -> bool:
    return (
        a.scheme.lower() == b.scheme.lower()
        and _canonical_origin_host(a.hostname) == _canonical_origin_host(b.hostname)
        and _effective_port(a) == _effective_port(b)
    )


def _prepare_redirect_step(
    current_method: str,
    current_parsed: SplitResult,
    next_url: str,
    status_code: int,
    req_kwargs: dict[str, Any],
    *,
    allow_private: bool | None,
) -> tuple[str, SplitResult, dict[str, Any]]:
    next_parsed = validate_url(next_url, allow_private=allow_private)
    next_kwargs = dict(req_kwargs)
    next_method = current_method

    headers = next_kwargs.get("headers")
    next_headers = dict(headers) if headers is not None else None

    if not _same_origin(current_parsed, next_parsed):
        next_kwargs.pop("cookies", None)
        if next_headers is not None:
            next_headers = {
                k: v for k, v in next_headers.items()
                if k.lower() not in _SENSITIVE_REDIRECT_HEADERS
            }

    if status_code in (301, 302, 303) and current_method.upper() not in ("GET", "HEAD"):
        next_method = "GET"
        next_kwargs.pop("content", None)
        next_kwargs.pop("data", None)
        next_kwargs.pop("json", None)
        if next_headers is not None:
            next_headers = {
                k: v for k, v in next_headers.items()
                if k.lower() not in {"content-length", "content-type"}
            }

    if next_headers is not None:
        next_kwargs["headers"] = next_headers

    return next_method, next_parsed, next_kwargs


def _construct_mock_client(cls: type[Any], timeout: float | None) -> Any:
    if timeout is None:
        return cls()
    try:
        sig = inspect.signature(cls)
        params = sig.parameters
        if "timeout" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return cls(timeout=timeout)
    except (TypeError, ValueError):
        pass
    return cls()


def create_safe_async_client(
    *,
    timeout: float | None = None,
    client_cls: type[httpx.AsyncClient] | None = None,
    allow_private: bool | None = None,
) -> httpx.AsyncClient:
    """Create an SSRF-hardened AsyncClient, or delegate cleanly if monkeypatched."""
    cls = client_cls or httpx.AsyncClient
    if cls is not _ORIG_ASYNC_CLIENT:
        return _construct_mock_client(cls, timeout)
    kwargs: dict[str, Any] = {
        "transport": SafeAsyncHTTPTransport(allow_private=allow_private),
        "trust_env": False,
        "follow_redirects": False,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return _ORIG_ASYNC_CLIENT(**kwargs)


def create_safe_sync_client(
    *,
    timeout: float | None = None,
    client_cls: type[httpx.Client] | None = None,
    allow_private: bool | None = None,
) -> httpx.Client:
    """Create an SSRF-hardened sync Client, or delegate cleanly if monkeypatched."""
    cls = client_cls or httpx.Client
    if cls is not _ORIG_SYNC_CLIENT:
        return _construct_mock_client(cls, timeout)
    kwargs: dict[str, Any] = {
        "transport": SafeHTTPTransport(allow_private=allow_private),
        "trust_env": False,
        "follow_redirects": False,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return _ORIG_SYNC_CLIENT(**kwargs)


async def async_request(
    method: str,
    url: str,
    *,
    timeout: float | None = 30.0,
    max_redirects: int = 0,
    client_cls: type[httpx.AsyncClient] | None = None,
    allow_private: bool | None = None,
    **req_kwargs: Any,
) -> httpx.Response:
    """Perform an async HTTP request with SSRF validation, IP pinning, and safe redirects."""
    parsed = validate_url(url, allow_private=allow_private)
    cls = client_cls or httpx.AsyncClient
    if cls is not _ORIG_ASYNC_CLIENT:
        client = _construct_mock_client(cls, timeout)
        async with client:
            if (
                method.upper() == "GET"
                and not req_kwargs
                and not hasattr(client, "request")
                and hasattr(client, "get")
            ):
                return await client.get(url)
            return await client.request(method, url, **req_kwargs)

    transport = SafeAsyncHTTPTransport(allow_private=allow_private)
    current_method = method
    current_url = url
    current_parsed = parsed
    current_kwargs = dict(req_kwargs)
    redirects_done = 0

    async with _ORIG_ASYNC_CLIENT(
        transport=transport,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        while True:
            resp = await client.request(current_method, current_url, **current_kwargs)
            if resp.status_code not in _REDIRECT_STATUSES:
                return resp
            location = resp.headers.get("location")
            if not location or max_redirects <= 0:
                return resp
            if redirects_done >= max_redirects:
                raise UnsafeURLError(f"重定向次数超过上限 ({max_redirects})")
            next_url = urljoin(current_url, location)
            prev_parsed = current_parsed
            current_method, current_parsed, current_kwargs = _prepare_redirect_step(
                current_method,
                current_parsed,
                next_url,
                resp.status_code,
                current_kwargs,
                allow_private=allow_private,
            )
            if not _same_origin(prev_parsed, current_parsed):
                client.cookies.clear()
            current_url = next_url
            redirects_done += 1


def sync_request(
    method: str,
    url: str,
    *,
    timeout: float | None = 30.0,
    max_redirects: int = 0,
    client_cls: type[httpx.Client] | None = None,
    allow_private: bool | None = None,
    **req_kwargs: Any,
) -> httpx.Response:
    """Perform a sync HTTP request with SSRF validation, IP pinning, and safe redirects."""
    parsed = validate_url(url, allow_private=allow_private)
    cls = client_cls or httpx.Client
    if cls is not _ORIG_SYNC_CLIENT:
        client = _construct_mock_client(cls, timeout)
        with client:
            if (
                method.upper() == "GET"
                and not req_kwargs
                and not hasattr(client, "request")
                and hasattr(client, "get")
            ):
                return client.get(url)
            return client.request(method, url, **req_kwargs)

    transport = SafeHTTPTransport(allow_private=allow_private)
    current_method = method
    current_url = url
    current_parsed = parsed
    current_kwargs = dict(req_kwargs)
    redirects_done = 0

    with _ORIG_SYNC_CLIENT(
        transport=transport,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        while True:
            resp = client.request(current_method, current_url, **current_kwargs)
            if resp.status_code not in _REDIRECT_STATUSES:
                return resp
            location = resp.headers.get("location")
            if not location or max_redirects <= 0:
                return resp
            if redirects_done >= max_redirects:
                raise UnsafeURLError(f"重定向次数超过上限 ({max_redirects})")
            next_url = urljoin(current_url, location)
            prev_parsed = current_parsed
            current_method, current_parsed, current_kwargs = _prepare_redirect_step(
                current_method,
                current_parsed,
                next_url,
                resp.status_code,
                current_kwargs,
                allow_private=allow_private,
            )
            if not _same_origin(prev_parsed, current_parsed):
                client.cookies.clear()
            current_url = next_url
            redirects_done += 1


def post_sync(
    url: str,
    *,
    json: Any = None,
    content: bytes | str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    max_redirects: int = 0,
    allow_private: bool | None = None,
) -> httpx.Response:
    """Send an SSRF-safe sync POST request, compatible with ``httpx.post`` mocks."""
    validate_url(url, allow_private=allow_private)
    call_kwargs: dict[str, Any] = {"timeout": timeout}
    if json is not None:
        call_kwargs["json"] = json
    if content is not None:
        call_kwargs["content"] = content
    if headers is not None:
        call_kwargs["headers"] = headers

    if httpx.post is not _ORIG_HTTPX_POST:
        return httpx.post(url, **call_kwargs)

    return sync_request(
        "POST",
        url,
        max_redirects=max_redirects,
        allow_private=allow_private,
        **call_kwargs,
    )


def get_sync(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 8.0,
    max_redirects: int = 3,
    allow_private: bool | None = None,
) -> httpx.Response:
    """Send an SSRF-safe sync GET request, compatible with ``httpx.get`` mocks."""
    validate_url(url, allow_private=allow_private)
    call_kwargs: dict[str, Any] = {}
    if headers is not None:
        call_kwargs["headers"] = headers
    if httpx.get is not _ORIG_HTTPX_GET:
        return httpx.get(url, timeout=timeout, **call_kwargs)
    return sync_request(
        "GET",
        url,
        timeout=timeout,
        max_redirects=max_redirects,
        allow_private=allow_private,
        **call_kwargs,
    )


def redact_url_secrets(
    text: str,
    *,
    extra_params: Iterable[str] = (),
    secret_values: Iterable[str] = (),
) -> str:
    """Redact sensitive query parameters, URL userinfo, and literal secret values from *text*."""
    result = str(text or "")
    if not result:
        return result

    for secret in secret_values:
        val = str(secret or "")
        if not val:
            continue
        result = result.replace(val, "***")
        encoded = quote(val, safe="")
        if encoded and encoded != val:
            result = result.replace(encoded, "***")

    secret_names = set(_SECRET_PARAM_NAMES)
    for param in extra_params:
        name = str(param or "").strip().lower()
        if name:
            secret_names.add(name)

    def _redact_single_url(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        try:
            parts = urlsplit(raw_url)
        except ValueError:
            return raw_url
        netloc = parts.netloc
        if "@" in netloc:
            _, _, hostpart = netloc.rpartition("@")
            netloc = f"***@{hostpart}"
        if parts.query:
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            redacted_pairs = [
                (k, "***" if k.lower() in secret_names else v)
                for k, v in pairs
            ]
            query = urlencode(redacted_pairs)
        else:
            query = parts.query
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))

    result = _URL_RE.sub(_redact_single_url, result)
    escaped_names = "|".join(sorted((re.escape(n) for n in secret_names), key=len, reverse=True))
    if escaped_names:
        bare_re = re.compile(_BARE_QUERY_SECRET_TEMPLATE.format(names=escaped_names))
        result = bare_re.sub(r"\1***", result)
    return result


def sanitize_error(
    err: BaseException | str,
    *,
    extra_params: Iterable[str] = (),
    secret_values: Iterable[str] = (),
    max_len: int | None = None,
) -> str:
    """Format an exception or message with URL credentials and secret tokens redacted."""
    cleaned = redact_url_secrets(
        str(err),
        extra_params=extra_params,
        secret_values=secret_values,
    )
    if max_len is not None and max_len >= 0:
        return cleaned[:max_len]
    return cleaned

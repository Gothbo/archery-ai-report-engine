# -*- coding: utf-8 -*-
"""跨站写请求拦截（PR #4 小加固）：纯 ASGI 中间件（不用 BaseHTTPMiddleware，避免影响 SSE 流与断开检测）。

背景（评估 §7 实测）：引擎没有鉴权，跨站页面用「无请求体的简单 POST」就能触发
/ingest/mock、报告 refresh=1 重算等写操作（浏览器拿不到响应，但服务端已执行）。
规则（只管 POST/PUT/PATCH/DELETE，GET 不受影响；不开 CORS）：
- 带 Origin：必须与请求 Host 同源，或在 config.security.allowed_origins 白名单中；Origin: null 一律拒绝
- 不带 Origin：本机工具（curl / Python / 宿主进程）放行；浏览器若带 Sec-Fetch-Site: cross-site 则拒绝
开关：config.security.block_cross_site_writes（默认开）。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

from app.config import get_config

logger = logging.getLogger("engine.security")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _norm_origin(scheme: str, host: str) -> str | None:
    """scheme + host[:port] → 规范化 origin（去默认端口、小写）。解析失败返回 None。"""
    try:
        parts = urlsplit(f"{scheme}://{host}")
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not hostname:
        return None
    if ":" in hostname:  # IPv6 字面量
        hostname = f"[{hostname}]"
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{hostname}".lower()
    return f"{scheme}://{hostname}:{port}".lower()


def _origin_of_header(origin: str) -> str | None:
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return _norm_origin(parts.scheme, parts.netloc)


def is_blocked(method: str, scheme: str, headers: dict[str, str], allowed: list[str]) -> str | None:
    """返回拒绝原因（None=放行）。headers 键为小写。"""
    if method.upper() not in UNSAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin is not None:
        if origin.strip().lower() == "null":
            return "Origin: null（沙箱 iframe / 本地文件页面）不允许写操作"
        req_origin = _origin_of_header(origin)
        if req_origin is None:
            return "Origin 头无法解析"
        own = _norm_origin(scheme, headers.get("host", ""))
        if own is not None and req_origin == own:
            return None
        if req_origin in {_origin_of_header(o) for o in allowed}:
            return None
        return f"跨站写请求（Origin={req_origin}）"
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        return "跨站写请求（Sec-Fetch-Site: cross-site）"
    return None


class CrossSiteWriteGuard:
    """ASGI 中间件：命中规则 → 403 {"code":"CROSS-SITE-BLOCKED"}，不进入路由。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method", "GET").upper() not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        sec = get_config().security
        if not sec.block_cross_site_writes:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        reason = is_blocked(scope["method"], scope.get("scheme", "http"), headers, sec.allowed_origins)
        if reason is None:
            await self.app(scope, receive, send)
            return
        logger.warning("拦截跨站写请求 %s %s：%s", scope["method"], scope.get("path"), reason)
        body = json.dumps({"code": "CROSS-SITE-BLOCKED", "detail": f"已拒绝：{reason}"},
                          ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

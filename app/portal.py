# -*- coding: utf-8 -*-
"""同源托管服务中心门户（PR #4）：/portal/ → config.portal.dist_dir（门户 npm run build 产物）。

- 门户产物（base './' + hash 路由）不提交进引擎仓库，只配目录
- 每次请求按当前配置解析目录（配置在启动时读取；测试夹具换配置也生效）
- 未配置 / 目录里没有 index.html → 404 PORTAL-NOT-CONFIGURED
- 不开 CORS：页面与 /api/v1 同源
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, get_config

_APPS: dict[str, StaticFiles] = {}


def portal_dist_dir() -> Path | None:
    raw = get_config().portal.dist_dir
    if not raw or not str(raw).strip():
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    return p if (p / "index.html").is_file() else None


class PortalApp:
    """挂在 /portal 下的 ASGI 应用：委托给按目录缓存的 StaticFiles(html=True)。"""

    async def __call__(self, scope, receive, send):
        dist = portal_dist_dir()
        if dist is None:
            resp = JSONResponse(status_code=404, content={
                "code": "PORTAL-NOT-CONFIGURED",
                "detail": "未配置门户产物目录（config.portal.dist_dir），或目录中没有 index.html"})
            await resp(scope, receive, send)
            return
        key = str(dist.resolve())
        app = _APPS.get(key)
        if app is None:
            app = _APPS[key] = StaticFiles(directory=key, html=True)
        await app(scope, receive, send)

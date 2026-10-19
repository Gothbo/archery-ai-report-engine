# -*- coding: utf-8 -*-
"""射箭 AI 训练报告引擎 — FastAPI 薄壳。

核心引擎（ingest/store/metrics/reports/memory）零 API 依赖；本模块只做
路由挂载 + 参数校验 + 异常转 HTTP（D3）。
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import swagger_ui
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.ask import router as ask_router
from app.api.athletes import router as athletes_router
from app.api.guidance import router as guidance_router
from app.api.ingest import router as ingest_router
from app.api.llm import router as llm_router
from app.api.reports import router as reports_router
from app.api.sessions import notes_router, sessions_router
from app.config import get_config
from app.logging_setup import setup_logging
from app.portal import PortalApp
from app.security import CrossSiteWriteGuard
from app.store.database import get_database

logger = logging.getLogger("engine")


def _engine_version() -> str | None:
    """引擎版本（PR #4，/health 的 engine_version）：优先读 pyproject.toml 的 [project] version，
    取不到（如打包后没有 pyproject）再用已安装包的元数据。版本号规则待 PM 定。"""
    import re
    from pathlib import Path

    try:
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        section = re.search(r"(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)", text)
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', section.group(1)) if section else None
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version("suooter-archery-engine")
    except Exception:  # noqa: BLE001
        return None


ENGINE_VERSION = _engine_version()


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = get_config()  # 校验失败 → 启动失败（口径 SSOT）
    setup_logging(cfg.log_dir)
    db = get_database(cfg)
    logger.info("引擎启动 config_summary=%s db=%s", cfg.summary(), db.db_path)
    yield


app = FastAPI(
    title="射箭AI训练报告引擎",
    version=ENGINE_VERSION or "0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# PR #4：跨站写请求拦截（纯 ASGI，见 app/security.py；不开 CORS）
app.add_middleware(CrossSiteWriteGuard)

app.include_router(ingest_router)
app.include_router(reports_router)
app.include_router(athletes_router)
app.include_router(sessions_router)
app.include_router(notes_router)
app.include_router(ask_router)
app.include_router(guidance_router)
app.include_router(llm_router)

_SWAGGER_STATIC = os.path.join(os.path.dirname(swagger_ui.__file__), "static")
app.mount("/swagger-static", StaticFiles(directory=_SWAGGER_STATIC), name="swagger-static")

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/portal", include_in_schema=False)
def portal_root() -> RedirectResponse:
    # 门户用相对路径（base './'），必须带结尾斜杠才能正确解析 ./assets/…
    return RedirectResponse(url="/portal/", status_code=307)


# PR #4：同源托管服务中心门户（config.portal.dist_dir；未配置 → 404 PORTAL-NOT-CONFIGURED）
app.mount("/portal", PortalApp(), name="portal")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/docs", include_in_schema=False)
def swagger_docs():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title="射箭AI训练报告引擎 - 接口文档",
        swagger_js_url="/swagger-static/swagger-ui-bundle.js",
        swagger_css_url="/swagger-static/swagger-ui.css",
    )


@app.get("/api/v1/health")
def health() -> dict:
    cfg = get_config()
    db = get_database()
    return {
        "status": "ok",
        "config_version": cfg.config_version,
        "timezone": cfg.timezone,
        "db": db.db_path,
        "mdc_source": "empty" if cfg.mdc_source is None else cfg.mdc_source,
        "llm_enabled": cfg.llm.enabled,  # B1：对话增强开关状态（默认关闭，P1 报告链路零影响）
        "engine_version": ENGINE_VERSION,  # PR #4：取自 pyproject.toml；版本号规则待 PM 定
    }

@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM ===== 射箭 AI 训练报告引擎 · 一键启动 =====

REM 1) 虚拟环境不存在则创建（Windows 优先 py launcher，其次 python）
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 .venv ...
    where py >nul 2>nul
    if not errorlevel 1 ( py -m venv .venv ) else ( python -m venv .venv )
    if errorlevel 1 ( echo 创建失败：请安装 Python 3.10+ 后重试 & exit /b 1 )
)

set "PY=.venv\Scripts\python.exe"

REM 2) 依赖未安装则按 pyproject.toml 安装
"%PY%" -c "import uvicorn, fastapi, pydantic, pydantic_settings, tzdata" >nul 2>nul
if errorlevel 1 (
    echo [2/3] 安装依赖 ...
    "%PY%" -m pip install -q -e ".[dev]"
    if errorlevel 1 ( echo 依赖安装失败 & exit /b 1 )
)

REM 3) 启动服务（Ctrl+C 停止）
echo [3/3] 启动 http://127.0.0.1:8000 ...
"%PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
endlocal

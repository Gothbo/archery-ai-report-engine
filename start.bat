@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM =====================================================================
REM  Archery AI Training Report Engine - one-click launcher
REM =====================================================================
REM  Keep this file ASCII-only. cmd.exe parses .bat with the OEM code page
REM  (936 on zh-CN), so UTF-8 Chinese written here shows up as garbage, and a
REM  mid-file "chcp 65001" shifts the parser's byte offsets for multi-byte
REM  text. ASCII bytes are identical under 936 and UTF-8, so it cannot break.
REM  Chinese log lines printed by the Python service still display fine.
REM =====================================================================

REM ---- 1) run profile: edit this one line to switch configs -----------
REM  config.llamacpp.local.json : local model on :8090 + demo MDC judgement
REM  config.demo.json           : demo MDC judgement, chat off
REM  config.json                : FORMAL SSOT. mdc_source=null, so reports
REM                               describe only (no judgement) and chat is
REM                               off until the M4.5 expert thresholds land.
set "ENGINE_CONFIG=config.llamacpp.local.json"
set "ENGINE_PORT=8000"
REM  Bind address. Default 127.0.0.1 = local machine only (P0: the API has
REM  no auth and serves athlete data). Only change this behind an auth proxy.
set "ENGINE_HOST=127.0.0.1"

REM ---- 2) local model server, optional --------------------------------
REM  Started only when the model file exists and the port is still free.
REM  Leave LLM_PORT free / keep an external server (llama.cpp Vulkan build,
REM  Ollama) on that port and this step simply reuses it.
set "LLM_PORT=8090"
set "LLM_MODEL=models\qwen2.5-1.5b-instruct-q4_k_m.gguf"
set "LLM_ALIAS=qwen2.5-1.5b-instruct-q4_k_m"

REM ---- 3) virtualenv --------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtualenv .venv ...
    where py >nul 2>nul
    if not errorlevel 1 ( py -m venv .venv ) else ( python -m venv .venv )
    if errorlevel 1 ( echo Failed to create venv - install Python 3.10+ and retry & exit /b 1 )
) else (
    echo [1/4] Virtualenv ready.
)
set "PY=%~dp0.venv\Scripts\python.exe"

REM ---- 4) dependencies ------------------------------------------------
"%PY%" -c "import uvicorn, fastapi, pydantic, pydantic_settings, tzdata" >nul 2>nul
if errorlevel 1 (
    echo [2/4] Installing dependencies ...
    "%PY%" -m pip install -q -e ".[dev]"
    if errorlevel 1 ( echo Dependency install failed & exit /b 1 )
) else (
    echo [2/4] Dependencies ready.
)

REM ---- 5) local model server ------------------------------------------
if not exist "%LLM_MODEL%" (
    echo [3/4] Model file missing - skipping local model server.
    echo        Expected: %LLM_MODEL%
) else (
    powershell -NoProfile -Command "exit (Get-NetTCPConnection -State Listen -LocalPort %LLM_PORT% -ErrorAction SilentlyContinue | Measure-Object).Count"
    if errorlevel 1 (
        echo [3/4] Port %LLM_PORT% already serving - reusing it.
    ) else (
        echo [3/4] Starting local model server on :%LLM_PORT% - first load takes about 30s ...
        start "llm-server :%LLM_PORT%" "%PY%" -m llama_cpp.server --model "%LLM_MODEL%" --model_alias "%LLM_ALIAS%" --host 127.0.0.1 --port %LLM_PORT% --n_ctx 2048
    )
)

REM ---- 6) engine ------------------------------------------------------
echo [4/4] Engine profile: %ENGINE_CONFIG%
echo        Open http://127.0.0.1:%ENGINE_PORT%/  - Ctrl+C to stop
"%PY%" -m uvicorn app.main:app --host %ENGINE_HOST% --port %ENGINE_PORT%
endlocal
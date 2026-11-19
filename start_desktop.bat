@echo off
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo 未找到 .venv，请先运行 start.bat 或创建虚拟环境。
  pause
  exit /b 1
)

.venv\Scripts\python.exe desktop\host.py %*
if errorlevel 1 pause

@echo off
chcp 65001 >nul 2>&1
title 工具箱

cd /d "%~dp0"

echo.
echo   ============================================
echo     工具箱
echo   ============================================
echo.

REM ---- 1) Resolve interpreter: project .venv first, then local .venv, then PATH ----
set "PYEXE="
if exist "%~dp0..\..\..\.venv\Scripts\python.exe" (
    set "PYEXE=%~dp0..\..\..\.venv\Scripts\python.exe"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYEXE=%~dp0.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

"%PYEXE%" --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python not found.
    echo.
    echo   Option A: use the portable build - double-click "工具箱.exe" ^(no Python needed^)
    echo   Option B: install Python 3.12+ ^(check "Add to PATH"^), then run this script again
    echo.
    pause
    exit /b 1
)

REM ---- 2) Dependency check: auto one-click install when anything is missing ----
echo   Checking dependencies...
"%PYEXE%" -c "import flask,requests,yaml,cryptography,qrcode,faker,pandas,openpyxl,colorama" >nul 2>&1
if %errorlevel% neq 0 goto :deps_install
"%PYEXE%" -c "import importlib.util as u,sys;sys.exit(0 if all(u.find_spec(m) for m in ['mitmproxy','mss','cv2','numpy','PyQt5','uiautomation']) else 1)" >nul 2>&1
if %errorlevel% neq 0 goto :deps_install
goto :deps_ok

:deps_install
echo.
echo   [Deps] Missing packages detected - auto install now...
echo   [Deps] First run may take 3-10 minutes. Portable build needs no install: 工具箱.exe
echo.
"%PYEXE%" -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --disable-pip-version-check
if errorlevel 1 (
    echo   [Deps] Mirror failed, retrying with default index...
    "%PYEXE%" -m pip install -r "%~dp0requirements.txt" --disable-pip-version-check
)
"%PYEXE%" -c "import flask,requests,yaml,cryptography,qrcode,faker,pandas,openpyxl,colorama" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Dependency install incomplete. Check network and run this script again.
    echo   [ERROR] Or use the portable build "工具箱.exe" which needs no install.
    echo.
    pause
    exit /b 1
)

:deps_ok
echo   Python: %PYEXE%
echo   Starting...

REM Kill stale processes on port 5050
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5050 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=1" %%b in ("%%a") do set IP=%%b
)
echo   URL: http://%IP%:5050
echo.
echo   Close this window to stop
echo   ============================================
echo.

"%PYEXE%" app.py

echo.
echo   ============================================
echo   Server stopped
echo   ============================================
echo.
pause

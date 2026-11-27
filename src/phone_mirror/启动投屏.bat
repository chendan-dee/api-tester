@echo off
chcp 65001 >nul
title 手机控制（投屏 + 点击）
cd /d "%~dp0"

REM adb/hdc/scrcpy 与 npm 依赖已随仓库，只要求本机有 Node 运行时
where node >nul 2>&1
if errorlevel 1 (
  echo [ERROR] 未找到 Node.js，请先安装 Node 18+（https://nodejs.org）
  pause
  exit /b 1
)

start "" http://127.0.0.1:9300/
node server.js
pause

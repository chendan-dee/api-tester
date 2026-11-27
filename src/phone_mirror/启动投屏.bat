@echo off
chcp 65001 >nul
title 手机控制（投屏 + 点击）
cd /d "%~dp0"
start "" http://127.0.0.1:9300/
node server.js
pause

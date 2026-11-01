@echo off
chcp 65001 >nul
setlocal
rem condense 一键入口：把长文档 / 大日志拖到本文件上即可。
rem 摘要会写到 <原文件>.digest.md，并复制到剪贴板。
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0oneclick-condense.ps1" %*
echo.
pause

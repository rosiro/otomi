@echo off
cd /d "%~dp0"
uv run otomi serve
if errorlevel 1 pause

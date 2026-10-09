@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "ROOT=%~dp0"
set "PYTHON=%ROOT%lab_paper_intake\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%ROOT%condmat-trend-radar\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" "%ROOT%paper_workspace.py" %*
exit /b %errorlevel%
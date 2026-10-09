@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
  echo.
  echo Installation failed. Keep this window open and send the error text to the maintainer.
  pause
  exit /b 1
)
echo.
echo CondMat Radar installation completed.
pause
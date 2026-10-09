@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Could not find .venv Python at %PYTHON%.
  exit /b 1
)

"%PYTHON%" -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  "%PYTHON%" -m pip install pyinstaller
  if errorlevel 1 exit /b 1
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

"%PYTHON%" -m PyInstaller --clean --noconfirm LabPaperIntake.spec
if errorlevel 1 exit /b 1

if not exist "dist\LabPaperIntake\config" mkdir "dist\LabPaperIntake\config"
copy /y ".env.example" "dist\LabPaperIntake\config\.env.example" >nul

echo.
echo Build complete: dist\LabPaperIntake\LabPaperIntake.exe
echo Do not publicly distribute a config\.env that contains a DeepSeek API key.
echo To configure a trusted computer, copy config\.env.example to config\.env and fill in the key locally.
endlocal

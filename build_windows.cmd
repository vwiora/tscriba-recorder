@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%build_windows.ps1"

if not exist "%PS_SCRIPT%" (
  echo ERROR: build_windows.ps1 not found next to this .cmd file.
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo Build failed with exit code %RC%.
  exit /b %RC%
)

echo.
echo Build finished successfully.
exit /b 0

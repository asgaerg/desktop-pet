@echo off
REM Diagnostic for the desktop pet launcher. Double-click this file.
REM Keep this file ASCII-only (cmd.exe reads .cmd using the OEM codepage).
setlocal
chcp 65001 >nul

set "ROOT=%~dp0"
set "LOG=%APPDATA%\@pet\desktop\logs\pet.log"

echo ================================================================
echo  Desktop Pet - launcher diagnostic
echo ================================================================
echo.

echo [0] Paths
echo     project root : %ROOT%
echo     log file     : %LOG%
echo.

echo [1] Is the log reachable, and how big is it?
if exist "%LOG%" (
  for %%A in ("%LOG%") do echo     size = %%~zA bytes   modified = %%~tA
) else (
  echo     LOG NOT FOUND
)
echo.

echo [2] Electron processes right now
tasklist /FI "IMAGENAME eq electron.exe" 2>nul | find /I "electron.exe"
if errorlevel 1 echo     none running
echo.

echo [3] Node and pnpm
where node 2>nul
node --version 2>nul
echo     ^(the launcher uses PET_NODE_EXE from dev.cmd, not necessarily this one^)
echo.

echo [4] Launching the pet now, with output shown...
echo ----------------------------------------------------------------
echo.
"%ROOT%dev.cmd"
set "RC=%ERRORLEVEL%"
echo.
echo ----------------------------------------------------------------
echo [5] dev.cmd returned exit code %RC%
echo.
echo [6] Electron processes AFTER the launch attempt
tasklist /FI "IMAGENAME eq electron.exe" 2>nul | find /I "electron.exe"
if errorlevel 1 echo     none running
echo.

echo [7] Last 12 lines of the log
if exist "%LOG%" (
  powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Encoding UTF8 -Tail 12"
) else (
  echo     LOG NOT FOUND
)
echo.

echo [8] Single-instance lock / Electron user-data state
echo     Electron keeps a cross-process lock here. A leftover lock from a killed
echo     run makes the NEXT launch quit immediately with exit code 0 and no output.
if exist "%APPDATA%\@pet\desktop" (
  echo     --- files matching Singleton/lock ---
  dir /b /a "%APPDATA%\@pet\desktop\*Singleton*" 2>nul
  dir /b /a "%APPDATA%\@pet\desktop\*lock*" 2>nul
  echo     --- created just now? ---
  powershell -NoProfile -Command "Get-ChildItem -LiteralPath \"$env:APPDATA\@pet\desktop\" -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'Singleton|lock|Lock' } | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize"
) else (
  echo     userData directory does not exist yet
)
echo.

echo [9] startup.log written by the main process on THIS launch attempt
set "SLOG=%ROOT%apps\desktop\dist\startup.log"
if exist "%SLOG%" (
  powershell -NoProfile -Command "Get-Content -LiteralPath '%SLOG%' -Encoding UTF8"
) else (
  echo     not written - Electron exited before loading the main process code
)
echo.

echo [10] Startup marker (existence means the window never appeared)
set "MARK=%APPDATA%\@pet\desktop\.launch-failed"
if exist "%MARK%" (echo     PRESENT - last launch never showed a window) else (echo     absent)
echo.

echo ================================================================
echo  Please send the whole window content to the assistant.
echo ================================================================
pause

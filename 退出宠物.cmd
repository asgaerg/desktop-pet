@echo off
REM Close the desktop pet (all running instances).
REM
REM Why this exists: the pet window is created with skipTaskbar:true, so it has NO
REM taskbar button -- the tray menu is the only in-app way to quit. If the tray
REM icon is hidden (it lives behind the "^" overflow area by default) the user is
REM stuck with a window they cannot close. This script gives a way out that does
REM not depend on finding a tray icon.
REM
REM Keep this file ASCII-only: cmd.exe reads .cmd using the OEM codepage.

echo ================================================================
echo  Closing the desktop pet
echo ================================================================
echo.

echo [1] Electron processes
tasklist /FI "IMAGENAME eq electron.exe" 2>nul | find /I "electron.exe"
if errorlevel 1 (
  echo     none running
) else (
  echo     terminating...
  taskkill /F /IM electron.exe >nul 2>&1
)
echo.

echo [2] Python sidecar (the brain process)
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH 2^>nul') do (
  echo     killing python PID %%~P
  taskkill /F /PID %%~P >nul 2>&1
)
echo.

echo [3] Result
tasklist /FI "IMAGENAME eq electron.exe" 2>nul | find /I "electron.exe" >nul
if errorlevel 1 (echo     electron: stopped) else (echo     electron: STILL RUNNING)
echo.

echo Done. You can start it again with the desktop shortcut.
echo.
pause

@echo off
REM ---------------------------------------------------------------
REM Launch the desktop pet WITHOUT pnpm and WITHOUT changing PATH.
REM
REM Why this wrapper exists: the `node` on PATH may be old (e.g. v14
REM from nvm), and `nvm use` rewrites a symlink in the drive root,
REM which needs administrator rights. But each nvm version directory
REM is directly executable, so we just point at one of them.
REM
REM --no-sandbox is passed because some Windows environments refuse to
REM let Chromium create child processes. The symptom is a loop of
REM "GPU process launch failed" followed by
REM "FATAL: GPU process isn't usable. Goodbye." and the app exits
REM before any window appears. Disabling the GPU (three different
REM switches) does NOT help, because the problem is child-process
REM creation, not the GPU. If it launches fine for you and you would
REM rather keep the renderer sandbox, delete --no-sandbox below and
REM run scripts\launch.mjs --dev --build directly instead.
REM
REM NOTE: keep this file ASCII-only. cmd.exe reads .cmd files using the
REM system OEM codepage (GBK on Chinese Windows), so non-ASCII comments
REM get parsed as commands and break the script.
REM
REM To switch Node versions, edit the line below.
REM ---------------------------------------------------------------

set "PET_NODE_EXE=D:\nvm\v18.12.0\node.exe"

if not exist "%PET_NODE_EXE%" (
  echo.
  echo Cannot find Node: %PET_NODE_EXE%
  echo Edit dev.cmd and point PET_NODE_EXE at an installed node.exe.
  echo List installed versions with:  nvm list
  echo.
  pause
  exit /b 1
)

"%PET_NODE_EXE%" "%~dp0scripts\launch.mjs" --dev --build --no-sandbox %*

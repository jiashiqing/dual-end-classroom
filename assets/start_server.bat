@echo off
setlocal
cd /d "%~dp0"
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE goto NOPY
title Class Sync Server
%PYEXE% "sync_server.py" %*
echo.
echo [stopped] server exited.
pause
exit /b 0

:NOPY
echo.
echo [ERROR] Python 3 not found on this machine.
echo   1. Install Python 3.8+ from https://www.python.org/downloads/
echo   2. Tick "Add python.exe to PATH" during setup
echo   3. Run this file again
echo.
pause
exit /b 1

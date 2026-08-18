@echo off
setlocal
cd /d "%~dp0"
title QAMcast - install

echo.
echo  ============================================================
echo    QAMcast  --  install
echo  ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  Python was not found on PATH.
    echo.
    echo  Install it from https://www.python.org/downloads/ and tick
    echo  "Add python.exe to PATH" in the installer, then run this again.
    echo.
    pause
    exit /b 1
)

rem  This file is also published on its own as a release asset, so it may be
rem  sitting in an empty folder with nothing to install. Fetch the project
rem  first in that case, rather than failing with a missing-file error.
if exist "tools\install.py" goto run

echo  This looks like a fresh folder - the project is not here yet.
echo.
where git >nul 2>&1
if errorlevel 1 (
    echo  git was not found either, so it cannot be fetched automatically.
    echo.
    echo  Download the source from
    echo    https://github.com/CasualArclamp/qamcast
    echo  unzip it, and run install.bat from inside.
    echo.
    pause
    exit /b 1
)
set "GET="
set /p "GET=Download QAMcast into this folder? Y/n: "
if /i "%GET%"=="n" (
    echo  Nothing to do.
    pause
    exit /b 1
)
echo.
git clone https://github.com/CasualArclamp/qamcast.git qamcast
if errorlevel 1 (
    echo.
    echo  The download failed.
    pause
    exit /b 1
)
cd qamcast
echo.
echo  Fetched into %CD%
echo.

:run
python tools\install.py
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

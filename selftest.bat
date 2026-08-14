@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title QAMcast - self test

echo.
echo  ============================================================
echo    QAMcast  --  self test
echo  ============================================================
echo.
echo  Runs the whole chain against a file instead of a sound card:
echo  generates a test tone, encodes it, modulates it to tx.wav,
echo  demodulates that back and checks what came out.
echo.
echo  No audio hardware is touched. Run this first - it separates
echo  "is the install working" from "is my audio wiring right",
echo  which a first hardware test otherwise confuses.
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  Python was not found on PATH.
    pause
    exit /b 1
)

echo  Profile to test:
echo.
echo    1   WIDE48        48 kHz card, up to  96 kbps   - start here
echo    2   WIDE          96 kHz card, up to 195 kbps
echo    3   WIDE44      44.1 kHz card, up to  88 kbps
echo    4   RADIO         48 kHz card, up to  57 kbps
echo    5   ACOUSTIC      48 kHz card, up to  47 kbps
echo    6   all of them
echo.
set "SEL="
set /p "SEL=Choice [1]: "
if "%SEL%"=="" set "SEL=1"

set "LIST="
if "%SEL%"=="1" call :pick WIDE48 64k
if "%SEL%"=="2" call :pick WIDE 128k
if "%SEL%"=="3" call :pick WIDE44 64k
if "%SEL%"=="4" call :pick RADIO 32k
if "%SEL%"=="5" call :pick ACOUSTIC 32k
if "%SEL%"=="6" call :pick "WIDE48 WIDE WIDE44 RADIO ACOUSTIC" 32k
if not defined LIST (
    echo  Not a choice.
    pause
    exit /b 1
)

set "CODEC=opus"
set /p "CODEC=Codec, opus or aac [opus]: "
if "%CODEC%"=="" set "CODEC=opus"

set "FAILED="
for %%P in (%LIST%) do (
    echo.
    echo  ------------------------------------------------------------
    echo   %%P
    echo  ------------------------------------------------------------
    python tools\selftest.py %%P %BR% %CODEC%
    if errorlevel 1 set "FAILED=!FAILED! %%P"
)

echo.
if defined FAILED (
    echo  FAILED:!FAILED!
) else (
    echo  All good. Next: devices.bat to find your sound card, then
    echo  tx.bat and rx.bat in two windows.
)
echo.
pause
exit /b 0

:pick
set "LIST=%~1"
set "BR=%~2"
goto :eof

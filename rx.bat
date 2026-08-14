@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title QAMcast - RECEIVE

echo.
echo  ============================================================
echo    QAMcast  --  RECEIVE
echo  ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  Python was not found on PATH.
    pause
    exit /b 1
)

:: ---------------------------------------------------------------- how
echo  How do you want to set this up?
echo.
echo    1   Web UI - pick everything in the browser, with scopes   - easiest
echo    2   Here in this window
echo.
set "MODE="
set /p "MODE=Choice [1]: "
if "%MODE%"=="" set "MODE=1"
if not "%MODE%"=="2" goto webui

:: ---------------------------------------------------------------- profile
echo.
echo  Profile - must match the transmitter, and it is the only
echo  setting that does. Bitrate, modulation, coding and codec all
echo  travel in the frame header, so this configures itself.
echo.
echo    1   WIDE48       48 kHz card    - start here
echo    2   WIDE         96 kHz card
echo    3   WIDE44     44.1 kHz card
echo    4   RADIO        48 kHz card    through a receiver
echo    5   RADIO44    44.1 kHz card    through a receiver
echo    6   ACOUSTIC     48 kHz card    microphone
echo    7   ACOUSTIC44 44.1 kHz card    microphone
echo.
set "SEL="
set /p "SEL=Profile [1]: "
if "%SEL%"=="" set "SEL=1"
if "%SEL%"=="1" set "PROFILE=WIDE48"
if "%SEL%"=="2" set "PROFILE=WIDE"
if "%SEL%"=="3" set "PROFILE=WIDE44"
if "%SEL%"=="4" set "PROFILE=RADIO"
if "%SEL%"=="5" set "PROFILE=RADIO44"
if "%SEL%"=="6" set "PROFILE=ACOUSTIC"
if "%SEL%"=="7" set "PROFILE=ACOUSTIC44"
if not defined PROFILE (
    echo  Not a choice.
    pause
    exit /b 1
)

:: ---------------------------------------------------------------- devices
echo.
echo  Reading sound devices ...
python tools\devices.py

echo  Enter the index of the INPUT device to listen on, or type
echo   wav  to decode a tx.wav written earlier.
echo.
echo  For a VB-CABLE loopback, pick CABLE Output here.
echo.
set "DEVICE="
set /p "DEVICE=Input device: "
if "%DEVICE%"=="" set "DEVICE=wav"

echo.
echo  Enter the index of the OUTPUT device to play the recovered
echo  audio on, or  none  for silent. Do NOT pick CABLE Input here
echo  or you will feed the decoded audio straight back into the
echo  modem's own input.
echo.
set "OUTDEV="
set /p "OUTDEV=Playback device [none]: "
if "%OUTDEV%"=="" set "OUTDEV=none"

:: ---------------------------------------------------------------- record
echo.
set "REC="
set /p "REC=Also record recovered audio to a wav? filename or blank: "

:: ---------------------------------------------------------------- go
echo.
echo  ============================================================
echo    profile   %PROFILE%
echo    listening %DEVICE%
echo    playback  %OUTDEV%
if defined REC echo    recording %REC%
echo  ============================================================
echo.
echo  UI at http://127.0.0.1:8732     Ctrl+C to stop.
echo.
echo  Audio will not start immediately. The interleaver holds about
echo  6 seconds, and nothing downstream of it is valid until it has
echo  filled - that wait is the price of the burst protection.
echo.

if defined REC (
    python rx.py --profile "%PROFILE%" --device "%DEVICE%" --output "%OUTDEV%" --record "%REC%"
) else (
    python rx.py --profile "%PROFILE%" --device "%DEVICE%" --output "%OUTDEV%"
)

echo.
pause
exit /b 0

:webui
echo.
echo  Starting the receiver with its web UI.
echo  Your browser should open at http://127.0.0.1:8732
echo.
echo  Pick the profile and devices there, then press Listen. The page
echo  shows lock, constellation, waterfall, PAD and auto-probe live.
echo  Ctrl+C here to stop.
echo.
python rx.py --open
echo.
pause
exit /b 0

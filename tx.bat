@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title QAMcast - TRANSMIT

echo.
echo  ============================================================
echo    QAMcast  --  TRANSMIT
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
echo  Profile - this must match the receiver. Pick the one for your
echo  card's sample rate; nothing else will lock.
echo.
echo    1   WIDE48       48 kHz card    up to  96 kbps   - start here
echo    2   WIDE         96 kHz card    up to 195 kbps   - only way to 192k
echo    3   WIDE44     44.1 kHz card    up to  88 kbps
echo    4   RADIO        48 kHz card    up to  57 kbps   through a transmitter
echo    5   RADIO44    44.1 kHz card    up to  53 kbps   through a transmitter
echo    6   ACOUSTIC     48 kHz card    up to  47 kbps   speaker to microphone
echo    7   ACOUSTIC44 44.1 kHz card    up to  43 kbps   speaker to microphone
echo.
set "SEL="
set "PROFILE="
set /p "SEL=Profile [1]: "
if "%SEL%"=="" set "SEL=1"
rem  Each choice calls a subroutine rather than chaining sets on one line.
rem  Chaining is wrong twice over: only the first set would be conditional, so
rem  every profile would inherit the last line's bitrate limits, and the
rem  chaining character is not protected inside a rem either.
if "%SEL%"=="1" call :setprof WIDE48 96 64k
if "%SEL%"=="2" call :setprof WIDE 195 128k
if "%SEL%"=="3" call :setprof WIDE44 88 64k
if "%SEL%"=="4" call :setprof RADIO 57 32k
if "%SEL%"=="5" call :setprof RADIO44 53 32k
if "%SEL%"=="6" call :setprof ACOUSTIC 47 24k
if "%SEL%"=="7" call :setprof ACOUSTIC44 43 24k
if not defined PROFILE (
    echo  Not a choice.
    pause
    exit /b 1
)

:: ---------------------------------------------------------------- source
echo.
echo  Source - a stream URL, or a local file. Anything ffmpeg opens.
echo  Examples:  https://stream.example/live.mp3
echo             C:\music\set.flac
echo.
echo  Or leave it blank to pick from the saved stations.
echo.
set "SOURCE="
set /p "SOURCE=Source: "
if "%SOURCE%"=="" call :pickstation
if "%SOURCE%"=="" (
    echo  Nothing to transmit.
    pause
    exit /b 1
)

:: ------------------------------------------------------------ passthrough
echo.
echo  Passthrough sends the stream's own packets without re-encoding, so
echo  the station's bits reach the receiver untouched. It works only if
echo  the source is already AAC or Opus, and the stream then sets the
echo  bitrate rather than the answer below.
echo.
set "PASS="
set /p "PASS=Pass through without re-encoding? y/N: "
set "PTFLAG="
if /i "!PASS:~0,1!"=="y" set "PTFLAG=--passthrough"
if defined PTFLAG (
    echo.
    echo  Checking what the source actually carries ...
    python tx.py --probe "%SOURCE%"
    echo.
)

:: ---------------------------------------------------------------- codec
set "CODEC=opus"
set "BITRATE=%DEFBR%"
if not defined PTFLAG call :askcodec

:: ---------------------------------------------------------------- device
echo.
echo  Reading sound devices ...
python tools\devices.py out

echo  Enter the index of the OUTPUT device, or type  wav  to write
echo  tx.wav instead of using a sound card.
echo.
echo  For a VB-CABLE loopback, pick CABLE Input here and set rx.bat
echo  to CABLE Output.
echo.
set "DEVICE="
set /p "DEVICE=Output device: "
if "%DEVICE%"=="" set "DEVICE=wav"

:: ---------------------------------------------------------------- metadata
echo.
set "STATION=QAM RADIO"
set /p "STATION=Station name [QAM RADIO]: "
if "%STATION%"=="" set "STATION=QAM RADIO"

:: ---------------------------------------------------------------- go
echo.
echo  ============================================================
echo    profile   %PROFILE%
echo    source    %SOURCE%
if defined PTFLAG (
    echo    audio     passed through, the stream sets the rate
) else (
    echo    audio     %CODEC% at %BITRATE%
)
echo    output    %DEVICE%
echo    station   %STATION%
echo  ============================================================
echo.
echo  UI at http://127.0.0.1:8731     Ctrl+C to stop.
echo.

python tx.py --profile "%PROFILE%" --source "%SOURCE%" --codec "%CODEC%" --bitrate "%BITRATE%" --device "%DEVICE%" --station "%STATION%" %PTFLAG%

echo.
pause
exit /b 0

:webui
echo.
echo  Starting the transmitter with its web UI.
echo  Your browser should open at http://127.0.0.1:8731
echo.
echo  Everything is set there: source, profile, bitrate, output device,
echo  station text, and the channel simulator. Ctrl+C here to stop.
echo.
python tx.py --open
echo.
pause
exit /b 0

:setprof
set "PROFILE=%~1"
set "MAXBR=%~2"
set "DEFBR=%~3"
goto :eof

:askcodec
rem  Only asked when re-encoding. In passthrough the source decides both, and
rem  offering a bitrate that will be discarded is just a way to mislead.
echo.
echo  Codec:
echo.
echo    opus   one codec across 16-192k, with loss concealment  - the default
echo    aac    HE-AAC, stepping v2 to v1 to LC as the rate rises
echo    xhe    xHE-AAC, the strongest thing where bits are scarce
echo.
echo  Opus and HE-AAC need nothing but ffmpeg. xHE-AAC needs the exhale
echo  encoder built once, and will offer to do it if you pick it.
echo.
set "CODEC=opus"
set /p "CODEC=Codec [opus]: "
if "%CODEC%"=="" set "CODEC=opus"
if /i "%CODEC%"=="xhe" call :needexhale
echo.
echo  %PROFILE% tops out at %MAXBR% kbps. Ask for more and it will refuse.
set "BITRATE="
set /p "BITRATE=Bitrate [%DEFBR%]: "
if "%BITRATE%"=="" set "BITRATE=%DEFBR%"
goto :eof

:needexhale
rem  Reached only by asking for xHE-AAC. Nothing in the Opus or HE-AAC paths
rem  comes through here, and neither does the receiver -- ffmpeg decodes
rem  xHE-AAC perfectly well, it just cannot encode it. So a normal install
rem  never needs a compiler.
echo.
python tools\build_exhale.py --check
if not errorlevel 1 goto :eof
echo.
echo  xHE-AAC needs the exhale encoder, which is not built yet. Nothing in
echo  ffmpeg can encode xHE-AAC, so there is no way around it -- but it is a
echo  one-time build, from source, and needs cmake and a C++ compiler.
echo.
set "BUILD="
set /p "BUILD=Build it now? Y/n: "
if /i "!BUILD:~0,1!"=="n" goto :noexhale
echo.
python tools\build_exhale.py
if errorlevel 1 goto :noexhale
goto :eof

:noexhale
echo.
echo  Carrying on with Opus, which spans the whole range and needs nothing.
set "CODEC=opus"
goto :eof

:pickstation
rem  The saved playlists, listed with each station's rates. Paste the URL of
rem  the feed you want -- printing them is enough, and it avoids building a
rem  numbered menu out of a list that changes whenever a .pls is added.
echo.
python tx.py --list-stations
if errorlevel 1 goto :eof
echo.
set /p "SOURCE=Paste a feed URL from above: "
goto :eof

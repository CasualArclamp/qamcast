@echo off
setlocal
cd /d "%~dp0"
title QAMcast - sound devices

echo.
echo  ============================================================
echo    QAMcast  --  sound devices
echo  ============================================================

where python >nul 2>&1
if errorlevel 1 goto nopython

python tools\devices.py %*
if errorlevel 1 goto fail

echo  Note the index in the first column - that is what tx.bat and
echo  rx.bat ask for.
echo.
echo  Only MME devices are shown, which is one entry per card. MME
echo  accepts any rate, but does it by resampling, which costs a
echo  little signal quality. For the cleanest path set the card to
echo  the rate you want in Windows sound settings, then run
echo  devices.bat --all and pick its WASAPI entry.
echo.
goto done

:nopython
echo.
echo  Python was not found on PATH.
echo.
goto done

:fail
echo.
echo  Could not list devices. Is sounddevice installed?
echo      pip install sounddevice
echo.

:done
pause

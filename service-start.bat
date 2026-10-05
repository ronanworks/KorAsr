@echo off
rem service-start.bat - start KorASR in the background (no window) and wait until it is ready.
rem Settings: scripts\service-env.cmd. Pass --no-pause (or --quiet) to skip the final pause.
setlocal
call "%~dp0scripts\service-env.cmd"
"%KORASR_PY%" "%~dp0scripts\service_ctl.py" start
if /I not "%~1"=="--no-pause" if /I not "%~1"=="--quiet" pause
endlocal

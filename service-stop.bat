@echo off
rem service-stop.bat - stop KorASR. Only a KorASR process on the port is stopped;
rem while a class is recording it asks first. Pass --no-pause (or --quiet) to skip the pause.
setlocal
call "%~dp0scripts\service-env.cmd"
"%KORASR_PY%" "%~dp0scripts\service_ctl.py" stop
if /I not "%~1"=="--no-pause" if /I not "%~1"=="--quiet" pause
endlocal

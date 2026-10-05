@echo off
rem service-restart.bat - stop KorASR, then start it again (e.g. after an update).
setlocal
call "%~dp0scripts\service-env.cmd"
"%KORASR_PY%" "%~dp0scripts\service_ctl.py" restart
if /I not "%~1"=="--no-pause" if /I not "%~1"=="--quiet" pause
endlocal

@echo off
rem service-status.bat - is KorASR running and healthy, are classes recording, GPU, Kimi balance, URLs.
setlocal
call "%~dp0scripts\service-env.cmd"
"%KORASR_PY%" "%~dp0scripts\service_ctl.py" status
if /I not "%~1"=="--no-pause" if /I not "%~1"=="--quiet" pause
endlocal

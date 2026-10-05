@echo off
rem service-logs.bat - show the last 40 log lines and follow new ones live (Ctrl+C to quit).
setlocal
call "%~dp0scripts\service-env.cmd"
"%KORASR_PY%" "%~dp0scripts\service_ctl.py" logs --follow -n 40
endlocal

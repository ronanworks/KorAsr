@echo off
rem Start KorASR detached without waiting (the settings are in scripts\service-env.cmd).
rem For everyday use prefer service-start.bat in the project folder: it checks the port,
rem waits until the service is ready and prints the URLs. Log: logs\service.log
cd /d "%~dp0.."
call "%~dp0service-env.cmd"
start "" "%KORASR_PYW%" start.py --host %KORASR_HOST% --port %KORASR_PORT%
echo KorASR starting in background; see logs\service.log

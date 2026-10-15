@echo off
rem Settings shared by service-*.bat, scripts\start_local_test.cmd and startAll.ps1.
rem When KorASR moves to another machine, edit this file only.

rem Python of the korasr conda environment (pythonw.exe runs the service without a window).
if not defined KORASR_PY set "KORASR_PY=C:\Users\4090\.conda\envs\korasr\python.exe"
if not defined KORASR_PYW set "KORASR_PYW=C:\Users\4090\.conda\envs\korasr\pythonw.exe"

rem Tailscale Serve/Funnel forward https://4090ai.tail1447d0.ts.net:8444 (tailnet) and
rem :8443 (public) to this address, so the service only listens on this machine.
if not defined KORASR_HOST set "KORASR_HOST=127.0.0.1"
if not defined KORASR_PORT set "KORASR_PORT=8000"

rem The classes since 2026-09-27 live in this database and recording folder
rem (.env names the same ones since 2026-10-05; set here too so these scripts never differ).
set "DATABASE_URL=sqlite:///./data/korasr-local-test.db"
set "RAW_AUDIO_DIR=data/raw-local-test"

set "HF_HUB_OFFLINE=1"
set "ASR_MODEL=large-v3"
set "ASR_DEVICE=cuda"
set "ASR_COMPUTE_TYPE=float16"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

rem CTranslate2 finds cuBLAS in the aicut environment's torch build; installing
rem nvidia-cublas-cu12 into the korasr environment would remove this dependency.
rem Node.js is needed when start.py rebuilds the frontend.
set "PATH=C:\Users\4090\.conda\envs\aicut\Lib\site-packages\torch\lib;C:\Program Files\nodejs;%PATH%"

rem Another machine keeps its own paths and port in scripts\service-env.local.cmd
rem (not in git), which runs last and may override anything above.
if exist "%~dp0service-env.local.cmd" call "%~dp0service-env.local.cmd"

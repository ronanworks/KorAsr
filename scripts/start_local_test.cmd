@echo off
rem Start KorASR detached with the local-test settings used since 2026-09-27
rem (test database, test recordings, large-v3 on CUDA), behind Tailscale Serve:
rem   https://4090ai.tail1447d0.ts.net:8444  ->  https+insecure://localhost:8000
rem Stop the old instance first (service-stop.bat). Log: logs\service.log
cd /d "%~dp0.."
set "DATABASE_URL=sqlite:///./data/korasr-local-test.db"
set "RAW_AUDIO_DIR=data/raw-local-test"
set "HF_HUB_OFFLINE=1"
set "ASR_MODEL=large-v3"
set "ASR_DEVICE=cuda"
set "ASR_COMPUTE_TYPE=float16"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
rem CTranslate2 currently finds cuBLAS in the aicut env's torch build; installing
rem nvidia-cublas-cu12 into the korasr env would remove this dependency.
set "PATH=C:\Users\4090\.conda\envs\aicut\Lib\site-packages\torch\lib;C:\Program Files\nodejs;%PATH%"
start "" "C:\Users\4090\.conda\envs\korasr\pythonw.exe" start.py --host 127.0.0.1 --port 8000
echo KorASR starting in background; see logs\service.log

"""Start, stop and inspect the KorASR service on Windows (used by the service-*.bat files).

The .bat files first run scripts\\service-env.cmd, so this process already carries the
service environment (database, model settings, PATH) and passes it on to the service.

    python scripts\\service_ctl.py status            # running? healthy? classes recording? URLs
    python scripts\\service_ctl.py start
    python scripts\\service_ctl.py stop [--yes]      # asks first while a class is recording
    python scripts\\service_ctl.py restart [--yes]
    python scripts\\service_ctl.py logs [--follow] [-n 40]

Only a process of the korasr environment running start.py is ever stopped; another
program on the port is reported and left alone. The database is opened read-only.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "service.log"
PORT = int(os.environ.get("KORASR_PORT", "8000"))
HOST = os.environ.get("KORASR_HOST", "127.0.0.1")
PYW = os.environ.get("KORASR_PYW") or str(Path(sys.executable).with_name("pythonw.exe"))
TAILSCALE = r"C:\Program Files\Tailscale\tailscale.exe"
START_MARK = "===== service start @ "
READY = "[Startup] Ready"
# Error-looking log lines of the current run (translation retries etc. are normal; this is a hint).
_ERROR_RE = re.compile(r"Traceback|\bERROR\b|Exception|failed after|could not|Error:", re.IGNORECASE)
_SERVE_URL_RE = re.compile(r"^(https?://\S+)\s+\((Funnel on|tailnet only)\)")
_SERVE_PROXY_RE = re.compile(r"proxy\s+(\S+)")


def _powershell(command: str) -> str:
    prefix = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return result.stdout.strip()


def listener() -> dict | None:
    """The process listening on PORT: pid, address, path, cmd, started, mem (bytes)."""
    # The parent matters on a Windows venv: venv\Scripts\pythonw.exe is only a launcher
    # that starts the base interpreter, which is the process holding the port (A8000).
    out = _powershell(
        f"$c = Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($c) { $p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\"; "
        "$q = if ($p) { Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\" } else { $null }; "
        "[pscustomobject]@{ pid = $c.OwningProcess; address = $c.LocalAddress; path = $p.ExecutablePath; "
        "cmd = $p.CommandLine; started = $(if ($p) { $p.CreationDate.ToString('o') } else { $null }); "
        "mem = $p.WorkingSetSize; parent_path = $(if ($q) { $q.ExecutablePath } else { $null }) } | ConvertTo-Json -Compress }"
    )
    return json.loads(out) if out else None


def _service_python_dirs() -> set[str]:
    """Folders of the configured service Python (KORASR_PY / KORASR_PYW), lower-case."""
    return {
        str(Path(value).parent).lower()
        for value in (os.environ.get("KORASR_PY"), os.environ.get("KORASR_PYW"))
        if value
    }


def is_korasr(info: dict | None) -> bool:
    """A process of this project's service: the configured (or korasr-env) Python running start.py."""
    if not info:
        return False
    path, cmd = (info.get("path") or "").lower(), (info.get("cmd") or "").lower()
    parent = (info.get("parent_path") or "").lower()
    dirs = _service_python_dirs()
    ours = (
        str(Path(path).parent).lower() in dirs
        or (parent and str(Path(parent).parent).lower() in dirs)   # venv launcher → base interpreter
        or "\\envs\\korasr\\" in path
    )
    return bool(path) and bool(ours) and "start.py" in cmd


def database_path() -> Path | None:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///"):
        return None
    path = Path(url[len("sqlite:///"):])
    return path if path.is_absolute() else ROOT / path


def class_counts() -> dict:
    path = database_path()
    if path is None or not path.exists():
        return {"error": f"数据库不存在：{path}"}
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = dict(con.execute("select recording_state, count(*) from session group by recording_state"))
    finally:
        con.close()
    return {"path": path, "recording": rows.get("recording", 0), "paused": rows.get("paused", 0),
            "total": sum(rows.values())}


def last_run(log_text: str) -> dict:
    """Facts about the latest service start in the log: when, ready or not, error-looking lines."""
    index = log_text.rfind(START_MARK)
    if index < 0:
        return {"started": None, "ready": False, "errors": []}
    run = log_text[index:]
    started = run[len(START_MARK):].split("=", 1)[0].strip()
    lines = run.splitlines()[1:]
    errors = [line for line in lines if _ERROR_RE.search(line)]
    return {"started": started, "ready": READY in run, "errors": errors}


def read_log_tail(max_bytes: int = 600_000) -> str:
    if not LOG.exists():
        return ""
    with open(LOG, "rb") as fh:
        fh.seek(max(0, LOG.stat().st_size - max_bytes))
        return fh.read().decode("utf-8", errors="replace")


def tailscale_urls(serve_status: str) -> list[tuple[str, bool]]:
    """(url, public) for every Tailscale Serve/Funnel entry that forwards to this service."""
    found, url, public = [], None, False
    for line in serve_status.splitlines():
        line = line.strip()
        match = _SERVE_URL_RE.match(line)
        if match:
            url, public = match.group(1), match.group(2) == "Funnel on"
            continue
        proxy = _SERVE_PROXY_RE.search(line)
        if url and proxy and re.search(rf"(localhost|127\.0\.0\.1):{PORT}\b", proxy.group(1)):
            found.append((url.rstrip("/"), public))
            url = None
    return found


def _serve_status() -> str:
    if not Path(TAILSCALE).exists():
        return ""
    try:
        return subprocess.run([TAILSCALE, "serve", "status"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _gpu() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip().splitlines()[0]
        used, total, util = (part.strip() for part in out.split(","))
        return f"显存 {int(used) / 1024:.1f} / {int(total) / 1024:.1f} GB，GPU 占用 {util}%"
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return "无法读取（nvidia-smi 不可用）"


def _health() -> str:
    try:
        import httpx
        warnings.filterwarnings("ignore")
        page = httpx.get(f"https://127.0.0.1:{PORT}/", verify=False, timeout=5).status_code
        api = httpx.get(f"https://127.0.0.1:{PORT}/api/sessions", verify=False, timeout=5).status_code
    except Exception as exc:  # noqa: BLE001 - any failure is the answer
        return f"无响应（{type(exc).__name__}）"
    ok = page == 200 and api == 401
    return f"{'正常' if ok else '异常'}（页面 {page}，接口未登录时 {api}）"


def _env_value(name: str) -> str:
    env = ROOT / ".env"
    if os.environ.get(name):
        return os.environ[name]
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return ""


def _kimi_balance() -> str:
    """Free account endpoint, no model call."""
    key = _env_value("MOONSHOT_API_KEY")
    base = _env_value("MOONSHOT_BASE_URL") or "https://api.moonshot.cn/v1"
    if not key:
        return "未配置 MOONSHOT_API_KEY"
    try:
        import httpx
        data = httpx.get(f"{base}/users/me/balance", headers={"Authorization": f"Bearer {key}"}, timeout=8).json()
        return f"¥{float(data['data']['available_balance']):.2f}"
    except Exception as exc:  # noqa: BLE001
        return f"查询失败（{type(exc).__name__}）"


def _uptime(started_iso: str | None) -> str:
    if not started_iso:
        return ""
    try:
        started = datetime.fromisoformat(started_iso[:26])
    except ValueError:
        return ""
    minutes = int((datetime.now() - started).total_seconds() // 60)
    return f"已运行 {minutes // 60} 小时 {minutes % 60} 分钟（{started:%m-%d %H:%M} 启动）"


def print_urls(serve_status: str | None = None) -> None:
    urls = tailscale_urls(serve_status if serve_status is not None else _serve_status())
    print(f"  本机：     https://localhost:{PORT}")
    for url, public in urls:
        print(f"  {'公网：' if public else 'Tailscale：'}{' ' * (5 if public else 1)}{url}"
              + ("   ← 互联网上任何人都能打开登录页" if public else ""))
    if not urls:
        print("  （Tailscale 没有转发到本服务的地址）")


def cmd_status(_args) -> int:
    info = listener()
    print("=" * 60)
    if info is None:
        print(f"KorASR：未运行（端口 {PORT} 空闲）")
    elif not is_korasr(info):
        print(f"端口 {PORT} 被其他程序占用：PID {info['pid']}  {info.get('path') or '?'}")
    else:
        print(f"KorASR：运行中   PID {info['pid']}   {_uptime(info.get('started'))}")
        print(f"  内存 {int(info.get('mem') or 0) / 2**30:.1f} GB，监听 {info.get('address')}:{PORT}")
        print(f"  健康检查：{_health()}")
    run = last_run(read_log_tail())
    if run["started"]:
        state = "已就绪" if run["ready"] else ("正在启动或启动失败" if info else "未就绪")
        print(f"  最近一次启动：{run['started'][:19].replace('T', ' ')}，{state}")
        if run["errors"]:
            print(f"  本次运行日志里有 {len(run['errors'])} 行像错误的记录，最近 3 行：")
            for line in run["errors"][-3:]:
                print("    " + line[:150])
    counts = class_counts()
    if "error" in counts:
        print(f"  {counts['error']}")
    else:
        recording = f"{counts['recording']} 节正在录音" if counts["recording"] else "没有课在录音"
        print(f"  课堂：{recording}，{counts['paused']} 节已暂停，共 {counts['total']} 节（{counts['path'].name}）")
    print(f"  GPU：{_gpu()}")
    print(f"  Kimi 余额：{_kimi_balance()}")
    print("访问地址：")
    print_urls()
    print(f"日志：{LOG}")
    print("=" * 60)
    return 0


def _wait_port_free(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if listener() is None:
            return True
        time.sleep(1)
    return False


def cmd_stop(args) -> int:
    info = listener()
    if info is None:
        print("KorASR 没有在运行。")
        return 0
    if not is_korasr(info):
        print(f"端口 {PORT} 被其他程序占用（PID {info['pid']}，{info.get('path') or '?'}），不是 KorASR，没有停止它。")
        return 1
    counts = class_counts()
    if counts.get("recording"):
        print(f"注意：有 {counts['recording']} 节课正在录音。停止后，下次启动时它们会变成“已暂停”，可以在课堂记录里继续。")
        if not args.yes and input("确定停止？输入 y 并回车确认：").strip().lower() != "y":
            print("已取消。")
            return 1
    subprocess.run(["taskkill", "/PID", str(info["pid"]), "/F"], capture_output=True)
    if _wait_port_free(20):
        print(f"[OK] KorASR 已停止（PID {info['pid']}）。GPU：{_gpu()}")
        return 0
    print(f"[失败] 20 秒后端口 {PORT} 仍被占用，请再运行一次，或在任务管理器里结束 PID {info['pid']}。")
    return 1


def cmd_start(_args) -> int:
    info = listener()
    if info is not None:
        if is_korasr(info):
            print(f"KorASR 已在运行（PID {info['pid']}），不用再启动。")
            print_urls()
            return 0
        print(f"端口 {PORT} 被其他程序占用（PID {info['pid']}，{info.get('path') or '?'}），没有启动。")
        return 1
    if not Path(PYW).exists():
        print(f"找不到 {PYW}。请在 scripts\\service-env.cmd 里改 KORASR_PYW。")
        return 1
    LOG.parent.mkdir(exist_ok=True)
    offset = LOG.stat().st_size if LOG.exists() else 0
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    launch = dict(
        cwd=ROOT, env=os.environ.copy(), close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    command = [PYW, "start.py", "--host", HOST, "--port", str(PORT)]
    try:
        # Leave the launcher's job object, so closing the window or tool that ran
        # this script does not take the service down with it.
        proc = subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **launch)
    except OSError:   # the job does not allow breakaway; a plain detached start still survives Explorer
        proc = subprocess.Popen(command, creationflags=flags, **launch)
    print("正在启动 KorASR（加载模型约 20–60 秒；前端代码有更新时会先重新构建）", end="", flush=True)
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(2)
        print(".", end="", flush=True)
        with open(LOG, "rb") as fh:
            fh.seek(offset)
            new = fh.read().decode("utf-8", errors="replace")
        if READY in new and listener() is not None:
            print(f"\n[OK] KorASR 已启动（PID {proc.pid}）。")
            print_urls()
            return 0
        if proc.poll() is not None:
            print(f"\n[失败] 服务进程已退出（代码 {proc.returncode}）。日志最后几行：")
            for line in new.splitlines()[-15:]:
                print("  " + line[:160])
            return 1
    print("\n[失败] 4 分钟内没有就绪。日志最后几行：")
    for line in read_log_tail(20_000).splitlines()[-15:]:
        print("  " + line[:160])
    return 1


def cmd_restart(args) -> int:
    if cmd_stop(args) != 0:
        return 1
    time.sleep(2)
    return cmd_start(args)


def cmd_logs(args) -> int:
    if not LOG.exists():
        print(f"还没有日志：{LOG}")
        return 1
    for line in read_log_tail(200_000).splitlines()[-args.n:]:
        print(line)
    if not args.follow:
        return 0
    print("—— 实时跟踪中，按 Ctrl+C 退出 ——")
    with open(LOG, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = fh.read()
                if chunk:
                    print(chunk.decode("utf-8", errors="replace"), end="", flush=True)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="KorASR service control")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("start")
    for name in ("stop", "restart"):
        sub.add_parser(name).add_argument("--yes", action="store_true", help="do not ask while a class is recording")
    logs = sub.add_parser("logs")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("-n", type=int, default=40)
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    handler = {"status": cmd_status, "start": cmd_start, "stop": cmd_stop,
               "restart": cmd_restart, "logs": cmd_logs}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())

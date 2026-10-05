"""
korAsr startup script — auto-generates a self-signed TLS cert so that
LAN devices (phones, laptops) can access the microphone over HTTPS.

Usage:
    python start.py            # HTTPS on port 8000
    python start.py --port 443 # custom port

First run: certificate is generated in certs/ and covers all local IPs.
Browser will show a security warning — click Advanced → Proceed (once per device).
"""
import argparse
import datetime
import ipaddress
import os
import shutil
import site
import socket
import subprocess
import sys
from pathlib import Path

from backend.config import settings
from backend.log_tee import TimestampedTee

# Windows 控制台默认 cp1252，print 任何非 Latin-1 字符（韩文/中文/→）都会
# UnicodeEncodeError 把整个 request handler 崩掉。强制 stdout/stderr 走 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# 文件日志 Tee：Windows 下 detached 启动（无 console / -WindowStyle Hidden）时
# stdout/stderr 没地方可写或写完即丢，调试两眼一抹黑。把所有输出 mirror 到
# logs/service.log，写多少都不丢；同时仍保留 console 输出（前台跑时一样可见）。
# Every line starts with the wall-clock time ("10:53:10.718 "); see backend/log_tee.py.
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
# append 模式：保留历史方便 grep；如果太大用户自己 truncate 或加 rotation
_LOG_FH = open(_LOG_DIR / "service.log", "a", encoding="utf-8", buffering=1)
_LOG_FH.write(f"\n===== service start @ {__import__('datetime').datetime.now().isoformat()} =====\n")

sys.stdout = TimestampedTee(sys.stdout, _LOG_FH)
sys.stderr = TimestampedTee(sys.stderr, _LOG_FH)

CERT_DIR = Path("certs")
CERT_PATH = CERT_DIR / "cert.pem"
KEY_PATH = CERT_DIR / "key.pem"


def _ensure_frontend_build():
    """Never serve an older SPA after a backend-only service start."""
    frontend_dir = Path(__file__).resolve().parent / "frontend"
    dist_index = frontend_dir / "dist" / "index.html"
    sources = [frontend_dir / "package.json", frontend_dir / "package-lock.json", frontend_dir / "vite.config.js"]
    sources.extend(p for p in (frontend_dir / "src").rglob("*") if p.is_file())
    newest_source = max((p.stat().st_mtime for p in sources if p.exists()), default=0)
    if dist_index.exists() and dist_index.stat().st_mtime >= newest_source:
        return
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm or not (frontend_dir / "node_modules").exists():
        raise RuntimeError(
            "Frontend build is missing or stale. Run 'cd frontend; npm ci; npm run build' "
            "before starting KorASR."
        )
    print("[Frontend] Source is newer than dist; rebuilding...")
    subprocess.run([npm, "run", "build"], cwd=frontend_dir, check=True)


def _add_nvidia_to_path():
    """
    Find NVIDIA CUDA DLL directories installed via pip (nvidia-cublas-cu12 etc.)
    and prepend them to PATH so CTranslate2 / faster-whisper can load them.
    """
    search_dirs = []
    try:
        search_dirs += site.getsitepackages()
    except AttributeError:
        pass
    try:
        search_dirs.append(site.getusersitepackages())
    except AttributeError:
        pass

    added = []
    current_path = os.environ.get("PATH", "")
    for site_dir in search_dirs:
        nvidia_dir = Path(site_dir) / "nvidia"
        if not nvidia_dir.exists():
            continue
        for pkg_dir in nvidia_dir.iterdir():
            bin_dir = pkg_dir / "bin"
            if bin_dir.exists() and str(bin_dir) not in current_path:
                added.append(str(bin_dir))

    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + current_path
        print(f"[CUDA] Added {len(added)} NVIDIA DLL path(s) to PATH")


def _local_ips() -> set[str]:
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            try:
                ipaddress.IPv4Address(addr)
                ips.add(addr)
            except ValueError:
                pass
    except Exception:
        pass
    # gethostname 在 Windows 上拿不到 Tailscale/VPN 虚拟网卡 IP，用 env 兜底加上
    for extra in settings.extra_cert_sans.split(","):
        extra = extra.strip()
        if not extra:
            continue
        try:
            ipaddress.IPv4Address(extra)
            ips.add(extra)
        except ValueError:
            pass
    return ips


def _extra_dns_sans() -> list[str]:
    raw = settings.extra_cert_sans
    out = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ipaddress.IPv4Address(token)  # already handled in _local_ips
        except ValueError:
            out.append(token)
    return out


def generate_cert():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    print("[SSL] Generating self-signed certificate...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    san: list[x509.GeneralName] = [x509.DNSName("localhost")]
    for ip in _local_ips():
        san.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
    for dns in _extra_dns_sans():
        san.append(x509.DNSName(dns))

    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "korAsr-local")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )

    CERT_DIR.mkdir(exist_ok=True)
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_PATH.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    print(f"[SSL] Certificate saved to {CERT_PATH}")
    print(f"[SSL] Covers IPs: {', '.join(sorted(_local_ips()))}")


def main():
    parser = argparse.ArgumentParser(description="Start korAsr with HTTPS")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address; use 127.0.0.1 behind Tailscale Serve")
    parser.add_argument("--regen-cert", action="store_true", help="Regenerate certificate")
    args = parser.parse_args()

    # Must happen BEFORE importing anything CUDA-related
    _add_nvidia_to_path()
    _ensure_frontend_build()

    if not CERT_PATH.exists() or args.regen_cert:
        generate_cert()
    else:
        print(f"[SSL] Using existing certificate ({CERT_PATH})")

    lan_ips = (_local_ips() - {"127.0.0.1"}) if args.host == "0.0.0.0" else set()
    print()
    print("=" * 55)
    print("  Access URLs (accept the browser security warning):")
    print(f"  https://localhost:{args.port}  <- this machine")
    for ip in sorted(lan_ips):
        print(f"  https://{ip}:{args.port}  <- LAN / phone")
    print("=" * 55)
    print()

    import uvicorn
    server_options = dict(
        host=args.host,
        port=args.port,
        ssl_certfile=str(CERT_PATH),
        ssl_keyfile=str(KEY_PATH),
        timeout_graceful_shutdown=30,
    )
    if os.name == "nt":
        # Windows Proactor transports can raise WinError 10054 in
        # _call_connection_lost before detaching from asyncio.Server. The
        # server then hangs in wait_closed() after Ctrl+C. Selector avoids
        # that transport path while supporting this HTTP/WebSocket server.
        import asyncio
        config = uvicorn.Config("backend.main:app", loop="none", **server_options)
        asyncio.run(uvicorn.Server(config).serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        uvicorn.run("backend.main:app", **server_options)


if __name__ == "__main__":
    main()

"""Local launcher for Faheem Pharmacy.

Starts the FastAPI app with Uvicorn (Linux under WSL). No arguments needed. Optional flags:
    --host 127.0.0.1  --port 8000  --no-browser
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import threading
import webbrowser
from pathlib import Path

from app.config import HOST, LOG_DIR, PORT


def windows_busy_ports() -> set[int]:
    """Ports Windows is listening on, when running under WSL.

    WSL forwards localhost to Windows, so a port that is free inside WSL but
    taken by a Windows program would send the browser to the wrong app.
    """
    try:
        if "microsoft" not in Path("/proc/version").read_text().lower():
            return set()
        out = subprocess.run(
            ["netstat.exe", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=10, cwd="/mnt/c" if Path("/mnt/c").is_dir() else None,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    ports: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "TCP" and parts[3] == "LISTENING":
            port = parts[1].rsplit(":", 1)[-1]
            if port.isdigit():
                ports.add(int(port))
    return ports


def find_free_port(host: str, start: int) -> int:
    busy = windows_busy_ports()
    for port in range(start, start + 50):
        if port in busy:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return start


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Faheem Pharmacy locally")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    port = find_free_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    # Record the chosen port so a launcher can open the right URL.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / ".port").write_text(str(port), encoding="utf-8")
    except OSError:
        pass

    if not args.no_browser:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    print("=" * 60)
    print("  Faheem Pharmacy Management System")
    print(f"  Open: {url}")
    print("  Press Ctrl+C to stop")
    print("=" * 60)

    import uvicorn

    from app.main import app

    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()

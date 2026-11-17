"""cloudflared quick tunnel：把手機 app 開一個臨時對外網址（*.trycloudflare.com）。"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .platform_support import bundled_tool

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# winget 安裝的預設位置（剛裝完時，已開著的視窗 PATH 可能還沒更新）
DEFAULT_INSTALL_PATHS = [
    Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
    Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
]


class TunnelError(Exception):
    pass


def parse_tunnel_url(line: str) -> str | None:
    m = _URL_RE.search(line)
    return m.group(0) if m else None


@dataclass
class TunnelHandle:
    url: str
    process: subprocess.Popen

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def find_cloudflared(configured: Path | None) -> Path | None:
    if configured:
        return configured
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    bundled = bundled_tool("cloudflared")
    if bundled:
        return bundled
    return next((p for p in DEFAULT_INSTALL_PATHS if p.exists()), None)


def start_quick_tunnel(port: int, exe: Path | None, timeout: float = 20) -> TunnelHandle:
    if exe is None or not Path(exe).exists():
        raise TunnelError("找不到 cloudflared，請先安裝（見 README）")
    try:
        proc = subprocess.Popen(
            [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        raise TunnelError(f"無法啟動 cloudflared：{e}") from e

    lines: queue.Queue[str] = queue.Queue()

    def pump():
        for line in proc.stderr:  # cloudflared 把網址印在 stderr
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        url = parse_tunnel_url(line)
        if url:
            return TunnelHandle(url, proc)
    TunnelHandle("", proc).stop()
    raise TunnelError("cloudflared 未在時限內提供網址（網路可能被擋）")

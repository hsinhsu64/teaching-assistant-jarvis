"""OpenEvidence 經使用者已登入的瀏覽器分頁送出（參考 htlin222/openevidence-mcp）。

openevidence-mcp 的 relay（127.0.0.1:8787）＋瀏覽器擴充功能，把請求交給使用者自己登入的
OpenEvidence 分頁執行，帶的是真正的瀏覽器登入狀態。本程式只是把 oe.py 送請求的那一步改走 relay：
relay 沒有連線就照舊用 cookies.json。驗證（CAPTCHA）若在分頁出現，由使用者自己在分頁通過，
本程式不嘗試繞過。

用法（子行程）：python oe_relay.py <oe.py 的參數…>，oe.py 的路徑放在環境變數 XIAOSHEN_OE_SCRIPT。
只用標準函式庫（直接以檔案執行，不依賴 xiaoshen 套件）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

RELAY = "http://127.0.0.1:8787"
HEALTH_TIMEOUT = 1.5
REQUEST_TIMEOUT_MS = 60_000


def _get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def relay_connected(get: Callable[[str, float], dict] = _get_json) -> bool:
    """relay 在跑、而且瀏覽器擴充功能已連上（有登入的 OpenEvidence 分頁可用）。"""
    try:
        h = get(f"{RELAY}/health", HEALTH_TIMEOUT)
    except (OSError, ValueError):
        return False
    return bool(h.get("ok")) and h.get("connected") is True


def relay_request(method: str, path: str, body: dict | None = None,
                  post: Callable[[str, dict, float], dict] = _post_json,
                  timeout_ms: int = REQUEST_TIMEOUT_MS) -> tuple[int, bytes]:
    """請 relay 在瀏覽器分頁裡送出這個請求；回傳（HTTP 狀態, 內容）。"""
    payload = {"method": method, "path": path, "timeoutMs": timeout_ms}
    if body is not None:
        payload["body"] = json.dumps(body)
    res = post(f"{RELAY}/relay", payload, timeout_ms / 1000 + 15)
    return int(res.get("status") or 0), str(res.get("body") or "").encode("utf-8")


def run_oe(script: Path, argv: list[str], connected: Callable[[], bool] = relay_connected,
           request: Callable[..., tuple[int, bytes]] = relay_request) -> None:
    """載入 oe.py；relay 有連線就把它送請求的 _request 換成走瀏覽器分頁，然後照原本的參數執行。"""
    spec = importlib.util.spec_from_file_location("oe_skill", script)
    module = importlib.util.module_from_spec(spec)
    sys.argv = [str(script), *argv]
    spec.loader.exec_module(module)
    if connected():
        _ensure_cookie_file(module)
        module._request = lambda jar, method, path, body=None: request(method, path, body)
    module.main()


def _ensure_cookie_file(module) -> None:
    """只在瀏覽器分頁登入、沒有 cookies.json 的電腦（2026-11-08）：oe.py 一開始一定要讀到 cookie 檔，
    給它一個只含假 cookie（網域 invalid.example）的檔案。請求都走 relay，這個 cookie 不會被送出。"""
    find = getattr(module, "resolve_cookies_path", None)
    if find is None:
        return
    try:
        find()
        return
    except SystemExit:
        pass
    placeholder = Path(tempfile.gettempdir()) / "xiaoshen-oe-relay-cookies.json"
    placeholder.write_text(json.dumps([{"name": "xiaoshen_relay", "value": "", "domain": "invalid.example",
                                        "path": "/"}]), encoding="utf-8")
    os.environ["OE_COOKIES"] = str(placeholder)


if __name__ == "__main__":
    use_relay = os.environ.get("XIAOSHEN_OE_RELAY", "auto") != "off"
    run_oe(Path(os.environ["XIAOSHEN_OE_SCRIPT"]), sys.argv[1:],
           connected=relay_connected if use_relay else (lambda: False))

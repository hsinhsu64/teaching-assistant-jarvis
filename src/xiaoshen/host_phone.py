"""主持人手機（spec §6.3b）：筆電 QR Code 配對後，手機經對外通道使用主持人功能。

對外通道只連到這裡的 gateway：
- 沒配對：完全等於住院醫師用的手機 app，碰不到任何主持人路由。
- 配對後：轉給控制 app，但拿不到投影片圖片（病例簡報畫面不經過對外通道），也不能配對或解除。
憑證只在記憶體，程式結束就失效。
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from typing import Callable
from urllib.parse import parse_qs

from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import eventlog

CODE_TTL = 120  # QR Code 有效秒數
COOKIE = "xs_host"
PAIR_PATH = "/h"
# 手機 app 的路由。例外的兩個是主持人開關，走控制 app。
PHONE_PATHS = {"/q", "/healthz"}
HOST_SWITCHES = {"/api/phone/control-enabled", "/api/phone/search-enabled"}
# 配對的手機也不給：投影片圖片、投影頁、配對本身
DENIED_EXACT = {"/display", "/source/page.png"}
DENIED_PREFIX = ("/slides/", "/deck/links/", "/api/host-phone/")
BAD_CODE_PAGE = ("<!doctype html><meta charset='utf-8'>"
                 "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                 "<p>這個 QR Code 已失效，請在筆電的主持人畫面重新產生。</p>")


class HostPairing:
    """一次性配對碼＋目前唯一的主持人手機憑證。"""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._lock = threading.Lock()
        self._code: str | None = None
        self._expires = 0.0
        self._session: str | None = None

    @property
    def paired(self) -> bool:
        return self._session is not None

    def new_code(self) -> str:
        with self._lock:
            self._code = secrets.token_urlsafe(24)
            self._expires = self._now() + CODE_TTL
            return self._code

    def redeem(self, code: str) -> str | None:
        """配對碼換憑證；取代舊手機。配對碼只能用一次。"""
        with self._lock:
            if not self._code or not code or self._now() > self._expires:
                return None
            if not hmac.compare_digest(self._code.encode(), code.encode()):
                return None
            self._code = None
            self._session = secrets.token_urlsafe(32)
            return self._session

    def valid(self, session: str | None) -> bool:
        with self._lock:
            return bool(self._session and session) and hmac.compare_digest(self._session.encode(), session.encode())

    def revoke(self) -> None:
        with self._lock:
            self._code = None
            self._session = None


def _session_cookie(scope: dict) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            try:
                morsel = SimpleCookie(value.decode("latin-1")).get(COOKIE)
            except CookieError:
                return None
            return morsel.value if morsel else None
    return None


def _phone_side(path: str) -> bool:
    return path in PHONE_PATHS or (path.startswith("/api/phone/") and path not in HOST_SWITCHES)


def _denied(path: str) -> bool:
    return path in DENIED_EXACT or path.startswith(DENIED_PREFIX)


def create_gateway(phone_app, control_app, state):
    """對外通道的入口：包住手機 app，配對過的手機才轉給控制 app。"""
    pairing: HostPairing = state.host_pairing

    def pair_response(query: bytes):
        session = pairing.redeem(parse_qs(query.decode("latin-1")).get("c", [""])[0])
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if session is None:
            return HTMLResponse(BAD_CODE_PAGE, 403, headers=headers)
        eventlog.log("host_phone", action="paired")
        state._changed()
        response = RedirectResponse("/host", 303, headers=headers)
        # Lax：掃 QR Code 由相機 app 開啟也帶得到 cookie；跨站 POST 與 WebSocket 不會帶
        response.set_cookie(COOKIE, session, httponly=True, secure=True, samesite="lax", path="/")
        return response

    async def gateway(scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await phone_app(scope, receive, send)  # lifespan
            return
        path = scope["path"]
        if scope["type"] == "http" and path == PAIR_PATH:
            await pair_response(scope.get("query_string", b""))(scope, receive, send)
        elif _phone_side(path) or not pairing.valid(_session_cookie(scope)):
            await phone_app(scope, receive, send)
        elif _denied(path):
            await PlainTextResponse("手機不提供這項內容", 403)(scope, receive, send)
        else:
            await control_app(scope, receive, send)

    return gateway

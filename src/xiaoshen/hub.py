"""WebSocket 廣播：投影畫面與主持人畫面各收自己受眾的快照。"""

from __future__ import annotations

import asyncio

from fastapi import WebSocket

from .state import AppState, Audience


class Hub:
    def __init__(self):
        self._clients: list[tuple[WebSocket, Audience]] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, ws: WebSocket, audience: Audience, state: AppState) -> None:
        await ws.accept()
        self.loop = asyncio.get_running_loop()
        self._clients.append((ws, audience))
        await ws.send_json(state.snapshot(audience))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients = [(w, a) for w, a in self._clients if w is not ws]

    async def broadcast(self, state: AppState) -> None:
        snaps: dict[str, dict] = {}
        for ws, audience in list(self._clients):
            if audience not in snaps:
                snaps[audience] = state.snapshot(audience)
            try:
                await ws.send_json(snaps[audience])
            except Exception:
                self.disconnect(ws)

    def notify(self, state: AppState) -> None:
        """可從任何執行緒／事件迴圈呼叫：把廣播排進 WebSocket 所在的迴圈。"""
        if self.loop is None or self.loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(self.broadcast(state))
        else:
            asyncio.run_coroutine_threadsafe(self.broadcast(state), self.loop)

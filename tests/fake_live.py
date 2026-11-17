"""測試用的假 Live 後端：依腳本吐事件，記錄收到的音訊與工具結果。"""

import asyncio
from contextlib import asynccontextmanager

from xiaoshen.live_backend import LiveEvent


class FakeLiveSession:
    def __init__(self, backend):
        self.backend = backend

    async def send_audio(self, pcm16k: bytes) -> None:
        self.backend.audio_bytes += len(pcm16k)

    async def send_tool_results(self, results) -> None:
        self.backend.tool_results.extend(results)
        self.backend.results_event.set()

    async def events(self):
        for ev in self.backend.script:
            if ev == "wait_results":  # 等工具結果回來再繼續
                await self.backend.results_event.wait()
                self.backend.results_event.clear()
                continue
            if isinstance(ev, (int, float)):
                await asyncio.sleep(ev)
                continue
            yield ev
        while True:  # 腳本跑完就保持連線、不再有事件
            await asyncio.sleep(3600)


class FakeLiveBackend:
    def __init__(self, script: list | None = None, fail: Exception | None = None, connect_delay: float = 0):
        self.script = script or []
        self.connect_delay = connect_delay
        self.fail = fail
        self.audio_bytes = 0
        self.tool_results = []
        self.connects = 0
        self.systems = []
        self.results_event = asyncio.Event()

    @asynccontextmanager
    async def connect(self, system, tools):
        self.connects += 1
        self.systems.append(system)
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.fail:
            raise self.fail
        yield FakeLiveSession(self)

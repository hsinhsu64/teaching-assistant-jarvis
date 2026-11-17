"""主持人手機的麥克風（spec §6.3b）。

手機按「叫醒」：手機瀏覽器收音（16 kHz int16 單聲道）經 WebSocket 送來，
那一輪對話只用手機的聲音、不收筆電麥克風；回到待命就改回筆電麥克風。
助理的回答照舊從筆電喇叭播出（會議室聽得到）。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import AsyncIterable, AsyncIterator

BLOCK_BYTES = 3200          # 100 ms：16 kHz × 2 bytes × 0.1 s，和筆電麥克風一樣
ACTIVE = ("listening", "thinking", "speaking")
NO_SESSION_BLOCKS = 50      # 手機開了麥克風但 5 秒內沒進入對話（叫醒失敗）→ 改回筆電麥克風
MAX_QUEUE = 50              # 最多累積 5 秒，再多就丟最舊的
SILENCE = bytes(BLOCK_BYTES)


class PhoneMic:
    """手機送來的音訊；同時只認最新的一條連線（gen）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._gen = 0
        self.active = False
        self._buf = bytearray()
        self._queue: deque[bytes] = deque(maxlen=MAX_QUEUE)

    def start(self) -> int:
        with self._lock:
            self._gen += 1
            self.active = True
            self._buf.clear()
            self._queue.clear()
            return self._gen

    def feed(self, gen: int, pcm: bytes) -> bool:
        """收進來切成 100 ms 一塊。回傳 False＝這條連線已不用，請關閉。"""
        with self._lock:
            if not self.active or gen != self._gen:
                return False
            self._buf += pcm
            while len(self._buf) >= BLOCK_BYTES:
                self._queue.append(bytes(self._buf[:BLOCK_BYTES]))
                del self._buf[:BLOCK_BYTES]
            return True

    def take(self) -> bytes | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def stop(self, gen: int | None = None) -> None:
        """gen：只有這條連線還是最新的才停（舊連線斷掉不影響新的）。"""
        with self._lock:
            if gen is not None and gen != self._gen:
                return
            self.active = False
            self._buf.clear()
            self._queue.clear()


class PhoneMicMux:
    """語音用的麥克風：平常是筆電麥克風；手機收音中改送手機的聲音。
    節奏照筆電麥克風（每 100 ms 一塊）：手機沒送到就補靜音，送太多就一次多送幾塊，不累積延遲。"""

    def __init__(self, laptop: AsyncIterable[bytes], phone: PhoneMic, state):
        self.laptop = laptop
        self.phone = phone
        self.state = state

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._run()

    async def _run(self) -> AsyncIterator[bytes]:
        in_session = False
        waited = 0
        async for block in self.laptop:
            if self.phone.active:
                if self.state.voice_status in ACTIVE:
                    in_session = True
                elif in_session or waited >= NO_SESSION_BLOCKS:
                    self.phone.stop()  # 這一輪結束（或叫醒失敗）：改回筆電麥克風
                else:
                    waited += 1
            if not self.phone.active:
                in_session, waited = False, 0
                yield block
                continue
            got = self.phone.take()
            yield got if got is not None else SILENCE
            while self.phone.pending() >= 2:
                yield self.phone.take() or SILENCE

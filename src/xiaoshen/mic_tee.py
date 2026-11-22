"""會議錄音的收音：和語音用同一支會議室麥克風。

- 語音在跑：TeeMic 包住筆電麥克風，原樣交給語音，同時餵一份給錄音器（不另開裝置）。
- 語音沒在跑（停用或中途停止）：recorder_pump 在錄音期間自己開麥克風餵錄音器，停止錄音就關掉。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterable, AsyncIterator, Callable

from .recording import Recorder


class TeeMic:
    def __init__(self, source: AsyncIterable[bytes], recorder: Recorder):
        self.source = source
        self.recorder = recorder

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._run()

    async def _run(self) -> AsyncIterator[bytes]:
        async for block in self.source:
            try:
                self.recorder.feed(block)
            except Exception:
                pass  # 錄音出問題不能影響語音
            yield block

    def close(self) -> None:
        close = getattr(self.source, "close", None)
        if close:
            close()


async def recorder_pump(recorder: Recorder, mic_factory: Callable[[], AsyncIterable[bytes]],
                        voice_running: Callable[[], bool], idle: float = 0.2) -> None:
    """一直跑：錄音中而語音沒在餵時，自己開麥克風。"""
    while True:
        if not (recorder.is_recording and not voice_running()):
            await asyncio.sleep(idle)
            continue
        try:
            mic = mic_factory()
        except Exception as e:  # 沒有麥克風：存下已錄的部分並停止，主持人畫面顯示原因
            await asyncio.to_thread(recorder.stop)
            recorder.last_error = f"錄音的麥克風無法使用：{e}"
            continue
        try:
            async for block in mic:
                if not recorder.is_recording or voice_running():
                    break
                recorder.feed(block)
        except Exception as e:
            await asyncio.to_thread(recorder.stop)
            recorder.last_error = f"錄音的麥克風中斷：{e}"
        finally:
            close = getattr(mic, "close", None)
            if close:
                close()

"""麥克風（16 kHz int16，每塊 100 ms）與喇叭（24 kHz int16，Gemini Live 的輸出格式）。"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Literal

import numpy as np
import sounddevice as sd

MIC_RATE = 16000
SPK_RATE = 24000
BLOCK = 1600  # 100 ms
TAIL_MARGIN = 0.2  # 播完後再多算的安全時間


def find_device(name_part: str, kind: Literal["input", "output"], devices: list[dict] | None = None) -> int | None:
    """依名稱片段找裝置；找不到回傳 None（= 系統預設）。"""
    if not name_part:
        return None
    devices = list(sd.query_devices()) if devices is None else devices
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(devices):
        if d.get(key, 0) > 0 and name_part.lower() in str(d.get("name", "")).lower():
            return i
    return None


_MAPPER = ("sound mapper", "音效對應表")  # Windows MME 的「預設裝置」代稱，選單另有「電腦預設」


def list_device_names(kind: Literal["input", "output"], devices: list[dict] | None = None,
                      hostapi: int | None = None) -> list[str]:
    """「設定」選單用的裝置名稱：只列預設音訊介面（Windows MME／Mac Core Audio）的裝置，不重複。

    find_device 取第一個名稱相符的裝置，預設介面排在最前面，選到的名稱一定對得到同一個裝置。"""
    devices = list(sd.query_devices()) if devices is None else devices
    hostapi = sd.default.hostapi if hostapi is None else hostapi
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    names: list[str] = []
    for d in devices:
        name = str(d.get("name", ""))
        if (d.get("hostapi") == hostapi and d.get(key, 0) > 0 and name and "\n" not in name
                and not any(m in name.lower() for m in _MAPPER) and name not in names):
            names.append(name)
    return names


def to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    n = int(round(len(x) * dst_rate / src_rate))
    if n == 0 or len(x) == 0:
        return np.zeros(0, dtype=np.float32)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


class MicStream:
    """非同步逐塊讀取麥克風（int16 PCM bytes）。"""

    def __init__(self, device: int | None):
        self.device = device
        self._queue: asyncio.Queue[bytes] | None = None
        self._stream = None

    def __aiter__(self):
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=50)

        def callback(indata, frames, t, status):
            data = bytes(indata)

            def put():
                if self._queue.full():
                    self._queue.get_nowait()  # 處理不及時丟最舊的，不累積延遲
                self._queue.put_nowait(data)

            loop.call_soon_threadsafe(put)

        self._stream = sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                         device=self.device, latency="low", callback=callback)
        self._stream.start()
        return self

    async def __anext__(self) -> bytes:
        return await self._queue.get()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class Speaker:
    """is_playing() 在最後一塊交給音效卡後，還要再算一段尾巴：
    輸出延遲（聲音真正從喇叭出來）＋輸入延遲與一個區塊（麥克風收進來）＋安全時間。"""

    def __init__(self, device: int | None, input_latency: float = 0.1):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_audio = 0.0
        self._stream = sd.RawOutputStream(samplerate=SPK_RATE, channels=1, dtype="int16", device=device,
                                          latency="low", callback=self._callback)
        self._stream.start()
        output_latency = float(getattr(self._stream, "latency", 0.1) or 0.1)
        self.tail_seconds = output_latency + input_latency + BLOCK / MIC_RATE + TAIL_MARGIN

    def _callback(self, outdata, frames, t, status) -> None:
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        if chunk:
            self._last_audio = time.monotonic()
        outdata[:len(chunk)] = chunk
        outdata[len(chunk):] = b"\x00" * (n - len(chunk))

    def play(self, pcm24k: bytes) -> None:
        with self._lock:
            self._buf += pcm24k
        self._last_audio = time.monotonic()

    def is_playing(self) -> bool:
        with self._lock:
            if self._buf:
                return True
        return time.monotonic() - self._last_audio < self.tail_seconds

    def stop(self) -> None:
        with self._lock:
            self._buf.clear()
        self._last_audio = time.monotonic()  # 裝置緩衝裡已送出的聲音還會播完，尾巴照算

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


def probe_mic(device: int | None, seconds: float = 0.5) -> float:
    """錄一小段回傳 RMS（自我檢查用）。"""
    rec = sd.rec(int(MIC_RATE * seconds), samplerate=MIC_RATE, channels=1, dtype="float32", device=device)
    sd.wait()
    return float(np.sqrt(np.mean(np.square(rec))))

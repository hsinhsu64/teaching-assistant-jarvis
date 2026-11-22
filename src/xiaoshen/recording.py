"""會議錄音器（按鈕開始／停止；不用語音指令，怕誤觸）。

麥克風每塊 100 ms 的 int16 PCM（16 kHz 單聲道）交給 feed()：
- 邊錄邊寫本機暫存 <temp>/<id>.part.pcm（沒有檔頭，程式當掉時已寫的部分照樣可讀），至少每 60 秒寫到磁碟一次；
- stop()：暫存轉 FLAC → 複製到 紀錄\\錄音\\<id>.flac → 複製完才寫 <id>.ready（工作站只處理有 .ready 的）→ 刪暫存；
- 超過 recording_max_minutes 自動停止並存檔；
- recover_unfinished()：啟動時把上次沒收尾的暫存收尾成 .flac＋.ready 照常送出。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np

from . import eventlog
from . import meeting_store as ms
from .config import Config

RATE = 16000
FLUSH_SECONDS = 60
PART_SUFFIX = ".part.pcm"


def _pcm_to_flac(part: Path, dest: Path) -> bool:
    """回傳 False＝暫存是空的（沒有聲音可存）。奇數 byte（寫到一半）捨去最後一個。"""
    import soundfile as sf

    raw = part.read_bytes()
    raw = raw[:len(raw) // 2 * 2]
    if not raw:
        return False
    sf.write(dest, np.frombuffer(raw, dtype=np.int16), RATE, format="FLAC", subtype="PCM_16")
    return True


def _level(block: bytes) -> float:
    x = np.frombuffer(block[:len(block) // 2 * 2], dtype=np.int16).astype(np.float64)
    if not x.size:
        return 0.0
    rms = float(np.sqrt(np.mean(x * x))) / 32768.0
    if rms <= 0:
        return 0.0
    return round(min(1.0, max(0.0, (20 * np.log10(rms) + 60) / 60)), 3)


class Recorder:
    def __init__(self, config: Config, now: Callable[[], datetime] = datetime.now,
                 clock: Callable[[], float] = time.monotonic, temp_dir: Path | None = None):
        self.config = config
        self._now = now
        self._clock = clock
        self.temp_dir = Path(temp_dir) if temp_dir is not None else ms.recordings_dir()
        self._lock = threading.RLock()
        self._file: BinaryIO | None = None
        self._started = 0.0
        self._last_flush = 0.0
        self.id: str | None = None
        self.last_path: Path | None = None   # 最近一次存好的錄音
        self.last_error: str = ""            # 最近一次存檔失敗的原因
        self._finisher: threading.Thread | None = None
        self.on_change: Callable[[], None] | None = None
        self._level = 0.0       # 最近一塊的音量（0～1），主持人畫面的音量條用
        self._level_at = 0.0

    # ---- 狀態 ----

    @property
    def is_recording(self) -> bool:
        return self._file is not None

    @property
    def level(self) -> float:
        """音量 0～1（-60～0 dBFS）；沒在錄、或超過 1 秒沒有聲音進來（麥克風斷了）是 0。"""
        with self._lock:
            if self._file is None or self._clock() - self._level_at > 1.0:
                return 0.0
            return self._level

    @property
    def elapsed(self) -> float:
        with self._lock:
            return self._clock() - self._started if self._file is not None else 0.0

    def _taken(self, mid: str) -> bool:
        p = ms.paths(self.config, mid)
        return p.audio.exists() or p.ready.exists() or (self.temp_dir / f"{mid}{PART_SUFFIX}").exists()

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ---- 錄音 ----

    def start(self) -> str:
        with self._lock:
            if self._file is not None:
                return self.id
            self.wait_idle(30)  # 自動停止的存檔還在跑：等它做完
            mid = ms.new_id(self._now(), self._taken)
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            self._file = open(self.temp_dir / f"{mid}{PART_SUFFIX}", "ab")
            self.id = mid
            self._started = self._last_flush = self._clock()
            self.last_error = ""
        eventlog.log("recording", action="start", id=mid)
        self._changed()
        return mid

    def feed(self, block: bytes) -> None:
        with self._lock:
            if self._file is None:
                return
            self._file.write(block)
            now = self._clock()
            self._level, self._level_at = _level(block), now
            if now - self._last_flush >= FLUSH_SECONDS:
                self._sync()
                self._last_flush = now
            if now - self._started > self.config.recording_max_minutes * 60:
                file, mid = self._detach()
            else:
                return
        eventlog.log("recording", action="max_reached", id=mid)
        # 轉檔要幾秒：不要卡住麥克風（語音也在用同一個串流）
        self._finisher = threading.Thread(target=self._finish, args=(file, mid), daemon=True)
        self._finisher.start()
        self._changed()

    def stop(self) -> Path | None:
        with self._lock:
            if self._file is None:
                return None
            file, mid = self._detach()
        path = self._finish(file, mid)
        self._changed()
        return path

    def wait_idle(self, timeout: float | None = None) -> None:
        t = self._finisher
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def _sync(self) -> None:
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError:
            pass

    def _detach(self) -> tuple[BinaryIO, str]:
        file, mid = self._file, self.id
        self._file, self.id = None, None
        return file, mid

    def _finish(self, file: BinaryIO, mid: str) -> Path | None:
        try:
            file.flush()
        finally:
            file.close()
        try:
            path = self._publish(mid)
        except Exception as e:  # 暫存留著，下次啟動會再收尾一次
            self.last_error = f"錄音存檔失敗：{e}"
            eventlog.log("recording", action="save_failed", id=mid)
            return None
        self.last_path = path
        eventlog.log("recording", action="saved", id=mid, file=path.name if path else "")
        return path

    def _publish(self, mid: str) -> Path | None:
        """暫存 → FLAC → 紀錄\\錄音；複製完成才寫 .ready。暫存是空的回 None（直接刪掉）。"""
        part = self.temp_dir / f"{mid}{PART_SUFFIX}"
        flac = self.temp_dir / f"{mid}.flac"
        p = ms.paths(self.config, mid)
        if not _pcm_to_flac(part, flac):
            part.unlink(missing_ok=True)
            return None
        p.audio.parent.mkdir(parents=True, exist_ok=True)
        partial = p.audio.with_name(p.audio.name + ".partial")
        shutil.copyfile(flac, partial)  # 先寫暫存名再換名：同步資料夾裡不會出現寫到一半的 .flac
        os.replace(partial, p.audio)
        # .ready 記下音檔大小：工作站據此確認雲端硬碟把整個音檔同步完了
        p.ready.write_text(json.dumps({"size": p.audio.stat().st_size,
                                       "saved": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        flac.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
        return p.audio

    def recover_unfinished(self) -> list[Path]:
        """上次錄音中當掉或斷電：把留下的暫存收尾送出（啟動時、還沒開始錄之前呼叫）。"""
        out: list[Path] = []
        if not self.temp_dir.is_dir():
            return out
        for part in sorted(self.temp_dir.glob(f"*{PART_SUFFIX}")):
            mid = part.name[:-len(PART_SUFFIX)]
            if not ms.valid_id(mid) or mid == self.id:
                continue
            p = ms.paths(self.config, mid)
            try:
                if p.ready.exists():  # 上次已經存好，只是暫存沒刪掉
                    part.unlink(missing_ok=True)
                    continue
                path = self._publish(mid)
            except Exception as e:
                self.last_error = f"上次的錄音收尾失敗：{e}"
                continue
            if path is not None:
                eventlog.log("recording", action="recovered", id=mid)
                out.append(path)
        return out

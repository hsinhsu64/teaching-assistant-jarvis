"""錄音器：邊錄邊寫本機暫存，停止後轉 FLAC 放進 紀錄\\錄音，寫完才建 .ready；當機後收尾。"""

from datetime import datetime

import numpy as np
import soundfile as sf

from xiaoshen import meeting_store as ms
from xiaoshen import recording
from xiaoshen.recording import Recorder

RATE = 16000
NOW = datetime(2026, 11, 22, 7, 30)


def tone(seconds: float) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(config, tmp_path, clock=None):
    return Recorder(config, now=lambda: NOW, clock=clock or Clock(), temp_dir=tmp_path / "tmp")


def feed_seconds(rec, seconds):
    block = tone(0.1)
    for _ in range(int(seconds * 10)):
        rec.feed(block)


def test_start_feed_stop_writes_flac_and_ready(config, tmp_path):
    rec = make(config, tmp_path)
    mid = rec.start()
    assert mid == "2026-11-22_0730" and rec.is_recording and rec.id == mid
    assert rec.start() == mid  # 已在錄：同一個 id
    feed_seconds(rec, 3)
    path = rec.stop()
    p = ms.paths(config, mid)
    assert path == p.audio and p.ready.exists()
    data, rate = sf.read(path, dtype="int16")
    assert rate == RATE and len(data) == 3 * RATE
    assert not rec.is_recording and rec.id is None
    assert list((tmp_path / "tmp").iterdir()) == []  # 暫存刪掉了
    assert rec.last_path == path


def test_ready_written_after_audio(config, tmp_path, monkeypatch):
    seen = []
    real_copy = recording.shutil.copyfile

    def spy(src, dst):
        seen.append(("copy", ms.paths(config, "2026-11-22_0730").ready.exists()))
        return real_copy(src, dst)

    monkeypatch.setattr(recording.shutil, "copyfile", spy)
    rec = make(config, tmp_path)
    rec.start()
    feed_seconds(rec, 1)
    rec.stop()
    assert seen == [("copy", False)]  # 複製音檔時還沒有 .ready
    assert ms.paths(config, "2026-11-22_0730").ready.exists()


def test_feed_ignored_when_not_recording(config, tmp_path):
    rec = make(config, tmp_path)
    rec.feed(tone(1))
    assert rec.stop() is None
    assert not (config.folder("紀錄") / "錄音").exists() or not any((config.folder("紀錄") / "錄音").iterdir())


def test_flushes_to_disk_every_minute(config, tmp_path):
    clock = Clock()
    rec = make(config, tmp_path, clock)
    rec.start()
    part = tmp_path / "tmp" / "2026-11-22_0730.part.pcm"
    rec.feed(tone(0.1))
    clock.t += 61
    rec.feed(tone(0.1))
    assert part.stat().st_size == 2 * 3200  # 程式在這裡當掉，已錄的部分也在磁碟上


def test_auto_stop_at_max_minutes(config, tmp_path):
    clock = Clock()
    rec = make(config, tmp_path, clock)
    rec.start()
    feed_seconds(rec, 1)
    clock.t += 180 * 60 - 1
    rec.feed(tone(0.1))
    assert rec.is_recording and rec.elapsed == 180 * 60 - 1
    clock.t += 2
    rec.feed(tone(0.1))
    assert not rec.is_recording
    rec.wait_idle(10)
    p = ms.paths(config, "2026-11-22_0730")
    assert p.audio.exists() and p.ready.exists()
    assert rec.last_path == p.audio


def test_recover_unfinished_part(config, tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    (tmp / "2026-11-21_0731.part.pcm").write_bytes(tone(2) + b"\x01")  # 寫到一半當掉：多一個 byte
    rec = make(config, tmp_path)
    out = rec.recover_unfinished()
    p = ms.paths(config, "2026-11-21_0731")
    assert out == [p.audio] and p.ready.exists()
    data, _ = sf.read(p.audio, dtype="int16")
    assert len(data) == 2 * RATE
    assert list(tmp.iterdir()) == []


def test_recover_ignores_empty_part(config, tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    (tmp / "2026-11-21_0731.part.pcm").write_bytes(b"")
    rec = make(config, tmp_path)
    assert rec.recover_unfinished() == []
    assert not ms.paths(config, "2026-11-21_0731").ready.exists()
    assert list(tmp.iterdir()) == []


def test_new_id_skips_existing_meeting(config, tmp_path):
    p = ms.paths(config, "2026-11-22_0730")
    p.ready.parent.mkdir(parents=True)
    p.ready.write_text("")
    rec = make(config, tmp_path)
    assert rec.start() == "2026-11-22_0730_2"


def test_ready_records_audio_size_and_copy_is_atomic(config, tmp_path, monkeypatch):
    """審查：.ready 記下音檔大小，工作站才分得出雲端硬碟只同步了一半；音檔先寫暫存名再換名。"""
    import json

    dests = []
    real_copy = recording.shutil.copyfile
    monkeypatch.setattr(recording.shutil, "copyfile", lambda s, d: dests.append(str(d)) or real_copy(s, d))
    rec = make(config, tmp_path)
    mid = rec.start()
    feed_seconds(rec, 1)
    path = rec.stop()
    assert dests and not dests[0].endswith(".flac")  # 不直接寫到同步資料夾裡的正式檔名
    info = json.loads(ms.paths(config, mid).ready.read_text(encoding="utf-8"))
    assert info["size"] == path.stat().st_size
    assert ms.audio_complete(ms.paths(config, mid))


def test_level_meter_while_recording(config, tmp_path):
    """使用者（2026-11-23）：錄音中，按鈕右側顯示音量條；level 0～1（-60 dBFS～0 dBFS）。"""
    clock = Clock()
    rec = make(config, tmp_path, clock)
    assert rec.level == 0.0  # 沒在錄
    rec.start()
    rec.feed(bytes(3200))  # 靜音
    assert rec.level == 0.0
    rec.feed(tone(0.1))    # 正弦波，峰值 8000（約 -15 dBFS RMS）
    assert 0.6 < rec.level < 0.85
    loud = (np.ones(1600) * 32767).astype(np.int16).tobytes()
    rec.feed(loud)
    assert rec.level == 1.0
    clock.t += 2          # 超過 1 秒沒有聲音進來（麥克風斷了）：歸零
    assert rec.level == 0.0
    rec.stop()
    assert rec.level == 0.0

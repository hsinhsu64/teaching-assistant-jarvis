import time

import numpy as np

import xiaoshen.audio_io as aio
from xiaoshen.audio_io import Speaker, find_device, resample, to_float, to_pcm16

DEVICES = [
    {"name": "Microsoft 音效對應表 - Input", "max_input_channels": 2, "max_output_channels": 0},
    {"name": "麥克風 (USBMIC1.)", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "喇叭 (USBMIC1.)", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Realtek Digital Output", "max_input_channels": 0, "max_output_channels": 2},
]


def test_find_device_by_substring():
    assert find_device("usbmic", "input", DEVICES) == 1
    assert find_device("USBMIC", "output", DEVICES) == 2


def test_find_device_missing_returns_none():
    assert find_device("Jabra", "input", DEVICES) is None
    assert find_device("", "input", DEVICES) is None


def test_pcm_roundtrip():
    x = np.array([0.0, 0.5, -0.5, 0.25], dtype=np.float32)
    assert np.allclose(to_float(to_pcm16(x)), x, atol=1e-4)


def test_resample_length():
    assert len(resample(np.zeros(2400, dtype=np.float32), 24000, 16000)) == 1600


class FakeOut:
    def __init__(self, **kw):
        self.kw = kw
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        pass


def test_speaker_is_playing_and_stop(monkeypatch):
    monkeypatch.setattr(aio.sd, "RawOutputStream", FakeOut)
    spk = Speaker(None)
    assert not spk.is_playing()
    spk.play(b"\x00\x00" * 2400)
    assert spk.is_playing()
    # 模擬音效卡把資料拉走
    out = bytearray(4800)
    spk._callback(out, 2400, None, None)
    assert spk.is_playing()  # 剛播完 300 ms 內仍算說話中（避免尾音回授）
    spk._last_audio = time.monotonic() - 1
    assert not spk.is_playing()


class LatentOut(FakeOut):
    latency = 0.25


def test_speaker_tail_includes_device_latency(monkeypatch):
    """喇叭出聲比 callback 晚一個輸出延遲，再被麥克風收進來又晚一個輸入延遲；尾巴要涵蓋。"""
    monkeypatch.setattr(aio.sd, "RawOutputStream", LatentOut)
    spk = Speaker(None, input_latency=0.15)
    assert spk.tail_seconds >= 0.25 + 0.15 + 0.1
    spk.play(b"\x00\x00" * 10)
    spk._callback(bytearray(20), 10, None, None)
    spk._last_audio = time.monotonic() - 0.45
    assert spk.is_playing()
    spk._last_audio = time.monotonic() - (spk.tail_seconds + 0.05)
    assert not spk.is_playing()


def test_speaker_stop_keeps_tail(monkeypatch):
    monkeypatch.setattr(aio.sd, "RawOutputStream", LatentOut)
    spk = Speaker(None)
    spk.play(b"\x01\x00" * 2400)
    spk.stop()
    assert spk.is_playing()  # 裝置緩衝裡的聲音還在播，尾巴照算


def test_list_device_names_default_hostapi_only_without_mapper_and_duplicates():
    from xiaoshen.audio_io import list_device_names

    devices = [{**d, "hostapi": 0} for d in DEVICES] + [
        {"name": "麥克風 (USBMIC1.)", "hostapi": 1, "max_input_channels": 1, "max_output_channels": 0},
        {"name": "麥克風排列 (Realtek(R) Audio)", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Microsoft Sound Mapper - Output", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "麥克風 (USBMIC1.)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
    ]
    assert list_device_names("input", devices, hostapi=0) == ["麥克風 (USBMIC1.)"]
    assert list_device_names("output", devices, hostapi=0) == ["喇叭 (USBMIC1.)", "Realtek Digital Output"]
    assert list_device_names("input", devices, hostapi=1) == ["麥克風 (USBMIC1.)", "麥克風排列 (Realtek(R) Audio)"]

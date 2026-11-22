"""轉寫工作站的檔案狀態機（假模型）。"""

import json

from xiaoshen import meeting_store as ms
from xiaoshen import worker_queue as wq

MID = "2026-11-22_0730"


def put(config, mid=MID, audio=b"fLaC" * 10, ready=True):
    p = ms.paths(config, mid)
    p.audio.parent.mkdir(parents=True, exist_ok=True)
    if audio is not None:
        p.audio.write_bytes(audio)
    if ready:
        p.ready.write_text("x")
    return p


class FakeEngines:
    models = {"asr": "fake-whisper", "diarization": "fake-diar", "names": "fake-ner"}

    def __init__(self, config, fail=None):
        self.config = config
        self.fail = fail
        self.seen_working = None

    def transcribe(self, audio):
        if self.fail == "asr":
            raise RuntimeError("音檔毀損")
        self.seen_working = ms.paths(self.config, audio.stem).working.exists()
        return [{"start": 0.0, "end": 40.0, "text": "我們來看王小明 病歷號 1234567"},
                {"start": 40.0, "end": 50.0, "text": "好"}]

    def diarize(self, audio):
        return [{"start": 0.0, "end": 40.0, "speaker": 3}, {"start": 40.0, "end": 50.0, "speaker": 7}], \
            {3: [1.0, 0.0], 7: [0.0, 1.0]}

    def person_names(self, text):
        return ["王小明"] if "王小明" in text else []


def test_pending_requires_ready_and_stable_size(config):
    seen = {}
    p = put(config, ready=False)
    assert wq.pending(config, seen) == []          # 沒有 .ready：還在同步
    p.ready.write_text("x")
    assert wq.pending(config, seen) == []          # 第一次看到：先記大小
    p.audio.write_bytes(b"fLaC" * 20)              # 雲端硬碟還在寫
    assert wq.pending(config, seen) == []
    assert wq.pending(config, seen) == [MID]       # 大小兩輪一樣：穩定


def test_pending_skips_ready_without_audio(config):
    seen = {}
    put(config, audio=None)                         # .ready 比音檔早到
    assert wq.pending(config, seen) == [] and wq.pending(config, seen) == []


def test_pending_skips_done_and_error(config):
    seen = {}
    put(config, "2026-11-22_0730")
    put(config, "2026-11-22_0731")
    put(config, "2026-11-22_0732")
    p1 = ms.paths(config, "2026-11-22_0730")
    p1.transcript_json.parent.mkdir(parents=True, exist_ok=True)
    p1.transcript_json.write_text("{}")
    ms.paths(config, "2026-11-22_0731").error.write_text("{}")
    wq.pending(config, seen)
    assert wq.pending(config, seen) == ["2026-11-22_0732"]


def test_process_writes_json_and_txt(config):
    p = put(config)
    eng = FakeEngines(config)
    out = wq.process(config, MID, eng)
    assert out == p.transcript_json
    doc = json.loads(p.transcript_json.read_text(encoding="utf-8"))
    assert doc["id"] == MID and len(doc["lines"]) == 2
    assert {c["text"] for c in doc["candidates"]} >= {"王小明", "病歷號 1234567"}
    assert doc["models"]["asr"] == "fake-whisper"
    assert set(doc["timing"]) >= {"asr", "diarization", "names", "total"}
    assert p.transcript_txt.read_text(encoding="utf-8").startswith("[00:00] 講者1：")
    assert eng.seen_working is True and not p.working.exists()
    assert not p.error.exists()


def test_process_writes_error_json_on_failure(config):
    p = put(config)
    out = wq.process(config, MID, FakeEngines(config, fail="asr"))
    assert out == p.error
    err = json.loads(p.error.read_text(encoding="utf-8"))
    assert err["error"] == "音檔毀損" and "RuntimeError" in err["detail"]
    assert not p.transcript_json.exists() and not p.working.exists()


def test_process_keeps_working_marker_fresh(config):
    """轉寫中定期更新 .working（心跳），筆電才分得出「還在轉」和「轉到一半當掉」。"""
    import os
    import time

    p = put(config)

    class Slow(FakeEngines):
        def transcribe(self, audio):
            old = time.time() - 3600
            os.utime(p.working, (old, old))
            time.sleep(0.3)
            return super().transcribe(audio)

        def diarize(self, audio):
            self.age = time.time() - p.working.stat().st_mtime
            return super().diarize(audio)

    eng = Slow(config)
    wq.process(config, MID, eng, heartbeat=0.05)
    assert eng.age < 60


def test_pending_requires_size_in_ready(config):
    """審查：.ready 先同步到、音檔只到一半（大小也停住了）→ 不處理。"""
    import json

    seen = {}
    p = put(config, ready=False)
    p.ready.write_text(json.dumps({"size": 999}), encoding="utf-8")
    assert wq.pending(config, seen) == [] and wq.pending(config, seen) == []
    p.ready.write_text(json.dumps({"size": p.audio.stat().st_size}), encoding="utf-8")
    assert wq.pending(config, seen) == [MID]


def test_only_one_worker_per_computer(tmp_path):
    """實測（2026-11-22）：舊的工作站沒關，新舊兩個同時跑，舊程式搶先轉了錄音。第二個要拿不到鎖。"""
    lock = tmp_path / "worker.lock"
    first = wq.single_instance(lock)
    assert first is not None
    assert wq.single_instance(lock) is None  # 已經有一個在跑
    first.close()  # 第一個結束（或當掉，作業系統會放掉鎖）
    again = wq.single_instance(lock)
    assert again is not None
    again.close()

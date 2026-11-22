"""筆電退路：家裡電腦超過時限沒轉好時，主持人按了才在這台電腦轉寫（不自動跑）。"""

import json
import os
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from xiaoshen import local_transcribe as lt
from xiaoshen import meeting_store as ms
from xiaoshen.hub import Hub
from xiaoshen.server import create_control_app

MID = "2026-11-22_0730"


def put_audio(config, minutes_ago=40):
    p = ms.paths(config, MID)
    p.audio.parent.mkdir(parents=True, exist_ok=True)
    p.audio.write_bytes(b"x")
    p.ready.write_text("x")
    t = (datetime.now() - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(p.ready, (t, t))
    return p


class FakeEngines:
    models = {"asr": "fake-turbo"}

    def transcribe(self, audio):
        time.sleep(0.05)
        return [{"start": 0.0, "end": 5.0, "text": "退路轉寫"}]

    def diarize(self, audio):
        return [{"start": 0.0, "end": 5.0, "speaker": 0}], {0: [1.0]}

    def person_names(self, text):
        return []


class FakeLocal:
    def __init__(self, ready=True):
        self._ready = ready
        self.downloads = 0

    def ready(self):
        return self._ready

    def make_engines(self):
        return FakeEngines()

    def download(self, progress):
        self.downloads += 1
        progress(1.0)
        self._ready = True


def client(state, local):
    return TestClient(create_control_app(state, Hub(), local_transcriber=local))


def test_overdue_offers_local(state):
    put_audio(state.config, minutes_ago=40)
    with client(state, FakeLocal()) as c:
        row = c.get("/api/meetings").json()[0]
    assert row["status"] == "overdue" and row["can_transcribe_local"] is True


def test_waiting_does_not_offer_local(state):
    put_audio(state.config, minutes_ago=5)
    with client(state, FakeLocal()) as c:
        row = c.get("/api/meetings").json()[0]
    assert row["status"] == "waiting" and row["can_transcribe_local"] is False


def test_local_requires_models(state):
    put_audio(state.config)
    local = FakeLocal(ready=False)
    with client(state, local) as c:
        r = c.post(f"/api/meetings/{MID}/transcribe-local")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["need_download"] is True and "600 MB" in detail["message"]
        assert local.downloads == 0  # 主持人按確認才下載
        assert c.post("/api/local-models/download").json()["status"] in ("running", "done")
        for _ in range(100):
            if c.get("/api/local-models").json()["status"] == "done":
                break
            time.sleep(0.02)
        assert c.get("/api/local-models").json() == {"status": "done", "ready": True, "progress": 1.0}
    assert local.downloads == 1


def test_local_runs_process_in_background(state):
    p = put_audio(state.config)
    p.error.parent.mkdir(parents=True, exist_ok=True)
    p.error.write_text(json.dumps({"error": "舊的失敗"}), encoding="utf-8")  # 失敗的也可以在這台重轉
    with client(state, FakeLocal()) as c:
        r = c.post(f"/api/meetings/{MID}/transcribe-local")
        assert r.status_code == 200 and r.json() == {"status": "running"}
        for _ in range(200):
            if p.transcript_json.exists():
                break
            time.sleep(0.02)
    doc = json.loads(p.transcript_json.read_text(encoding="utf-8"))
    assert doc["lines"][0]["text"] == "退路轉寫" and doc["models"]["asr"] == "fake-turbo"
    assert not p.error.exists()


def test_local_refuses_when_already_transcribed(state):
    p = put_audio(state.config)
    p.transcript_json.parent.mkdir(parents=True, exist_ok=True)
    p.transcript_json.write_text("{}")
    with client(state, FakeLocal()) as c:
        assert c.post(f"/api/meetings/{MID}/transcribe-local").status_code == 409


def test_models_ready_checks_files(tmp_path):
    assert lt.LocalTranscriber(tmp_path).ready() is False
    for rel in lt.REQUIRED_FILES:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    assert lt.LocalTranscriber(tmp_path).ready() is True


def test_chunks_split_long_segments():
    diar = [{"start": 0.0, "end": 60.0, "speaker": 0}, {"start": 60.0, "end": 60.3, "speaker": 1}]
    chunks = lt.chunks(diar, max_seconds=25)
    assert chunks == [(0.0, 25.0), (25.0, 50.0), (50.0, 60.0)]  # 太短（<0.5 秒）的略過

"""錄音接上麥克風、API、快照、結束程式與啟動收尾。"""

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from xiaoshen import meeting_store as ms
from xiaoshen.config import REPO_ROOT
from xiaoshen.hub import Hub
from xiaoshen.mic_tee import TeeMic, recorder_pump
from xiaoshen.recording import Recorder
from xiaoshen.server import create_control_app

BLOCK = b"\x01\x00" * 1600  # 100 ms


def make_recorder(config, tmp_path):
    return Recorder(config, now=lambda: datetime(2026, 11, 22, 7, 30), temp_dir=tmp_path / "rec")


class FakeMic:
    def __init__(self, blocks=None):
        self.blocks = blocks
        self.closed = False
        self.sent = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self.blocks is not None and self.sent >= self.blocks:
            raise StopAsyncIteration
        self.sent += 1
        return BLOCK

    def close(self):
        self.closed = True


def test_tee_mic_feeds_recorder_and_passes_blocks(config, tmp_path):
    rec = make_recorder(config, tmp_path)
    rec.start()

    async def run():
        return [b async for b in TeeMic(FakeMic(5), rec)]

    out = asyncio.run(run())
    assert out == [BLOCK] * 5
    path = rec.stop()
    assert path.exists()
    import soundfile as sf
    assert len(sf.read(path)[0]) == 5 * 1600


def test_pump_used_when_voice_disabled(config, tmp_path):
    """語音停用（啟動檢查失敗）時按「開始錄音」：錄音器自己開麥克風。"""
    rec = make_recorder(config, tmp_path)
    mics = []

    def factory():
        mics.append(FakeMic())
        return mics[-1]

    async def run():
        task = asyncio.create_task(recorder_pump(rec, factory, lambda: False, idle=0.01))
        await asyncio.sleep(0.05)
        assert mics == []  # 沒在錄不開麥克風
        rec.start()
        for _ in range(200):
            await asyncio.sleep(0.005)
            if mics and mics[0].sent >= 10:
                break
        path = await asyncio.to_thread(rec.stop)
        await asyncio.sleep(0.05)
        task.cancel()
        return path

    path = asyncio.run(run())
    assert len(mics) == 1 and mics[0].closed
    assert path is not None and path.exists()


def test_pump_idle_when_voice_running(config, tmp_path):
    rec = make_recorder(config, tmp_path)
    mics = []

    async def run():
        task = asyncio.create_task(recorder_pump(rec, lambda: mics.append(FakeMic()) or mics[-1], lambda: True,
                                                 idle=0.01))
        rec.start()
        await asyncio.sleep(0.05)
        task.cancel()
        rec.stop()

    asyncio.run(run())
    assert mics == []  # 語音在跑：TeeMic 已經在餵，不另開麥克風


def test_snapshot_shows_recording(state, tmp_path):
    assert state.snapshot("display")["recording"] == {"active": False, "elapsed": 0, "id": None}
    host = state.snapshot("host")["recording"]
    assert host["active"] is False and host["available"] is False
    state.recorder = make_recorder(state.config, tmp_path)
    state.recorder.start()
    snap = state.snapshot("display")["recording"]
    assert snap["active"] is True and snap["id"] == "2026-11-22_0730"
    state.recorder.stop()


def test_api_start_stop(state, tmp_path):
    state.recorder = make_recorder(state.config, tmp_path)
    with TestClient(create_control_app(state, Hub())) as c:
        r = c.post("/api/recording/start")
        assert r.status_code == 200 and r.json()["id"] == "2026-11-22_0730"
        state.recorder.feed(BLOCK)
        r = c.post("/api/recording/stop")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "2026-11-22_0730"
        assert data["path"] == str(ms.paths(state.config, "2026-11-22_0730").audio)


def test_api_without_recorder_409(state):
    with TestClient(create_control_app(state, Hub())) as c:
        assert c.post("/api/recording/start").status_code == 409


def test_quit_stops_recording(state, tmp_path):
    from xiaoshen.launcher import finish_recording

    state.recorder = make_recorder(state.config, tmp_path)
    state.recorder.start()
    state.recorder.feed(BLOCK)
    asyncio.run(finish_recording(state))
    assert not state.recorder.is_recording
    assert ms.paths(state.config, "2026-11-22_0730").ready.exists()


def test_startup_recovers_unfinished(state, tmp_path):
    from xiaoshen.launcher import setup_recorder

    tmp = tmp_path / "rec"
    tmp.mkdir()
    (tmp / "2026-11-21_0730.part.pcm").write_bytes(BLOCK * 10)
    rec = setup_recorder(state.config, state, temp_dir=tmp)
    assert state.recorder is rec
    assert ms.paths(state.config, "2026-11-21_0730").ready.exists()
    assert any("2026-11-21_0730" in w for w in state.warnings)


def test_pages_have_record_controls():
    host = (REPO_ROOT / "web" / "host.html").read_text(encoding="utf-8")
    display = (REPO_ROOT / "web" / "display.html").read_text(encoding="utf-8")
    assert 'id="recordToggle"' in host
    header = host[host.index("<header>"):host.index("</header>")]
    assert 'id="recordToggle"' in header and 'id="recordMsg"' in header  # 使用者要求：放在最頂端那一列
    assert 'id="recBadge"' in display and "● 錄音中" in display


def test_phone_questions_kept_only_while_recording(state, tmp_path, monkeypatch):
    """使用者（2026-11-23）：只收錄音期間（開始～停止）的「詢問老師」提問，記下是錄音的第幾秒。"""
    private = tmp_path / "private"
    monkeypatch.setattr(ms, "private_dir", lambda: private)
    clock = [100.0]
    state.recorder = Recorder(state.config, now=lambda: datetime(2026, 11, 22, 7, 30), clock=lambda: clock[0],
                              temp_dir=tmp_path / "rec")
    state.add_teacher_question("錄音前的問題")
    mid = state.recorder.start()
    clock[0] = 165.0
    state.add_teacher_question("SRNS 第二線用什麼？")
    state.recorder.feed(BLOCK)
    state.recorder.stop()
    state.add_teacher_question("錄音後的問題")
    assert ms.load_questions(private, mid) == [{"t": 65.0, "text": "SRNS 第二線用什麼？"}]
    assert len(state.teacher_questions) == 3  # 主持人畫面照常列出全部


def test_api_level(state, tmp_path):
    state.recorder = make_recorder(state.config, tmp_path)
    with TestClient(create_control_app(state, Hub())) as c:
        assert c.get("/api/recording/level").json() == {"active": False, "level": 0.0}
        c.post("/api/recording/start")
        state.recorder.feed(BLOCK)
        r = c.get("/api/recording/level").json()
        assert r["active"] is True and 0.0 <= r["level"] <= 1.0
        c.post("/api/recording/stop")

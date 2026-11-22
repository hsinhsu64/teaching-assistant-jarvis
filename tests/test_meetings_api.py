"""主持人畫面的會議記錄區：場次清單、確認畫面資料、生成、刪除。"""

import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fakes import FakeLLM
from xiaoshen import meeting_store as ms
from xiaoshen.config import REPO_ROOT
from xiaoshen.hub import Hub
from xiaoshen.server import create_control_app

MID = "2026-11-22_0730"


def put_transcript(config, mid=MID, candidates=None):
    p = ms.paths(config, mid)
    p.audio.parent.mkdir(parents=True, exist_ok=True)
    p.audio.write_bytes(b"x")
    p.ready.write_text("x")
    doc = {"version": 1, "id": mid,
           "lines": [{"start": 0.0, "end": 30.0, "text": "看王小明，病歷號 1234567", "speaker": 0},
                     {"start": 30.0, "end": 40.0, "text": "周醫師說的對", "speaker": 1}],
           "speakers": [{"speaker": 0, "seconds": 30.0, "samples": ["看王小明"]},
                        {"speaker": 1, "seconds": 10.0, "samples": ["周醫師說的對"]}],
           "candidates": candidates if candidates is not None else [
               {"text": "病歷號 1234567", "kind": "chart", "count": 1, "lines": [0]},
               {"text": "王小明", "kind": "person", "count": 1, "lines": [0]},
               {"text": "周醫師", "kind": "person", "count": 1, "lines": [1]}],
           "models": {}, "timing": {}}
    p.transcript_json.parent.mkdir(parents=True, exist_ok=True)
    p.transcript_json.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def private(tmp_path, monkeypatch):
    d = tmp_path / "private"
    monkeypatch.setattr(ms, "private_dir", lambda: d)
    monkeypatch.setattr(ms, "_default_private_dir", lambda: d)
    return d


def client(state, llm=None):
    return TestClient(create_control_app(state, Hub(), minutes_llm=llm))


def wait_job(c, mid=MID):
    for _ in range(200):
        r = c.get(f"/api/meetings/{mid}/minutes").json()
        if r["status"] != "running":
            return r
        time.sleep(0.02)
    raise AssertionError("背景工作沒有結束")


def test_list_meetings_api(state, private):
    put_transcript(state.config)
    with client(state) as c:
        rows = c.get("/api/meetings").json()
    assert rows[0]["id"] == MID and rows[0]["status"] == "transcribed"


def test_list_shows_error_reason(state, private):
    p = ms.paths(state.config, "2026-11-21_0730")
    p.audio.parent.mkdir(parents=True, exist_ok=True)
    p.ready.write_text("x")
    p.error.parent.mkdir(parents=True, exist_ok=True)
    p.error.write_text(json.dumps({"error": "音檔毀損", "detail": "..."}), encoding="utf-8")
    with client(state) as c:
        row = c.get("/api/meetings").json()[0]
    assert row["status"] == "error" and row["error"] == "音檔毀損"


def test_review_unchecks_known_names(state, private):
    put_transcript(state.config)
    private.mkdir(parents=True)
    (private / "known_names.json").write_text(json.dumps(["周醫師", "林醫師"], ensure_ascii=False), encoding="utf-8")
    with client(state) as c:
        data = c.get(f"/api/meetings/{MID}/review").json()
    assert data["known_names"] == ["周醫師", "林醫師"]
    checked = {x["text"]: x["checked"] for x in data["candidates"]}
    assert checked == {"病歷號 1234567": True, "王小明": True, "周醫師": False}
    assert [s["speaker"] for s in data["speakers"]] == [0, 1]
    assert data["meeting_type"] == "病例討論"


def test_review_404_without_transcript(state, private):
    with client(state) as c:
        assert c.get(f"/api/meetings/{MID}/review").status_code == 404
        assert c.get("/api/meetings/not-an-id/review").status_code == 404


def test_generate_flow_ok(state, private):
    put_transcript(state.config)
    llm = FakeLLM({"minutes_md": "## 一、今天的主題與簡報\n病人A", "corrections": []})
    body = {"speaker_names": {"0": "周醫師", "1": "林醫師"},
            "items": [{"text": "王小明", "kind": "person"}, {"text": "病歷號 1234567", "kind": "chart"}],
            "meeting_type": "病例討論"}
    with client(state, llm) as c:
        r = c.post(f"/api/meetings/{MID}/minutes", json=body)
        assert r.status_code == 200
        res = wait_job(c)
        assert res["status"] == "ok" and res["md"].endswith(f"{MID}.md")
        assert c.get("/api/meetings").json()[0]["status"] == "minutes_done"
    assert "王小明" in ms.paths(state.config, MID).minutes_md.read_text(encoding="utf-8")


def test_generate_blocked_shows_hits(state, private):
    put_transcript(state.config)
    llm = FakeLLM({"minutes_md": "x", "corrections": []})
    body = {"speaker_names": {}, "items": [{"text": "王小明", "kind": "person"}], "meeting_type": "病例討論"}
    with client(state, llm) as c:
        c.post(f"/api/meetings/{MID}/minutes", json=body)
        res = wait_job(c)
    assert res["status"] == "blocked" and res["hits"][0]["text"] == "病歷號 1234567"
    assert llm.calls == []


def test_generate_failure_reported(state, private):
    put_transcript(state.config)
    body = {"speaker_names": {}, "items": [{"text": "病歷號 1234567", "kind": "chart"}], "meeting_type": "x"}
    with client(state, FakeLLM(exc=RuntimeError("配額用完"))) as c:
        c.post(f"/api/meetings/{MID}/minutes", json=body)
        res = wait_job(c)
    assert res["status"] == "failed" and "配額用完" in res["error"]


def test_known_names_saved(state, private):
    put_transcript(state.config)
    private.mkdir(parents=True)
    (private / "known_names.json").write_text(json.dumps(["林醫師"], ensure_ascii=False), encoding="utf-8")
    llm = FakeLLM({"minutes_md": "x", "corrections": []})
    body = {"speaker_names": {"0": " 周醫師 ", "1": "講者2"},
            "items": [{"text": "王小明", "kind": "person"}, {"text": "病歷號 1234567", "kind": "chart"}],
            "meeting_type": "病例討論"}
    with client(state, llm) as c:
        c.post(f"/api/meetings/{MID}/minutes", json=body)
        wait_job(c)
    names = json.loads((private / "known_names.json").read_text(encoding="utf-8"))
    assert names == ["周醫師", "林醫師"]  # 新填的排前面；「講者N」不記


def test_delete_only_without_minutes(state, private):
    p = put_transcript(state.config)
    with client(state) as c:
        p.minutes_docx.parent.mkdir(parents=True, exist_ok=True)
        p.minutes_docx.write_bytes(b"x")
        assert c.delete(f"/api/meetings/{MID}").status_code == 409
        p.minutes_docx.unlink()
        r = c.delete(f"/api/meetings/{MID}")
        assert r.status_code == 200 and f"{MID}.flac" in r.json()["removed"]
    assert not p.audio.exists() and not p.transcript_json.exists() and not p.ready.exists()


def test_delete_refuses_recording_in_progress(state, private, tmp_path):
    from xiaoshen.recording import Recorder
    from datetime import datetime

    state.recorder = Recorder(state.config, now=lambda: datetime(2026, 11, 22, 7, 30), temp_dir=tmp_path / "rec")
    state.recorder.start()
    with client(state) as c:
        assert c.delete(f"/api/meetings/{MID}").status_code == 409
    state.recorder.stop()


def test_host_has_meetings_section():
    html = (REPO_ROOT / "web" / "host.html").read_text(encoding="utf-8")
    assert 'id="secMeetings"' in html


def test_second_send_while_running_is_refused(state, private):
    """使用者：整理中再按一次「送出整理」不可以送出第二次。"""
    import threading

    gate = threading.Event()

    class SlowLLM:
        calls = 0

        def generate_json(self, *a, **k):
            SlowLLM.calls += 1
            gate.wait(5)
            return {"minutes_md": "x", "corrections": []}

    put_transcript(state.config)
    body = {"speaker_names": {}, "items": [{"text": "王小明", "kind": "person"},
                                           {"text": "病歷號 1234567", "kind": "chart"}], "meeting_type": "x"}
    with client(state, SlowLLM()) as c:
        assert c.post(f"/api/meetings/{MID}/minutes", json=body).status_code == 200
        second = c.post(f"/api/meetings/{MID}/minutes", json=body)
        gate.set()
        wait_job(c)
    assert second.status_code == 409 and SlowLLM.calls == 1


def test_view_minutes(state, private):
    """使用者：會議記錄生成後，主持人按「檢視記錄」直接看。"""
    p = put_transcript(state.config)
    with client(state) as c:
        assert c.get(f"/api/meetings/{MID}/minutes-view").status_code == 404  # 還沒生成
        p.minutes_md.parent.mkdir(parents=True, exist_ok=True)
        p.minutes_md.write_text("# 晨會會議記錄\n\n## 一、今天的主題與簡報\n- 王小明 水腫\n", encoding="utf-8")
        r = c.get(f"/api/meetings/{MID}/minutes-view")
        assert c.get("/api/meetings/bad-id/minutes-view").status_code == 404
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == MID and "<li>王小明 水腫</li>" in body["html"] and body["docx"].endswith(f"{MID}.docx")


def test_meeting_status_for_phone_has_no_patient_data(state, private):
    """主持人手機用的清單：只有日期、進度、可否在筆電轉寫；不給檔案路徑、擋下的病人資料片段。"""
    put_transcript(state.config)
    body = {"speaker_names": {}, "items": [{"text": "王小明", "kind": "person"}], "meeting_type": "x"}
    with client(state, FakeLLM({"minutes_md": "x", "corrections": []})) as c:
        c.post(f"/api/meetings/{MID}/minutes", json=body)
        wait_job(c)  # 病歷號沒勾：被擋下，minutes_job 內有病人資料片段
        rows = c.get("/api/meeting-status").json()
    assert rows == [{"id": MID, "status": "transcribed", "has_minutes": False, "fallback_minutes": 30,
                     "can_transcribe_local": False, "job": "blocked"}]


def test_review_lists_phone_questions_and_their_phi(state, private):
    """確認畫面列出錄音期間的手機提問（預設勾選），提問裡的病人資料也列進去識別候選。"""
    put_transcript(state.config)
    ms.add_question(private, MID, 12.0, "病歷號 7654321 那位要不要換藥？")
    with client(state) as c:
        r = c.get(f"/api/meetings/{MID}/review").json()
    assert r["questions"] == [{"i": 0, "t": 12.0, "text": "病歷號 7654321 那位要不要換藥？"}]
    cand = next(x for x in r["candidates"] if x["text"] == "病歷號 7654321")
    assert cand["kind"] == "chart" and cand["checked"] and cand["count"] == 1


def test_selected_phone_questions_sent_for_minutes(state, private):
    put_transcript(state.config)
    ms.add_question(private, MID, 12.0, "第一題要列入")
    ms.add_question(private, MID, 20.0, "第二題不要")
    llm = FakeLLM({"minutes_md": "x", "corrections": []})
    body = {"speaker_names": {}, "items": [{"text": "王小明", "kind": "person"},
                                           {"text": "病歷號 1234567", "kind": "chart"}],
            "meeting_type": "x", "questions": [0]}
    with client(state, llm) as c:
        c.post(f"/api/meetings/{MID}/minutes", json=body)
        assert wait_job(c)["status"] == "ok"
    assert "第一題要列入" in llm.calls[0]["prompt"] and "第二題不要" not in llm.calls[0]["prompt"]


def test_transcript_view_and_edit(state, private):
    """使用者（2026-11-23）：主持人可以看並修改逐字稿（文字與講者）；.json 與 .txt 一起更新。"""
    p = put_transcript(state.config)
    with client(state) as c:
        r = c.get(f"/api/meetings/{MID}/transcript").json()
        assert [l["i"] for l in r["lines"]] == [0, 1] and r["lines"][0]["text"] == "看王小明，病歷號 1234567"
        ok = c.post(f"/api/meetings/{MID}/transcript",
                    json={"lines": [{"i": 0, "text": "看王小明，病歷號 7654321"}, {"i": 1, "speaker": 0}]})
        assert ok.status_code == 200
        bad = c.post(f"/api/meetings/{MID}/transcript", json={"lines": [{"i": 9, "text": "x"}]})
        assert bad.status_code == 400
        assert c.get("/api/meetings/2026-11-21_0730/transcript").status_code == 404
    doc = json.loads(p.transcript_json.read_text(encoding="utf-8"))
    assert doc["lines"][0]["text"] == "看王小明，病歷號 7654321" and doc["lines"][1]["speaker"] == 0
    assert doc.get("edited_at")
    assert "病歷號 7654321" in {x["text"] for x in doc["candidates"]}
    assert "講者1：周醫師說的對" in p.transcript_txt.read_text(encoding="utf-8")


def test_minutes_edit_rewrites_md_and_docx(state, private):
    """使用者（2026-11-23）：會議記錄在畫面上改，以畫面為準重寫 .md 與 Word。"""
    from docx import Document

    p = put_transcript(state.config)
    with client(state) as c:
        assert c.post(f"/api/meetings/{MID}/minutes-md", json={"md": "x"}).status_code == 404  # 還沒生成
        p.minutes_md.parent.mkdir(parents=True, exist_ok=True)
        p.minutes_md.write_text("# 舊的\n", encoding="utf-8")
        r = c.post(f"/api/meetings/{MID}/minutes-md", json={"md": "# 晨會會議記錄\n\n## 一、今天的主題與簡報\n- 改過的內容\n"})
        assert r.status_code == 200
        assert "改過的內容" in c.get(f"/api/meetings/{MID}/minutes-view").json()["html"]
        assert c.get(f"/api/meetings/{MID}/minutes-view").json()["md"].startswith("# 晨會會議記錄")
    assert "改過的內容" in p.minutes_md.read_text(encoding="utf-8")
    assert any("改過的內容" in par.text for par in Document(str(p.minutes_docx)).paragraphs)


def test_delete_minutes_only(state, private):
    """使用者（2026-11-23）：有會議記錄的場次也能刪除——只刪會議記錄，錄音與逐字稿留著可以重新生成。"""
    p = put_transcript(state.config)
    p.minutes_md.parent.mkdir(parents=True, exist_ok=True)
    for f in (p.minutes_md, p.minutes_docx):
        f.write_text("x", encoding="utf-8")
    ms.mark_minutes_generated(state.config, MID, datetime(2026, 11, 22, 9, 0))
    with client(state) as c:
        assert c.get("/api/meetings").json()[0]["status"] == "minutes_done"
        assert c.delete(f"/api/meetings/{MID}/minutes").status_code == 200
        assert c.get("/api/meetings").json()[0]["status"] == "transcribed"
        assert c.delete(f"/api/meetings/{MID}/minutes").status_code == 404  # 已經沒有了
    assert not any(f.exists() for f in (p.minutes_md, p.minutes_docx, p.minutes_at))
    assert p.audio.exists() and p.transcript_json.exists()

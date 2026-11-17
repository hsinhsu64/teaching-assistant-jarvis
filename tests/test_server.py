import json

import pytest
from fastapi.testclient import TestClient

from xiaoshen.hub import Hub
from xiaoshen.server import create_control_app, create_phone_app


@pytest.fixture
def control_client(state):
    with TestClient(create_control_app(state, Hub())) as c:
        yield c


@pytest.fixture
def phone_client(state):
    with TestClient(create_phone_app(state)) as c:
        yield c


def test_phone_app_has_no_control_routes(phone_client):
    for path in ["/host", "/display", "/api/nav", "/api/quiz/reveal", "/api/state", "/slides/0.png", "/qr.png"]:
        assert phone_client.get(path).status_code in (404, 405), path
        assert phone_client.post(path).status_code in (404, 405), path


def test_phone_healthz(phone_client):
    assert phone_client.get("/healthz").json() == {"ok": True}


def test_phone_bad_token_403(phone_client, state):
    assert phone_client.get("/q?t=wrong").status_code == 403
    assert phone_client.get("/api/phone/state?t=wrong&cid=a").status_code == 403
    r = phone_client.post("/api/phone/submit", json={"t": "wrong", "cid": "a", "answers": {}, "final": True})
    assert r.status_code == 403


def test_phone_state_without_run(phone_client, state):
    assert phone_client.get(f"/api/phone/state?t={state.token}&cid=a").json() == {"run": None}


def test_phone_submit_flow(phone_client, control_client, state):
    control_client.post("/api/nav", json={"action": "next"})
    r = phone_client.post("/api/phone/submit", json={"t": state.token, "cid": "a", "answers": {"D-p2": 1}, "final": True})
    assert r.status_code == 200
    v = phone_client.get(f"/api/phone/state?t={state.token}&cid=a").json()
    assert v["run"]["questions"][0]["mine"] == 1 and "answer" not in v["run"]["questions"][0]


def test_phone_submit_invalid_422(phone_client, control_client, state):
    control_client.post("/api/nav", json={"action": "next"})
    r = phone_client.post("/api/phone/submit", json={"t": state.token, "cid": "a", "answers": {"D-p2": 9}, "final": True})
    assert r.status_code == 422


def test_phone_submit_after_close_409(phone_client, control_client, state):
    control_client.post("/api/nav", json={"action": "next"})
    control_client.post("/api/quiz/reveal")
    r = phone_client.post("/api/phone/submit", json={"t": state.token, "cid": "a", "answers": {"D-p2": 0}, "final": True})
    assert r.status_code == 409


def test_phone_submit_without_run_409(phone_client, state):
    r = phone_client.post("/api/phone/submit", json={"t": state.token, "cid": "a", "answers": {}, "final": True})
    assert r.status_code == 409


def test_nav_and_state(control_client):
    control_client.post("/api/nav", json={"action": "goto", "page": 2})
    assert control_client.get("/api/state?audience=display").json()["page"] == 2


def test_display_ws_payload_has_no_answer(control_client):
    control_client.post("/api/nav", json={"action": "next"})
    with control_client.websocket_connect("/ws?audience=display") as ws:
        snap = ws.receive_json()
    assert snap["run"] is not None
    blob = json.dumps(snap, ensure_ascii=False)
    assert '"answer"' not in blob and '"explanation"' not in blob


def test_ws_receives_broadcast_after_change(control_client):
    with control_client.websocket_connect("/ws?audience=host") as ws:
        ws.receive_json()
        control_client.post("/api/nav", json={"action": "next"})
        snap = ws.receive_json()
    assert snap["page"] == 1 and snap["run"]["questions"][0]["answer"] == 1


def test_slides_png(control_client):
    r = control_client.get("/slides/0.png?w=400")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert control_client.get("/slides/9.png").status_code == 404


def test_qr_requires_public_base(control_client, state):
    assert control_client.get("/qr.png").status_code == 404
    state.public_base = "https://x.trycloudflare.com"
    assert control_client.get("/qr.png").content[:4] == b"\x89PNG"


def test_dispatch_endpoint(control_client, state):
    assert control_client.post("/api/quiz/dispatch").status_code == 404
    control_client.post("/api/nav", json={"action": "next"})
    assert control_client.post("/api/quiz/dispatch").status_code == 409
    control_client.post("/api/quiz/reveal")
    assert control_client.post("/api/quiz/dispatch").status_code == 200


def test_collect_and_hide(control_client, state):
    control_client.post("/api/nav", json={"action": "next"})
    control_client.post("/api/quiz/collect")
    assert state.drawer == "results"
    control_client.post("/api/drawer/hide")
    assert state.drawer == "hidden"


def test_decks_listing_and_switch(control_client, state, make_pdf, config):
    other = make_pdf(["x" * 40], folder=config.folder("簡報"), name="E.pdf")
    names = [d["name"] for d in control_client.get("/api/decks").json()]
    assert set(names) == {"D", "E"}
    control_client.post("/api/present/stop")  # 播放中不能換簡報（§6.0）
    assert control_client.post("/api/deck", json={"path": str(other)}).status_code == 200
    assert state.deck.name == "E"


def test_deck_path_outside_folder_rejected(control_client, tmp_path):
    assert control_client.post("/api/deck", json={"path": str(tmp_path / "x.pdf")}).status_code == 400


def test_phone_static_only_serves_phone_assets(phone_client):
    assert phone_client.get("/static/host.html").status_code == 404
    assert phone_client.get("/static/host.js").status_code == 404
    assert phone_client.get("/static/display.js").status_code == 404


# ---- Phase 2 ----

def test_phone_app_still_has_no_new_routes(phone_client):
    for path in ["/api/ask", "/api/quiz/generate", "/api/drafts/publish", "/api/drafts/remove",
                 "/api/drafts/regenerate", "/api/answer/quotes", "/api/answer/show",
                 "/api/references", "/api/references/upload"]:
        assert phone_client.get(path).status_code in (404, 405), path
        assert phone_client.post(path).status_code in (404, 405), path


@pytest.fixture
def ai_client(state):
    from fakes import FakeLLM
    from xiaoshen.jobs import JobRunner
    from xiaoshen.library import Library

    quote = "Case 2 Case 2 Case 2 Case 2"
    llm = FakeLLM({"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "p", "quote": quote, "source_label": "今天的簡報", "page": 2}]})
    jobs = JobRunner(state, llm, state.config, Library(state.config.folder("參考資料")))
    with TestClient(create_control_app(state, Hub(), jobs)) as c:
        c.jobs = jobs
        yield c


def test_ask_endpoint(ai_client, state):
    r = ai_client.post("/api/ask", json={"text": "重點？", "route": "deck", "refs": []})
    assert r.status_code == 200 and r.json()["sent"] is True
    ai_client.jobs.wait_idle()
    assert ai_client.get("/api/state?audience=display").json()["ai_answer"]["cards"][0]["point_zh"] == "p"


def test_ask_endpoint_phi(ai_client):
    r = ai_client.post("/api/ask", json={"text": "病歷號 1234567", "route": "deck", "refs": []})
    assert r.json() == {"sent": False, "phi": ["病歷號 1234567", "1234567"]}


def test_ask_rejects_bad_route_and_empty(ai_client):
    assert ai_client.post("/api/ask", json={"text": "q", "route": "web", "refs": []}).status_code == 422
    assert ai_client.post("/api/ask", json={"text": "  ", "route": "deck", "refs": []}).status_code == 422


def test_quiz_generate_bad_n(ai_client):
    assert ai_client.post("/api/quiz/generate", json={"n": 30, "route": "deck", "refs": [], "focus": ""}).status_code == 422


def test_drafts_publish_without_drafts_404(ai_client):
    assert ai_client.post("/api/drafts/publish").status_code == 404


def test_ai_routes_503_without_jobs(control_client):
    assert control_client.post("/api/ask", json={"text": "q", "route": "deck", "refs": []}).status_code == 503


def test_upload_reference(ai_client, state, tmp_path, make_pdf):
    src = make_pdf(["KDIGO text " * 5], folder=tmp_path / "up", name="KDIGO 2021.pdf")
    r = ai_client.post("/api/references/upload", files={"file": ("KDIGO 2021.pdf", src.read_bytes(), "application/pdf")})
    assert r.status_code == 200 and r.json()["name"] == "KDIGO 2021"
    assert (state.config.folder("參考資料") / "KDIGO 2021.pdf").exists()
    refs = ai_client.get("/api/references").json()
    assert refs == [{"name": "KDIGO 2021", "pages": 1, "scanned": False}]


def test_upload_reference_rejects_other_types_and_paths(ai_client, state):
    r = ai_client.post("/api/references/upload", files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400
    r = ai_client.post("/api/references/upload", files={"file": ("../../x.pdf", b"%PDF", "application/pdf")})
    assert not (state.config.data_root / "x.pdf").exists()


# ---- Phase 3 ----

def test_phone_wake_token_and_cooldown(phone_client, state, monkeypatch):
    import xiaoshen.state as st_mod

    calls = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(st_mod.time, "monotonic", lambda: clock["t"])
    assert phone_client.post("/api/phone/wake", json={"t": "bad"}).status_code == 403
    assert phone_client.post("/api/phone/wake", json={"t": state.token}).status_code == 409  # 語音未啟用
    state.wake_handler = lambda: calls.append(1)
    state.voice_status = "standby"
    assert phone_client.post("/api/phone/wake", json={"t": state.token}).json() == {"ok": True, "ignored": False}
    assert phone_client.post("/api/phone/wake", json={"t": state.token}).json() == {"ok": True, "ignored": True}
    clock["t"] += 11
    state.voice_status = "listening"
    assert phone_client.post("/api/phone/wake", json={"t": state.token}).json()["ignored"] is True
    state.voice_status = "standby"
    assert phone_client.post("/api/phone/wake", json={"t": state.token}).json()["ignored"] is False
    assert len(calls) == 2


def test_phone_app_routes_still_minimal(phone_client):
    paths = sorted(r.path for r in phone_client.app.routes if hasattr(r, "methods"))
    # §6.3a：只加打字發問與翻頁／播放／結束，其餘指令仍不開放
    assert paths == ["/api/phone/ask", "/api/phone/control", "/api/phone/end", "/api/phone/nav", "/api/phone/play",
                     "/api/phone/state", "/api/phone/submit", "/api/phone/teacher", "/api/phone/wake", "/healthz", "/q",
                     "/static/{name}"]


def test_display_snapshot_hides_unfinished_user_line(state):
    # §6.0（2026-11-04）起對話上投影，但「你」還沒說完的那一句先不顯示
    state.add_transcript("你", "病人狀況")
    disp = state.snapshot("display")
    assert disp["voice_transcript"] == [] and "病人狀況" not in str(disp)
    assert disp["voice_status"] == "off" and disp["voice_phi_alert"] is False
    assert state.snapshot("host")["voice_transcript"] == ["你：病人狀況"]


def test_voice_ref_select(control_client, state):
    state.ref_candidates = ["KDIGO 2021 GN", "KDIGO 2025 NS"]
    assert control_client.post("/api/voice/ref", json={"name": "KDIGO 2025 NS"}).json()["voice_ref"] == "KDIGO 2025 NS"
    assert state.ref_candidates == []
    assert control_client.post("/api/voice/ref", json={"name": ""}).json()["voice_ref"] is None


def test_voice_wake_stop_and_phi_clear(control_client, state):
    assert control_client.post("/api/voice/wake").status_code == 409
    calls = []
    state.wake_handler = lambda: calls.append("wake")
    state.stop_handler = lambda: calls.append("stop")
    assert control_client.post("/api/voice/wake").status_code == 200
    assert control_client.post("/api/voice/stop").status_code == 200
    assert calls == ["wake", "stop"]
    state.voice_phi_alert = True
    control_client.post("/api/voice/phi/clear")
    assert state.voice_phi_alert is False


def test_figure_route_and_zoom(control_client, phone_client, state, tmp_path):
    from xiaoshen.answering import Answer, Card, Figure

    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8 jpeg bytes")
    state.answer = Answer("abc", "q", "openevidence", "answered", cards=[Card("p", "q", "[1] X")],
                          figures=[Figure("Figure 1", "cap", str(img), "[1] X")])
    r = control_client.get("/ai/figure/abc/0")
    assert r.status_code == 200 and r.content == b"\xff\xd8 jpeg bytes"
    assert control_client.get("/ai/figure/abc/1").status_code == 404
    assert control_client.get("/ai/figure/old/0").status_code == 404
    assert phone_client.get("/ai/figure/abc/0").status_code == 404
    assert control_client.post("/api/answer/figure", json={"index": 0}).json() == {"figure_zoom": 0}
    assert state.snapshot("display")["figure_zoom"] == 0
    assert control_client.post("/api/answer/figure", json={"index": None}).json() == {"figure_zoom": None}
    assert control_client.post("/api/answer/figure", json={"index": 5}).status_code == 404


def test_answer_card_render_project_save_hide(control_client, phone_client, state):
    from xiaoshen.answering import Answer, Card

    assert control_client.post("/api/answer/card").status_code == 409  # 沒有回答
    state.answer = Answer("ab12", "SRNS 一線？", "deck", "not_found", summary_zh="資料中沒有提到")
    assert control_client.post("/api/answer/card").status_code == 409  # 沒有驗證過的字卡
    state.answer = Answer("ab12", "SRNS 一線？", "deck", "answered",
                          cards=[Card("p", "We recommend calcineurin inhibitors", "今天的簡報 第 2 頁")])
    r = control_client.post("/api/answer/card")
    assert r.status_code == 200
    saved = state.config.folder("紀錄") / "字卡" / r.json()["file"]
    assert saved.exists() and saved.read_bytes()[:4] == b"\x89PNG" and "SRNS" in saved.name
    disp = state.snapshot("display")
    assert disp["card"] == {"answer_id": "ab12", "url": "/ai/card/ab12.png"}
    img = control_client.get("/ai/card/ab12.png")
    assert img.status_code == 200 and img.content == saved.read_bytes()
    assert control_client.get("/ai/card/zz.png").status_code == 404
    assert phone_client.get("/ai/card/ab12.png").status_code == 404
    control_client.post("/api/answer/card/hide")
    assert state.snapshot("display")["card"] is None
    control_client.post("/api/answer/card")
    control_client.post("/api/drawer/hide")
    assert state.card is None  # 收起抽屜一併收回字卡


# ---- 主畫面（§6.0）----

def test_present_button_works_whatever_the_voice_state(control_client, state):
    # 「播放簡報」一直顯示，語音在線也能用（取代「只在語音不在線時」）
    control_client.post("/api/present/stop")
    for st in ("standby", "off", "offline"):
        state.voice_status = st
        assert control_client.post("/api/present").json() == {"presenting": True}
        control_client.post("/api/present/stop")


def test_present_stop_returns_to_hud(control_client, state):
    assert control_client.post("/api/present/stop").json() == {"presenting": False}
    assert control_client.get("/api/state").json()["presenting"] is False


def test_open_deck_rejected_while_presenting(control_client, state):
    r = control_client.post("/api/deck", json={"path": str(state.deck.path)})
    assert r.status_code == 409


def test_decks_listing_refreshes_state(control_client, state):
    control_client.get("/api/decks")
    assert [d["name"] for d in state.snapshot("display")["decks"]] == ["D"]


def test_present_allowed_while_cloud_unreachable(control_client, state):
    # 斷網時語音 30 秒後會回到 standby，但雲端仍連不上 → 主持人仍要能播放（§6.0、§2）
    control_client.post("/api/present/stop")
    state.voice_status = "standby"
    state.voice_cloud_ok = False
    assert control_client.post("/api/present").json() == {"presenting": True}
    assert state.snapshot("host")["voice_down"] is True


def test_ask_accepts_pubmed_route(ai_client):
    r = ai_client.post("/api/ask", json={"text": "SRNS?", "route": "pubmed", "refs": []})
    assert r.status_code == 200
    ai_client.jobs.wait_idle()


# ---- 顯示出處原文（§6.2a）----

def test_source_show_page_and_close(state):
    from xiaoshen.answering import Answer, Card
    from xiaoshen.source_view import SourceViewer

    state.answer = Answer("a", "q", "deck", "answered",
                          cards=[Card("p", "Case 2 Case 2", "今天的簡報 第 2 頁", doc="今天的簡報", page=2)])
    viewer = SourceViewer(state, None, http_get=lambda u: "")
    with TestClient(create_control_app(state, Hub(), None, viewer)) as c:
        r = c.post("/api/source/show", json={"card": 1, "kind": "auto"})
        assert r.status_code == 200 and r.json()["status"] == "ok"
        snap = c.get("/api/state?audience=display").json()
        assert snap["source_view"]["kind"] == "page"
        png = c.get(snap["source_view"]["url"])
        assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
        assert c.post("/api/source/close").json()["status"] == "ok"
        assert c.get("/api/state?audience=display").json()["source_view"] is None
        assert c.get("/source/page.png").status_code == 404


def test_source_show_without_viewer_and_bad_kind(control_client):
    assert control_client.post("/api/source/show", json={"card": 1}).status_code == 503
    assert control_client.post("/api/source/show", json={"card": 1, "kind": "zip"}).status_code == 422


def test_source_show_and_close_are_logged_with_source(state, tmp_path):
    # 實機 2026-11-05：投影停在原文畫面、主持人按「關閉原文」沒反應，記錄裡看不到主持人按了什麼
    import json as _json

    from xiaoshen import eventlog
    from xiaoshen.answering import Answer, Card
    from xiaoshen.source_view import SourceViewer

    state.answer = Answer("a", "q", "deck", "answered",
                          cards=[Card("p", "Case 2 Case 2", "今天的簡報 第 2 頁", doc="今天的簡報", page=2)])
    eventlog.configure(tmp_path)
    try:
        with TestClient(create_control_app(state, Hub(), None, SourceViewer(state, None, http_get=lambda u: ""))) as c:
            c.post("/api/source/show", json={"card": 1, "kind": "page"})
            c.post("/api/source/close")
    finally:
        eventlog.configure(None)
    rows = [_json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    src = [(r["event"], r.get("source"), r.get("status")) for r in rows if r["event"].startswith("source_")]
    assert src == [("source_show", "host", "ok"), ("source_close", "host", "ok")]


def test_static_files_and_pages_are_revalidated(control_client):
    # 實機 2026-11-05：更新程式後瀏覽器仍用快取的舊 host.js（新的「關閉原文」鈕不見、按了沒反應）
    for path in ("/static/host.js", "/static/display.js", "/host", "/display"):
        r = control_client.get(path)
        cc = r.headers.get("cache-control", "")
        assert r.status_code == 200 and ("no-cache" in cc or "no-store" in cc), path


def test_pages_reference_versioned_scripts(control_client):
    # 已經被瀏覽器快取的舊 JS：網址帶版本，更新後就一定抓新的
    import re as _re

    html = control_client.get("/host").text
    assert _re.search(r'/static/host\.js\?v=\d+"', html) and _re.search(r'/static/quizview\.js\?v=\d+"', html)
    assert _re.search(r'/static/common\.css\?v=\d+"', control_client.get("/display").text)


# ---- 主持人畫面「結束程式」（使用者要求 2026-11-05：按下後再確認才關）----

def test_quit_calls_handler(control_client, state):
    calls = []
    state.quit_handler = lambda: calls.append(1)
    r = control_client.post("/api/quit")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls == [1]


def test_quit_without_handler_409(control_client, state):
    state.quit_handler = None
    r = control_client.post("/api/quit")
    assert r.status_code == 409
    assert "Ctrl+C" in r.json()["detail"]


def test_quit_requires_post(control_client, state):
    calls = []
    state.quit_handler = lambda: calls.append(1)
    assert control_client.get("/api/quit").status_code == 405
    assert calls == []


def test_phone_app_cannot_quit(phone_client, state):
    calls = []
    state.quit_handler = lambda: calls.append(1)
    assert phone_client.post("/api/quit").status_code in (404, 405)
    assert calls == []


# ---- 原文重點摘要簡報（§6.2b）----

class _SummaryJobs:
    def __init__(self):
        self.started, self.cancelled = 0, 0

    def summarize_source(self, viewer):
        self.started += 1
        return "j1"

    def cancel_summary(self):
        self.cancelled += 1
        return True


def test_summary_start_409_without_source(state):
    jobs = _SummaryJobs()
    with TestClient(create_control_app(state, Hub(), jobs, viewer=object())) as c:
        state.source_view = None
        r = c.post("/api/summary/start")
        assert r.status_code == 409 and r.json()["detail"] == "請先打開要整理的原文"
        assert jobs.started == 0


def test_summary_start_and_cancel(state):
    jobs = _SummaryJobs()
    with TestClient(create_control_app(state, Hub(), jobs, viewer=object())) as c:
        state.source_view = {"kind": "web", "card": 1, "url": "https://x"}
        assert c.post("/api/summary/start").json() == {"job": "j1"}
        assert c.post("/api/summary/cancel").json() == {"ok": True}
        assert jobs.started == 1 and jobs.cancelled == 1


def test_summary_endpoints_control_only(phone_client):
    for path in ["/api/summary/start", "/api/summary/cancel"]:
        assert phone_client.post(path).status_code in (404, 405), path



def test_summary_start_409_when_busy(state):
    class Busy(_SummaryJobs):
        def summarize_source(self, viewer):
            return ""

    with TestClient(create_control_app(state, Hub(), Busy(), viewer=object())) as c:
        state.source_view = {"kind": "web", "card": 1, "url": "https://x"}
        r = c.post("/api/summary/start")
        assert r.status_code == 409 and "稍後" in r.json()["detail"]



# ---- OE 簡報（§6.2c）：投影幕點參考文獻 ----

def test_deck_links_endpoint_only_for_oe_deck(control_client, state, tmp_path):
    import json as _json
    from pathlib import Path

    assert control_client.get("/deck/links/0").json() == []
    pdf = state.deck.path
    Path(pdf).with_suffix(".links.json").write_text(_json.dumps({"0": [{"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2, "ref": "n:3"}]}),
                                                     encoding="utf-8")
    state.temp_kind = "oe"
    try:
        assert control_client.get("/deck/links/0").json()[0]["ref"] == "n:3"
    finally:
        state.temp_kind = None


def test_oe_ref_endpoint_control_only(phone_client):
    assert phone_client.post("/api/oe/ref", json={"ref": "n:1"}).status_code in (404, 405)
    assert phone_client.get("/deck/links/0").status_code in (404, 405)


def test_oe_ref_endpoint_opens(state):
    class V:
        def open_reference(self, ref):
            return {"status": "ok", "message": ref}

    with TestClient(create_control_app(state, Hub(), None, viewer=V())) as c:
        assert c.post("/api/oe/ref", json={"ref": "n:3"}).json() == {"status": "ok", "message": "n:3"}



def test_oe_play_endpoint_control_only(phone_client, state):
    assert phone_client.post("/api/oe/play").status_code in (404, 405)

    class J:
        def play_answer_oe(self):
            return True

    with TestClient(create_control_app(state, Hub(), J())) as c:
        assert c.post("/api/oe/play").json() == {"ok": True}


def test_oe_stop_endpoint_control_only(phone_client, state, make_pdf, config):
    assert phone_client.post("/api/oe/stop").status_code in (404, 405)
    state.play_summary(make_pdf(["O"], folder=config.folder("紀錄") / "OE簡報", name="o.pdf"), kind="oe")
    with TestClient(create_control_app(state, Hub())) as c:
        assert c.post("/api/oe/stop").json() == {"ok": True}
        assert state.temp_kind is None
        assert c.post("/api/oe/stop").json() == {"ok": False}


# ---- 設定（名字、喚醒詞、專科、會議類型） ----

SETTINGS = {"display_name": "賈維斯", "wake_word": "", "specialty": "", "meeting_type": "病例討論", "meeting_type_other": ""}


@pytest.fixture
def tmp_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("# 說明\ndata_root = 'D:\\x'\naudio_device = 'USBMIC'\n", encoding="utf-8")
    return p


@pytest.fixture
def client(state, tmp_config):
    with TestClient(create_control_app(state, Hub(), config_path=tmp_config)) as c:
        yield c


def test_get_settings_defaults_and_meeting_types(client):
    from xiaoshen.persona import MEETING_TYPES

    body = client.get("/api/settings").json()
    assert body == {**SETTINGS, "meeting_types": list(MEETING_TYPES)}


def test_post_settings_validates(client, tmp_config):
    before = tmp_config.read_text(encoding="utf-8")
    r = client.post("/api/settings", json={**SETTINGS, "meeting_type": "晨會"})
    assert r.status_code == 422 and "meeting_type" in r.json()["errors"]
    r = client.post("/api/settings", json={**SETTINGS, "display_name": ""})
    assert r.status_code == 422 and "display_name" in r.json()["errors"]
    assert tmp_config.read_text(encoding="utf-8") == before


def test_post_settings_updates_persona_immediately(client, tmp_config):
    from xiaoshen.persona import context, current

    r = client.post("/api/settings", json={"display_name": "賈維斯", "wake_word": "", "specialty": "心臟科",
                                            "meeting_type": "其它", "meeting_type_other": "讀書會"})
    assert r.status_code == 200
    assert context(current()) == "心臟科讀書會"
    assert client.get("/api/settings").json()["specialty"] == "心臟科"


def test_post_settings_writes_config_and_keeps_other_lines(client, tmp_config):
    import tomllib

    client.post("/api/settings", json={**SETTINGS, "display_name": "小智慧", "specialty": "腎臟科"})
    raw = tomllib.loads(tmp_config.read_text(encoding="utf-8"))
    assert raw["display_name"] == "小智慧" and raw["specialty"] == "腎臟科" and raw["wake_word"] == ""
    assert raw["data_root"] == "D:\\x" and raw["audio_device"] == "USBMIC"
    assert "# 說明" in tmp_config.read_text(encoding="utf-8")
    assert client.get("/api/settings").json()["display_name"] == "小智慧"  # 表單顯示已存的值


def test_restart_needed_only_for_name_or_wake(client):
    r = client.post("/api/settings", json={**SETTINGS, "specialty": "心臟科", "meeting_type": "VS teaching"})
    assert r.json() == {"ok": True, "restart_needed": False}
    r = client.post("/api/settings", json={**SETTINGS, "wake_word": "賈維斯"})  # 有效喚醒詞沒變
    assert r.json()["restart_needed"] is False
    assert client.post("/api/settings", json={**SETTINGS, "wake_word": "小賈"}).json()["restart_needed"] is True
    assert client.post("/api/settings", json={**SETTINGS, "display_name": "小智慧"}).json()["restart_needed"] is True


def test_name_in_snapshot_stays_running_name_until_restart(client, state):
    from xiaoshen.persona import current

    client.post("/api/settings", json={**SETTINGS, "display_name": "小智慧", "specialty": "腎臟科"})
    assert current().display_name == "賈維斯" and current().specialty == "腎臟科"
    assert client.get("/api/state?audience=host").json()["app"] == {"name": "賈維斯", "wake": "賈維斯", "audience": "與會人員"}


def test_settings_not_on_phone_app(phone_client):
    assert phone_client.get("/api/settings").status_code == 404
    assert phone_client.post("/api/settings", json=SETTINGS).status_code in (404, 405)


def test_snapshot_has_app_name(state):
    from xiaoshen.persona import Persona, set_persona

    set_persona(Persona(display_name="賈維斯"))
    for audience in ("display", "host"):
        assert state.snapshot(audience)["app"] == {"name": "賈維斯", "wake": "賈維斯", "audience": "與會人員"}


def test_snapshot_wake_word_differs_from_name(state):
    from xiaoshen.persona import Persona, set_persona, set_wake_word

    set_persona(Persona(display_name="腎臟科教學助理"))
    set_wake_word("小助")
    for audience in ("display", "host"):
        assert state.snapshot(audience)["app"] == {"name": "腎臟科教學助理", "wake": "小助", "audience": "與會人員"}


def test_web_hints_tell_people_to_say_the_wake_word():
    """「喊…」「說…，播放…」要用喚醒詞，不是名字（I-2）。"""
    from xiaoshen.server import WEB_DIR

    display = (WEB_DIR / "display.js").read_text(encoding="utf-8")
    host = (WEB_DIR / "host.js").read_text(encoding="utf-8")
    quiz = (WEB_DIR / "quizview.js").read_text(encoding="utf-8")
    assert "function wakeWord() { return appInfo.wake || appInfo.name; }" in quiz
    assert "喊「${wakeWord()}」開始對話" in display
    assert "「${esc(wakeWord())}，播放" in display and "「${esc(wakeWord())}，結束簡報」" in display
    assert "待命（喊「${wakeWord()}」或按叫醒）" in host
    for text in (display, host):
        assert "喊「${appName()}" not in text and "「${esc(appName())}，" not in text


def test_wake_word_js_uses_snapshot_wake_then_name():
    import json
    import shutil
    import subprocess

    from xiaoshen.server import WEB_DIR

    node = shutil.which("node")
    if node is None:
        pytest.skip("沒有 node")
    script = ("const document = { querySelectorAll: () => [], querySelector: () => null };\n"
              + (WEB_DIR / "quizview.js").read_text(encoding="utf-8")
              + "\nsetApp({ name: '腎臟科教學助理', wake: '小助', audience: '與會人員' }); const a = [appName(), wakeWord()];"
              + "\nsetApp({ name: '阿腎', audience: '與會人員' }); const b = wakeWord();"
              + "\nprocess.stdout.write(JSON.stringify([a, b]));\n")
    r = subprocess.run([node, "-"], input=script.encode("utf-8"), capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert json.loads(r.stdout.decode("utf-8")) == [["腎臟科教學助理", "小助"], "阿腎"]


def test_phone_control_view_has_app_name(phone_client, state):
    assert phone_client.get(f"/api/phone/control?t={state.token}").json()["app"] == {"name": "賈維斯", "wake": "賈維斯", "audience": "與會人員"}


def test_web_has_no_hardcoded_name():
    from xiaoshen.server import WEB_DIR

    for f in WEB_DIR.glob("*.*"):
        t = f.read_text(encoding="utf-8")
        assert "住院醫師" not in t and "晨會" not in t, f.name

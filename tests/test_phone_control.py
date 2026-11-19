"""手機打字發問與投影控制（spec §6.3a）。"""

import pytest
from fastapi.testclient import TestClient

from xiaoshen.hub import Hub
from xiaoshen.phone_control import PhoneControl
from xiaoshen.server import create_phone_app


@pytest.fixture
def hud_state(config, make_pdf, bank_question):
    """3 頁簡報 D.pdf，開機在主畫面（沒有播放）。"""
    from xiaoshen.state import AppState

    deck = make_pdf(["Case 1 " * 5, "Case 2 " * 5, "Case 3 " * 5], folder=config.folder("簡報"), name="D.pdf")
    st = AppState(config, {("D", 2): bank_question})
    st.open_deck(deck)
    st.set_deck_paths([deck])
    st.phone_search_enabled = True   # §6.3a：預設關閉；這裡的測試假設主持人已開放
    st.phone_control_enabled = True  # 2026-11-18 起也預設關閉；同上
    return st


class FakeJobs:
    def __init__(self, job="j1"):
        self.job, self.asked, self.played = job, [], 0

    def ask(self, text, route, refs, spoken=False):
        self.asked.append((text, route, spoken))
        return self.job

    def play_pending_oe(self):
        self.played += 1
        return True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TickClock(Clock):
    """每讀一次往前 10 秒：測試不受翻頁／播放的速率限制影響。"""

    def __call__(self):
        self.t += 10
        return self.t


def pc(state, jobs=None, clock=None):
    return PhoneControl(state, jobs or FakeJobs(), clock=clock or TickClock())


# ---- 打字發問 ----

def test_ask_goes_to_jobs_and_says_sent(hud_state):
    jobs = FakeJobs()
    r = pc(hud_state, jobs).ask("c1", "SRNS 一線？", "openevidence")
    assert r == {"status": "ok", "message": "已送出，結果會顯示在投影幕上"}
    assert jobs.asked == [("SRNS 一線？", "openevidence", False)]


def test_search_only_literature_route(hud_state):
    # 2026-11-05 修訂：「搜尋網路」只查文獻，拿掉「根據今天的簡報」
    jobs = FakeJobs()
    assert pc(hud_state, jobs).ask("c1", "這份簡報重點？", "deck")["status"] == "error"
    assert pc(hud_state, jobs).ask("c2", "q", "references")["status"] == "error"
    assert pc(hud_state, jobs).ask("c3", "q", "openevidence")["status"] == "ok"
    assert len(jobs.asked) == 1


def test_ask_rejects_empty_and_too_long(hud_state):
    p = pc(hud_state)
    assert p.ask("c1", "   ", "openevidence")["message"] == "請輸入問題"
    assert "300" in p.ask("c1", "字" * 301, "openevidence")["message"]


def test_ask_refused_while_another_question_runs(hud_state):
    hud_state.answer_job = {"id": "x", "kind": "answer"}
    jobs = FakeJobs()
    r = pc(hud_state, jobs).ask("c1", "q", "openevidence")
    assert r["status"] == "busy" and "稍後" in r["message"] and jobs.asked == []


def test_ask_refused_during_quiz(hud_state, bank_question):
    hud_state.start_run("single", [bank_question])
    jobs = FakeJobs()
    assert pc(hud_state, jobs).ask("c1", "q", "openevidence")["status"] == "busy" and jobs.asked == []


def test_ask_cooldown_per_phone(hud_state):
    clock = Clock()
    p = pc(hud_state, FakeJobs(), clock)
    assert p.ask("c1", "q1", "openevidence")["status"] == "ok"
    clock.t += 10
    r = p.ask("c1", "q2", "openevidence")
    assert r["status"] == "busy" and "20 秒" in r["message"]
    clock.t += 21
    assert p.ask("c1", "q4", "openevidence")["status"] == "ok"  # 30 秒後同一支手機可以再問


def test_ask_phi_not_sent(hud_state):
    r = pc(hud_state, FakeJobs(job="")).ask("c1", "A123456789 的病人", "openevidence")
    assert r["status"] == "blocked" and "識別資訊" in r["message"]


def test_command_text_is_just_a_question(hud_state):
    jobs = FakeJobs()
    pc(hud_state, jobs).ask("c1", "下一頁", "openevidence")
    assert jobs.asked == [("下一頁", "openevidence", False)] and hud_state.page == 0


# ---- 投影控制 ----

def test_view_lists_decks_and_status(hud_state):
    v = pc(hud_state).view()
    assert v["presenting"] is False and v["decks"] == ["D"] and v["page_count"] == 3


def test_nav_only_while_presenting(hud_state):
    p = pc(hud_state)
    assert p.nav("next")["message"] == "目前不在簡報中" and hud_state.page == 0
    hud_state.play(hud_state.deck.path)
    assert p.nav("next")["status"] == "ok" and hud_state.page == 1
    assert p.nav("prev")["status"] == "ok" and hud_state.page == 0
    assert p.nav("goto", 3)["status"] == "ok" and hud_state.page == 2
    assert p.nav("goto", 9)["message"] == "沒有第 9 頁，這份簡報共 3 頁"


def test_play_deck_from_home(hud_state):
    r = pc(hud_state).play("deck", "D")
    assert r["status"] == "ok" and hud_state.presenting


def test_play_refused_while_presenting(hud_state):
    hud_state.play(hud_state.deck.path)
    r = pc(hud_state).play("deck", "D")
    assert r["status"] == "refused" and "只能翻頁或結束" in r["message"]


def test_play_unknown_deck(hud_state):
    assert pc(hud_state).play("deck", "nope")["status"] == "not_found"


def test_play_oe_pending_and_replay(hud_state, make_pdf, config):
    jobs = FakeJobs()
    hud_state.oe_pending = make_pdf(["OE"], folder=config.folder("紀錄") / "OE簡報", name="oe.pdf")
    assert pc(hud_state, jobs).play("oe")["status"] == "ok" and jobs.played == 1
    hud_state.oe_pending = None
    assert pc(hud_state, jobs).play("summary")["status"] == "not_found"
    s = make_pdf(["S"], folder=config.folder("紀錄") / "摘要簡報", name="s.pdf")
    hud_state.last_summary = s
    assert pc(hud_state, jobs).play("summary")["status"] == "ok" and hud_state.temp_kind == "summary"


def test_end_returns_to_previous_screen(hud_state, make_pdf, config):
    p = pc(hud_state)
    assert p.end()["message"] == "目前不在簡報中"
    hud_state.play(hud_state.deck.path)
    hud_state.goto(1)
    hud_state.play_summary(make_pdf(["S"], folder=config.folder("紀錄") / "摘要簡報", name="s.pdf"))
    assert p.end()["status"] == "ok" and hud_state.deck.name == "D" and hud_state.page == 1 and hud_state.presenting
    assert p.end()["status"] == "ok" and not hud_state.presenting


# ---- 手機 app 的端點 ----

@pytest.fixture
def phone(hud_state):
    jobs = FakeJobs()
    with TestClient(create_phone_app(hud_state, jobs)) as c:
        yield c, hud_state, jobs


def test_phone_endpoints_need_token(phone):
    c, st, _ = phone
    assert c.post("/api/phone/ask", json={"t": "bad", "cid": "c", "text": "q", "route": "openevidence"}).status_code == 403
    assert c.get("/api/phone/control?t=bad").status_code == 403
    assert c.post("/api/phone/nav", json={"t": "bad", "action": "next"}).status_code == 403


def test_phone_endpoints_work(phone):
    c, st, jobs = phone
    t = st.token
    assert c.get(f"/api/phone/control?t={t}").json()["decks"] == ["D"]
    assert c.post("/api/phone/ask", json={"t": t, "cid": "c", "text": "q", "route": "openevidence"}).json()["status"] == "ok"
    assert c.post("/api/phone/play", json={"t": t, "kind": "deck", "name": "D"}).json()["status"] == "ok"
    assert c.post("/api/phone/nav", json={"t": t, "action": "next"}).json()["status"] == "ok" and st.page == 1
    assert c.post("/api/phone/end", json={"t": t}).json()["status"] == "ok" and not st.presenting


def test_phone_still_cannot_reach_host_commands(phone):
    c, st, _ = phone
    for path in ["/api/quiz/dispatch", "/api/quiz/reveal", "/api/quiz/collect", "/api/drawer/hide",
                 "/api/quiz/generate", "/api/drafts/publish", "/api/nav", "/api/present", "/api/ask"]:
        assert c.post(path, json={"t": st.token}).status_code in (404, 405), path


# ---- 審查修正 ----

def test_play_refused_during_quiz(hud_state, bank_question, make_pdf, config):
    # I1：派題中手機不能播 OE／摘要簡報（會收起題目與 QR）
    jobs = FakeJobs()
    hud_state.start_run("single", [bank_question])
    hud_state.oe_pending = make_pdf(["OE"], folder=config.folder("紀錄") / "OE簡報", name="oe.pdf")
    p = pc(hud_state, jobs)
    for kind, name in (("oe", ""), ("summary", ""), ("deck", "D")):
        r = p.play(kind, name)
        assert r["status"] == "refused" and "作答" in r["message"], kind
    assert jobs.played == 0 and hud_state.drawer == "quiz"
    v = p.view()
    assert v["has_oe"] is False and v["decks"] == [] and v["quiz_open"] is True


def test_ask_global_interval_across_phones(hud_state):
    # I2：換一個 cid（新分頁）不能繞過；全部手機合計也有間隔
    clock = Clock()
    p = pc(hud_state, FakeJobs(), clock)
    assert p.ask("c1", "q1", "openevidence")["status"] == "ok"
    r = p.ask("c-new", "q2", "openevidence")
    assert r["status"] == "busy" and "秒後" in r["message"]
    clock.t += 21
    assert p.ask("c-new", "q2", "openevidence")["status"] == "ok"


def test_controls_throttled(hud_state):
    hud_state.bank = {}  # 不讓翻到第 2 頁自動派題（派題中不能播放）
    clock = Clock()
    p = pc(hud_state, FakeJobs(), clock)
    hud_state.play(hud_state.deck.path)
    assert p.nav("next")["status"] == "ok"
    assert p.nav("next")["status"] == "busy" and hud_state.page == 1  # 太快：不翻
    clock.t += 1
    assert p.nav("next")["status"] == "ok" and hud_state.page == 2
    assert p.end()["status"] == "ok"
    assert p.play("deck", "D")["status"] == "ok"
    assert p.end()["status"] == "busy" and hud_state.presenting  # 連續結束：擋
    clock.t += 4
    assert p.end()["status"] == "ok"
    assert p.play("deck", "D")["status"] == "ok"
    assert p.end()["status"] == "busy"
    clock.t += 4
    assert p.end()["status"] == "ok"
    assert p.play("deck", "D")["status"] == "ok"
    clock.t += 1
    p2_play = p.play("deck", "D")  # 播放中本來就不能再播
    assert p2_play["status"] == "refused"


def test_host_can_pause_phone_control(hud_state):
    jobs = FakeJobs()
    p = pc(hud_state, jobs)
    hud_state.phone_control_enabled = False
    for r in (p.play("deck", "D"), p.nav("next"), p.end()):
        assert r["status"] == "paused" and "主持人已暫停" in r["message"]
    assert not hud_state.presenting
    assert p.view()["control_enabled"] is False
    assert p.ask("c1", "q", "openevidence")["status"] == "ok"  # 搜尋網路有自己的開關


def test_host_toggle_endpoint(state):
    from xiaoshen.server import create_control_app

    with TestClient(create_control_app(state, Hub())) as c:
        assert c.post("/api/phone/control-enabled", json={"enabled": False}).json() == {"enabled": False}
        assert state.phone_control_enabled is False and state.snapshot("host")["phone_control_enabled"] is False
        assert c.post("/api/phone/control-enabled", json={"enabled": True}).json() == {"enabled": True}
        assert c.post("/api/phone/search-enabled", json={"enabled": True}).json() == {"enabled": True}
        assert state.phone_search_enabled is True and state.snapshot("host")["phone_search_enabled"] is True



# ---- 2026-11-05 修訂：搜尋網路預設關閉；詢問老師 ----

def test_search_off_by_default(config):
    from xiaoshen.state import AppState

    st = AppState(config, {})
    jobs = FakeJobs()
    r = pc(st, jobs).ask("c1", "q", "openevidence")
    assert st.phone_search_enabled is False and r["status"] == "off" and jobs.asked == []
    assert pc(st, jobs).view()["search_enabled"] is False


def test_control_off_by_default(config):
    """2026-11-18：手機投影控制也預設關閉，主持人在「手機操作」區打開才能用。"""
    from xiaoshen.state import AppState

    st = AppState(config, {})
    r = pc(st).end()
    assert st.phone_control_enabled is False and r["status"] == "paused"
    assert pc(st).view()["control_enabled"] is False and st.snapshot("host")["phone_control_enabled"] is False


def test_teacher_question_goes_to_host_only(hud_state):
    r = pc(hud_state).teacher("c1", "老師，CNI 要用多久？")
    assert r == {"status": "ok", "message": "已送給老師"}
    host = hud_state.snapshot("host")["teacher_questions"]
    assert [q["text"] for q in host] == ["老師，CNI 要用多久？"] and host[0]["phi"] is False
    disp = hud_state.snapshot("display")
    assert "CNI 要用多久" not in str(disp) and disp["teacher_questions_shown"] == []


def test_teacher_question_works_even_when_switches_off(hud_state):
    hud_state.phone_search_enabled = False
    hud_state.phone_control_enabled = False
    assert pc(hud_state).teacher("c1", "q")["status"] == "ok"


def test_teacher_question_validation_and_cooldown(hud_state):
    clock = Clock()
    p = pc(hud_state, clock=clock)
    assert p.teacher("c1", "  ")["message"] == "請輸入問題"
    assert "300" in p.teacher("c1", "字" * 301)["message"]
    assert p.teacher("c1", "q1")["status"] == "ok"
    assert p.teacher("c1", "q2")["status"] == "busy"
    clock.t += 16
    assert p.teacher("c1", "q2")["status"] == "ok"


def test_teacher_question_list_capped(hud_state):
    for i in range(55):
        hud_state.add_teacher_question(f"q{i}")
    qs = hud_state.snapshot("host")["teacher_questions"]
    assert len(qs) == 50 and qs[-1]["text"] == "q54"


def test_show_teacher_question_on_projector_and_hide(hud_state):
    qid = hud_state.add_teacher_question("CNI 要用多久？")
    assert hud_state.show_teacher_question(qid) is True
    disp = hud_state.snapshot("display")
    assert disp["drawer"] == "teacher" and disp["teacher_questions_shown"] == [{"text": "CNI 要用多久？"}]
    hud_state.hide_drawer()
    assert hud_state.snapshot("display")["teacher_questions_shown"] == []


def test_phi_teacher_question_cannot_be_projected(hud_state):
    qid = hud_state.add_teacher_question("病人 A123456789 的劑量？")
    q = hud_state.snapshot("host")["teacher_questions"][0]
    assert q["phi"] is True
    assert hud_state.show_teacher_question(qid) is False
    assert hud_state.snapshot("display")["teacher_questions_shown"] == []


def test_host_teacher_endpoints(state):
    from xiaoshen.server import create_control_app

    jobs = FakeJobs()
    with TestClient(create_control_app(state, Hub(), jobs)) as c:
        qid = state.add_teacher_question("CNI 要用多久？")
        assert c.post("/api/teacher/show", json={"id": qid}).json()["ok"] is True
        r = c.post("/api/teacher/ask", json={"id": qid}).json()
        assert r["ok"] is True and jobs.asked == [("CNI 要用多久？", "openevidence", False)]
        assert c.post("/api/teacher/remove", json={"id": qid}).json()["ok"] is True
        assert state.snapshot("host")["teacher_questions"] == []
        assert c.post("/api/teacher/show", json={"id": "nope"}).json()["ok"] is False


def test_phone_teacher_endpoint(phone):
    c, st, _ = phone
    assert c.post("/api/phone/teacher", json={"t": "bad", "cid": "c", "text": "q"}).status_code == 403
    assert c.post("/api/phone/teacher", json={"t": st.token, "cid": "c", "text": "q"}).json()["status"] == "ok"
    assert c.get(f"/api/phone/control?t={st.token}").json()["search_enabled"] is True


def test_host_shown_flag_clears_after_drawer_hidden(hud_state):
    # 瀏覽器實測：收起抽屜後主持人畫面還顯示「投影中」，按鈕也不見了
    qid = hud_state.add_teacher_question("CNI 要用多久？")
    hud_state.show_teacher_question(qid)
    assert hud_state.snapshot("host")["teacher_questions"][0]["shown"] is True
    hud_state.hide_drawer()
    assert hud_state.snapshot("host")["teacher_questions"][0]["shown"] is False



# ---- 實機回饋：投影後要能收回；可以同時投影多題；OE 簡報結束後狀態要更新 ----

def test_several_questions_shown_together_in_order(hud_state):
    a = hud_state.add_teacher_question("第一題")
    b = hud_state.add_teacher_question("第二題")
    hud_state.show_teacher_question(a)
    hud_state.show_teacher_question(b)
    assert hud_state.snapshot("display")["teacher_questions_shown"] == [{"text": "第一題"}, {"text": "第二題"}]
    host = {q["text"]: q["shown"] for q in hud_state.snapshot("host")["teacher_questions"]}
    assert host == {"第一題": True, "第二題": True}


def test_retract_one_question_then_last_closes_drawer(hud_state):
    a = hud_state.add_teacher_question("第一題")
    b = hud_state.add_teacher_question("第二題")
    hud_state.show_teacher_question(a)
    hud_state.show_teacher_question(b)
    assert hud_state.hide_teacher_question(a) is True
    assert hud_state.snapshot("display")["teacher_questions_shown"] == [{"text": "第二題"}]
    assert hud_state.hide_teacher_question(b) is True
    assert hud_state.drawer == "hidden" and hud_state.snapshot("display")["teacher_questions_shown"] == []


def test_hide_drawer_clears_shown_questions(hud_state):
    a = hud_state.add_teacher_question("第一題")
    hud_state.show_teacher_question(a)
    hud_state.hide_drawer()
    b = hud_state.add_teacher_question("第二題")
    hud_state.show_teacher_question(b)  # 收起後再投影另一題：前一題不會又冒出來
    assert hud_state.snapshot("display")["teacher_questions_shown"] == [{"text": "第二題"}]


def test_host_hide_endpoints(state):
    from xiaoshen.server import create_control_app

    with TestClient(create_control_app(state, Hub(), FakeJobs())) as c:
        a = state.add_teacher_question("第一題")
        b = state.add_teacher_question("第二題")
        c.post("/api/teacher/show", json={"id": a})
        c.post("/api/teacher/show", json={"id": b})
        assert c.post("/api/teacher/hide", json={"id": a}).json()["ok"] is True
        assert [q["text"] for q in state.snapshot("display")["teacher_questions_shown"]] == ["第二題"]
        assert c.post("/api/teacher/hide-all").json()["ok"] is True
        assert state.drawer == "hidden"


def test_oe_status_follows_projector(hud_state, make_pdf, config):
    # 實機：OE 簡報收回後，主持人畫面仍顯示「OE 簡報：投影中」
    from xiaoshen.answering import Answer

    p = make_pdf(["OE"], folder=config.folder("紀錄") / "OE簡報", name="oe.pdf")
    hud_state.answer = Answer("a1", "q", "openevidence", "answered", oe_deck=str(p), oe_pages=1, oe_play="oe_playing")
    hud_state.play_summary(p, kind="oe")
    assert hud_state.snapshot("host")["ai_answer"]["oe_deck"]["play"] == "oe_playing"
    hud_state.stop_presenting()
    assert hud_state.snapshot("host")["ai_answer"]["oe_deck"]["play"] == "oe_ended"


def test_oe_play_endpoint_replays_ended_deck(hud_state, make_pdf, config):
    from xiaoshen.answering import Answer
    from xiaoshen.jobs import JobRunner
    from xiaoshen.library import Library
    from xiaoshen.server import create_control_app

    p = make_pdf(["OE"], folder=config.folder("紀錄") / "OE簡報", name="oe.pdf")
    hud_state.answer = Answer("a1", "q", "openevidence", "answered", oe_deck=str(p), oe_pages=1, oe_play="oe_playing")
    jobs = JobRunner(hud_state, None, config, Library(config.folder("參考資料")))
    with TestClient(create_control_app(hud_state, Hub(), jobs)) as c:
        assert c.post("/api/oe/play").json() == {"ok": True}
    assert hud_state.temp_kind == "oe" and hud_state.snapshot("host")["ai_answer"]["oe_deck"]["play"] == "oe_playing"


def test_play_unconverted_pptx_from_phone(hud_state, make_pdf):
    folder = hud_state.config.folder("簡報")
    src = folder / "New.pptx"
    src.write_bytes(b"pptx")
    hud_state.set_deck_paths([src, *hud_state.deck_paths])
    hud_state.deck_converter = lambda s: make_pdf(["New " * 5], folder=s.parent, name=s.stem + ".pdf")
    r = pc(hud_state).play("deck", "New")
    assert r["status"] == "converting" and "好了會自動播放" in r["message"]
    hud_state._convert_thread.join(5)
    assert hud_state.presenting and hud_state.deck.name == "New"

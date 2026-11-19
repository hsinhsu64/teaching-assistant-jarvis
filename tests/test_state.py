import time
import pytest

from xiaoshen.quiz import Question


def test_open_deck_starts_at_first_page_without_dispatch(state):
    assert state.deck.name == "D" and state.page == 0 and state.run is None and state.drawer == "hidden"


def test_page_change_auto_dispatches(state):
    state.next()
    assert state.page == 1 and state.run is not None and state.drawer == "quiz"
    assert state.run.questions[0].id == "D-p2"


def test_no_dispatch_while_run_open(state):
    state.next()
    first = state.run
    state.prev()
    state.next()
    assert state.run is first


def test_auto_dispatch_only_once_per_page(state):
    state.next()
    state.reveal()
    state.prev()
    state.next()
    assert state.run.status == "closed" and state.drawer == "results"


def test_manual_dispatch_redispatches(state):
    state.next()
    state.reveal()
    run = state.dispatch_current()
    assert run.status == "open" and state.drawer == "quiz"


def test_dispatch_current_without_question(state):
    with pytest.raises(LookupError):
        state.dispatch_current()


def test_start_run_rejects_second_open_run(state):
    state.next()
    with pytest.raises(RuntimeError):
        state.start_run("single", [Question("x", "s", ["A", "B"], 0)])


def test_page_clamped(state):
    state.goto(99)
    assert state.page == 2
    state.goto(-5)
    assert state.page == 0


def test_navigation_without_deck_is_noop(config):
    from xiaoshen.state import AppState

    st = AppState(config, {})
    st.next()
    assert st.page == 0 and st.deck is None


def test_open_bad_deck_keeps_running(state, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"x")
    state.open_deck(bad)
    assert state.deck is None and state.warnings


def test_reveal_saves_record(state, config):
    state.next()
    state.submit("a", {"D-p2": 1}, final=True)
    state.reveal()
    assert state.drawer == "results"
    assert len(list(config.folder("紀錄").glob("*.json"))) == 1


def test_reveal_survives_write_failure(state, monkeypatch):
    import xiaoshen.state as st_mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(st_mod, "save_record", boom)
    state.next()
    state.reveal()
    assert state.drawer == "results" and any("紀錄" in w for w in state.warnings)


def test_collect_closes_exam(state):
    state.start_run("exam", [Question("e1", "s", ["A", "B"], 0), Question("e2", "s", ["A", "B"], 1)])
    state.collect()
    assert state.run.status == "closed" and state.drawer == "results"


def test_hide_drawer(state):
    state.next()
    state.hide_drawer()
    assert state.drawer == "hidden"


def test_on_change_called(state):
    calls = []
    state.on_change = lambda: calls.append(1)
    state.next()
    assert calls


def test_snapshot_display_vs_host(state):
    state.public_base = "https://x.trycloudflare.com"
    state.warnings.append("w")
    state.next()
    d, h = state.snapshot("display"), state.snapshot("host")
    assert "warnings" not in d and "checks" not in d
    assert h["warnings"] == ["w"] and h["run"]["questions"][0]["answer"] == 1
    assert d["phone_url"] == f"https://x.trycloudflare.com/q?t={state.token}"
    assert d["deck"] == {"name": "D", "page_count": 3, "version": state.deck_version, "temp_kind": None}
    assert d["keys"]["next"] == state.config.keys_next


def test_deck_version_unique_across_processes(config, make_pdf, bank_question):
    from xiaoshen.state import AppState

    path = config.folder("簡報") / "D.pdf"
    a, b = AppState(config, {}), AppState(config, {})
    a.open_deck(path)
    b.open_deck(path)
    assert a.deck_version != b.deck_version


def test_phone_link_down_hides_url_and_marks_check(state):
    from xiaoshen.state import check_phone_link

    state.public_base = "https://x.trycloudflare.com"
    state.checks = [{"name": "手機作答", "level": "ok", "detail": "x"}]
    calls = []
    state.on_change = lambda: calls.append(1)
    check_phone_link(state, http_get=lambda u: 502)
    assert state.phone_url() is None and state.checks[0]["level"] == "fail" and calls
    check_phone_link(state, http_get=lambda u: 200)
    assert state.phone_url() and state.checks[0]["level"] == "ok"


def test_phone_url_hidden_while_link_pending(state):
    state.public_base = "https://x.trycloudflare.com"
    state.phone_pending = True
    snap = state.snapshot("display")
    assert snap["phone_url"] is None and snap["phone_pending"] is True
    state.phone_pending = False
    assert state.snapshot("display")["phone_pending"] is False and state.phone_url()


def test_phone_link_check_exception_counts_as_down(state):
    from xiaoshen.state import check_phone_link

    state.public_base = "https://x.trycloudflare.com"

    def boom(u):
        raise RuntimeError("net")

    check_phone_link(state, http_get=boom)
    assert state.phone_url() is None


def _pdf_converter(make_pdf_fn, texts):
    """假轉檔器：輸出含指定文字的 PDF。"""
    import shutil

    def convert(src, dst):
        shutil.copy(make_pdf_fn(texts[src.stem]), dst)

    return convert


def test_refresh_presentations_converts_and_records_check(state_factory, make_pdf):
    from xiaoshen.state import refresh_presentations

    st = state_factory()
    (st.config.folder("簡報") / "New.pptx").write_bytes(b"x")
    report = refresh_presentations(st, _pdf_converter(make_pdf, {"New": ["n " * 20]}), set())
    assert report.converted == ["New.pptx"]
    assert (st.config.folder("簡報") / "New.pdf").exists()
    assert st.conversion_check == {"name": "簡報轉檔", "level": "ok", "detail": "已轉換：New.pptx"}


def test_refresh_reloads_open_deck_keeping_page(state, make_pdf):
    import os
    import time

    from xiaoshen.state import refresh_presentations

    state.goto(2)
    old_version = state.deck_version
    src = state.config.folder("簡報") / "D.pptx"
    src.write_bytes(b"x")
    t = time.time() + 60
    os.utime(src, (t, t))     # 使用者剛改過 pptx
    refresh_presentations(state, _pdf_converter(make_pdf, {"D": ["v2 p1 " * 5, "v2 p2 " * 5, "v2 p3 " * 5, "v2 p4 " * 5]}), set())
    assert state.deck.page_count == 4 and "v2 p3" in state.deck.page_text(2)
    assert state.page == 2 and state.deck_version != old_version


def test_refresh_failure_warns_once(state_factory):
    from xiaoshen.state import refresh_presentations

    st = state_factory()
    (st.config.folder("簡報") / "Bad.pptx").write_bytes(b"x")

    def broken(src, dst):
        raise RuntimeError("壞檔")

    reported = set()
    refresh_presentations(st, broken, reported)
    refresh_presentations(st, broken, reported)
    assert sum("Bad.pptx" in w for w in st.warnings) == 1
    assert st.conversion_check["level"] == "warn"


def test_refresh_without_powerpoint_warns(state_factory):
    from xiaoshen.state import refresh_presentations

    st = state_factory()
    (st.config.folder("簡報") / "A.pptx").write_bytes(b"x")
    refresh_presentations(st, None, set())
    assert st.conversion_check["level"] == "warn" and "PowerPoint" in st.conversion_check["detail"]
    assert not (st.config.folder("簡報") / "A.pdf").exists()


def test_refresh_with_auto_convert_off_skips_and_warns(state_factory):
    from xiaoshen.state import refresh_presentations

    st = state_factory(auto_convert_pptx=False)
    (st.config.folder("簡報") / "A.pptx").write_bytes(b"x")
    called = []
    report = refresh_presentations(st, lambda s, d: called.append(s), set())
    assert called == [] and report.attempted == []
    assert st.conversion_check == {"name": "簡報轉檔", "level": "warn",
                                   "detail": "已關閉自動轉檔，請自己匯出 PDF"}
    assert not (st.config.folder("簡報") / "A.pdf").exists()


# ---- 主畫面（§6.0）----

@pytest.fixture
def hud_state(config, make_pdf, bank_question):
    """同 conftest state，但只 open_deck、不播放（開機狀態）。"""
    from xiaoshen.state import AppState

    deck = make_pdf(["Case 1 " * 5, "Case 2 " * 5, "Case 3 " * 5], folder=config.folder("簡報"), name="D.pdf")
    st = AppState(config, {("D", 2): bank_question})
    st.open_deck(deck)
    return st


def test_open_deck_does_not_present(hud_state):
    assert hud_state.presenting is False and hud_state.snapshot("display")["presenting"] is False


def test_preview_nav_does_not_dispatch(hud_state):
    hud_state.next()
    assert hud_state.page == 1 and hud_state.run is None


def test_play_presents_from_page_one(hud_state):
    hud_state.next()
    hud_state.play(hud_state.deck.path)
    assert hud_state.presenting and hud_state.page == 0 and hud_state.run is None


def test_replay_resumes_last_presenting_page(hud_state):
    hud_state.play(hud_state.deck.path)
    hud_state.goto(2)
    hud_state.stop_presenting()
    hud_state.goto(0)  # 主持人預覽改了頁碼
    hud_state.play(hud_state.deck.path)
    assert hud_state.page == 2


def test_other_deck_starts_at_page_one(hud_state, make_pdf):
    hud_state.play(hud_state.deck.path)
    hud_state.goto(2)
    hud_state.stop_presenting()
    other = make_pdf(["x " * 5, "y " * 5], folder=hud_state.config.folder("簡報"), name="E.pdf")
    hud_state.play(other)
    assert hud_state.deck.name == "E" and hud_state.page == 0


def test_auto_dispatch_not_repeated_after_reopen(hud_state):
    hud_state.play(hud_state.deck.path)
    hud_state.next()
    hud_state.reveal()
    hud_state.stop_presenting()
    hud_state.open_deck(hud_state.deck.path)
    hud_state.play(hud_state.deck.path)
    hud_state.goto(0)
    hud_state.next()
    assert hud_state.run.status == "closed"


def test_open_user_line_hidden_on_display(hud_state):
    hud_state.add_transcript("你", "小森，A1234")
    assert hud_state.display_transcript() == []
    assert hud_state.snapshot("host")["voice_transcript"] == ["你：小森，A1234"]
    hud_state.close_transcript_line()
    assert hud_state.display_transcript() == ["你：小森，A1234"]


def test_phi_line_masked_on_display(hud_state):
    hud_state.add_transcript("你", "病歷號：12345678 的孩子")
    hud_state.add_transcript("小森", "好的")
    assert hud_state.display_transcript() == ["你：（含個資，已隱藏）", "小森：好的"]
    assert hud_state.snapshot("display")["voice_transcript"] == ["你：（含個資，已隱藏）", "小森：好的"]


def test_deck_list_in_snapshot(hud_state, make_pdf):
    p = make_pdf(["z " * 5], folder=hud_state.config.folder("簡報"), name="F.pdf")
    hud_state.set_deck_paths([hud_state.deck.path, p])
    assert hud_state.snapshot("display")["decks"] == [{"name": "D"}, {"name": "F"}]


# ---- 整支審查修正 ----

def test_failed_switch_keeps_current_deck(hud_state, tmp_path):
    hud_state.play(hud_state.deck.path)
    hud_state.goto(2)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"x")
    hud_state.play(bad)
    assert hud_state.deck.name == "D" and hud_state.presenting and hud_state.page == 2
    assert hud_state.warnings


def test_phi_split_across_lines_masks_whole_turn(hud_state):
    hud_state.add_transcript("你", "病歷號：1234")
    hud_state.close_transcript_line()          # 工具呼叫先到
    hud_state.add_transcript("小森", "我查一下")
    hud_state.add_transcript("你", "5678 的孩子")  # 逐字稿晚到
    hud_state.end_transcript_turn()
    assert hud_state.display_transcript() == ["你：（含個資，已隱藏）", "小森：我查一下", "你：（含個資，已隱藏）"]
    hud_state.add_transcript("你", "下一個問題")
    hud_state.end_transcript_turn()
    assert hud_state.display_transcript()[-1] == "你：下一個問題"  # 下一輪不受影響


# ---- 原文重點摘要簡報（§6.2b）：臨時播放，結束後回到原本的畫面 ----

def _summary_pdf(make_pdf, config, name="2026-11-05_1320_KDIGO.pdf"):
    return make_pdf(["Summary cover", "Summary slide", "Summary slide 2"],
                    folder=config.folder("紀錄") / "摘要簡報", name=name)


def test_end_summary_returns_to_presenting_deck_page(state, make_pdf, config):
    a = state.deck
    state.goto(2)
    assert state.play_summary(_summary_pdf(make_pdf, config)) is True
    assert state.presenting and state.page == 0 and state.summary_path is not None
    state.stop_presenting()
    assert state.deck is a and state.page == 2 and state.presenting is True
    assert state.summary_path is None


def test_end_summary_from_home_returns_home(hud_state, make_pdf, config):
    a = hud_state.deck
    hud_state.play_summary(_summary_pdf(make_pdf, config))
    hud_state.stop_presenting()
    assert hud_state.presenting is False and hud_state.deck is a


def test_summary_keeps_today_deck_ocr(state, make_pdf, config):
    state.page_ocr = {0: "抄錄文字"}
    state.play_summary(_summary_pdf(make_pdf, config))
    assert state.answer_deck.name == "D" and state.page_ocr == {0: "抄錄文字"}
    state.stop_presenting()
    assert state.page_ocr == {0: "抄錄文字"}


def test_play_other_deck_during_summary_drops_restore(state, make_pdf, config):
    b = make_pdf(["B1", "B2"], folder=config.folder("簡報"), name="B.pdf")
    state.play_summary(_summary_pdf(make_pdf, config))
    state.play(b)
    assert state.deck.name == "B" and state.summary_path is None
    state.stop_presenting()
    assert state.deck.name == "B" and state.presenting is False


def test_play_today_deck_during_summary_resumes_its_page(state, make_pdf, config):
    path = state.deck.path
    state.goto(2)
    state.play_summary(_summary_pdf(make_pdf, config))
    state.play(path)
    assert state.deck.name == "D" and state.page == 2 and state.summary_path is None


def test_summary_replay_keeps_first_restore(state, make_pdf, config):
    a = state.deck
    state.goto(1)
    state.play_summary(_summary_pdf(make_pdf, config))
    state.play_summary(_summary_pdf(make_pdf, config, name="second.pdf"))
    state.stop_presenting()
    assert state.deck is a and state.page == 1


def test_no_auto_dispatch_on_summary_pages(config, make_pdf, bank_question):
    from xiaoshen.state import AppState

    deck = make_pdf(["x", "y"], folder=config.folder("簡報"), name="D.pdf")
    summ = make_pdf(["s1", "s2"], folder=config.folder("紀錄") / "摘要簡報", name="S.pdf")
    st = AppState(config, {("S", 2): bank_question})
    st.play(deck)
    st.play_summary(summ)
    st.goto(1)
    assert st.run is None


def test_play_summary_bad_file_keeps_current(state, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    a = state.deck
    assert state.play_summary(bad) is False
    assert state.deck is a and state.summary_path is None and state.warnings


def test_last_summary_remembered(state, make_pdf, config):
    p = _summary_pdf(make_pdf, config)
    state.play_summary(p)
    state.stop_presenting()
    assert state.last_summary == p



def test_summary_without_today_deck_leaves_no_deck(config, make_pdf):
    # 審查 #1：沒有開簡報時做摘要，結束後摘要簡報不能變成「今天的簡報」
    from xiaoshen.state import AppState

    st = AppState(config, {})
    st.play_summary(make_pdf(["S1", "S2"], folder=config.folder("紀錄") / "摘要簡報", name="S.pdf"))
    assert st.answer_deck is None
    st.stop_presenting()
    assert st.deck is None and st.answer_deck is None and st.presenting is False


# ---- OE 簡報（§6.2c）：臨時簡報有種類（summary／oe）----

def test_oe_temp_deck_restores_like_summary(state, make_pdf, config):
    a = state.deck
    state.goto(2)
    p = make_pdf(["OE cover", "OE body"], folder=config.folder("紀錄") / "OE簡報", name="oe.pdf")
    assert state.play_summary(p, kind="oe") is True
    assert state.temp_kind == "oe" and state.answer_deck is a
    state.stop_presenting()
    assert state.deck is a and state.page == 2 and state.presenting and state.temp_kind is None


def test_last_temp_per_kind(state, make_pdf, config):
    s = make_pdf(["S"], folder=config.folder("紀錄") / "摘要簡報", name="s.pdf")
    o = make_pdf(["O"], folder=config.folder("紀錄") / "OE簡報", name="o.pdf")
    state.play_summary(s)
    state.play_summary(o, kind="oe")
    assert state.last_temp == {"summary": s, "oe": o} and state.last_summary == s


def test_oe_play_hides_drawer(state, make_pdf, config):
    state.drawer = "answer"
    state.play_summary(make_pdf(["O"], folder=config.folder("紀錄") / "OE簡報", name="o.pdf"), kind="oe")
    assert state.drawer == "hidden"
    assert state.snapshot("display")["deck"]["temp_kind"] == "oe"


def test_oe_pending_defaults_none(state):
    assert state.oe_pending is None


# ---- 主持人畫面「收回 OE 簡報」按鈕（使用者要求 2026-11-06）----

def test_stop_oe_deck_returns_to_original_page(state, make_pdf, config):
    a = state.deck
    state.goto(1)
    state.play_summary(make_pdf(["OE cover", "OE body"], folder=config.folder("紀錄") / "OE簡報", name="oe.pdf"),
                       kind="oe")
    assert state.stop_oe_deck() is True
    assert state.deck is a and state.page == 1 and state.presenting and state.temp_kind is None


def test_stop_oe_deck_leaves_other_decks_alone(state, make_pdf, config):
    # OE 簡報已經收回（或在播摘要簡報）時再按：不能把原本的簡報也關掉
    assert state.stop_oe_deck() is False and state.presenting
    state.play_summary(make_pdf(["S"], folder=config.folder("紀錄") / "摘要簡報", name="s.pdf"))
    assert state.stop_oe_deck() is False and state.temp_kind == "summary"


# ---- 播放時才把 pptx 轉成 PDF（開機不轉） ----

def _pptx(folder, name, age=0.0):
    import os

    path = folder / name
    path.write_bytes(b"pptx")
    t = time.time() - 100 + age
    os.utime(path, (t, t))
    return path


def _pdf_maker(make_pdf, gate=None, calls=None):
    """假轉檔：寫出同名的真 PDF；gate 有給就等它放行（模擬轉很久）。"""
    def convert(src):
        if calls is not None:
            calls.append(src.name)
        if gate is not None:
            assert gate.wait(5)
        return make_pdf(["Converted " + src.stem], folder=src.parent, name=src.stem + ".pdf")
    return convert


def _finish(state):
    state._convert_thread.join(5)
    assert not state._convert_thread.is_alive()


def test_request_deck_plays_a_ready_pdf_at_once(state_factory, make_pdf):
    st = state_factory()
    pdf = make_pdf(["A"], folder=st.config.folder("簡報"), name="A.pdf")
    assert st.request_deck(pdf, play=True) == "ready"
    assert st.presenting and st.deck.name == "A" and st.deck_converting is None


def test_request_deck_uses_the_pdf_of_an_already_converted_pptx(state_factory, make_pdf):
    st = state_factory()
    folder = st.config.folder("簡報")
    src = _pptx(folder, "A.pptx")
    make_pdf(["A"], folder=folder, name="A.pdf")  # 比 pptx 新
    st.deck_converter = lambda s: pytest.fail("不該再轉")
    assert st.request_deck(src, play=True) == "ready" and st.deck.name == "A"


def test_request_deck_converts_a_pptx_then_plays_it(state_factory, make_pdf):
    import threading

    st = state_factory()
    folder = st.config.folder("簡報")
    src = _pptx(folder, "New.pptx")
    gate = threading.Event()
    st.deck_converter = _pdf_maker(make_pdf, gate)
    assert st.request_deck(src, play=True) == "converting"
    assert st.snapshot("host")["deck_converting"] == {"name": "New", "play": True}
    assert not st.presenting
    gate.set()
    _finish(st)
    assert st.presenting and st.deck.name == "New" and st.deck_converting is None
    assert folder / "New.pdf" in st.deck_paths and src not in st.deck_paths


def test_request_deck_open_only_then_play_while_converting(state_factory, make_pdf):
    """主持人畫面：下拉選單選了還沒轉的 pptx 先轉；轉檔中按「播放簡報」，轉完就直接播放。"""
    import threading

    st = state_factory()
    src = _pptx(st.config.folder("簡報"), "New.pptx")
    gate = threading.Event()
    calls = []
    st.deck_converter = _pdf_maker(make_pdf, gate, calls)
    assert st.request_deck(src, play=False) == "converting"
    assert st.request_deck(src, play=True) == "converting"  # 同一份：不重轉，改成轉完播放
    gate.set()
    _finish(st)
    assert calls == ["New.pptx"] and st.presenting and st.deck.name == "New"


def test_request_deck_open_only_does_not_project(state_factory, make_pdf):
    st = state_factory()
    src = _pptx(st.config.folder("簡報"), "New.pptx")
    st.deck_converter = _pdf_maker(make_pdf)
    assert st.request_deck(src, play=False) == "converting"
    _finish(st)
    assert st.deck.name == "New" and not st.presenting


def test_request_deck_one_conversion_at_a_time(state_factory, make_pdf):
    import threading

    st = state_factory()
    folder = st.config.folder("簡報")
    a, b = _pptx(folder, "A.pptx"), _pptx(folder, "B.pptx")
    gate = threading.Event()
    st.deck_converter = _pdf_maker(make_pdf, gate)
    assert st.request_deck(a, play=True) == "converting"
    assert st.request_deck(b, play=True) == "busy"
    gate.set()
    _finish(st)
    assert st.deck.name == "A"


def test_request_deck_without_powerpoint_is_an_error(state_factory):
    st = state_factory()
    src = _pptx(st.config.folder("簡報"), "New.pptx")
    assert st.request_deck(src, play=True) == "error"
    assert not st.presenting and any("New.pptx" in w for w in st.warnings)


def test_request_deck_conversion_failure_warns_and_stays(state_factory, make_pdf):
    st = state_factory()
    folder = st.config.folder("簡報")
    keep = make_pdf(["Keep"], folder=folder, name="Keep.pdf")
    st.play(keep)
    src = _pptx(folder, "New.pptx", age=50)

    def broken(s):
        raise RuntimeError("PowerPoint busy")

    st.deck_converter = broken
    assert st.request_deck(src, play=True) == "converting"
    _finish(st)
    assert st.deck.name == "Keep" and st.presenting and st.deck_converting is None
    assert any("New.pptx" in w and "busy" in w for w in st.warnings)


def test_newest_deck_entry_is_today_when_no_deck_is_open(state_factory):
    st = state_factory()
    folder = st.config.folder("簡報")
    new = _pptx(folder, "New.pptx", age=50)
    _pptx(folder, "Old.pptx")
    from xiaoshen.converter import list_deck_entries

    st.set_deck_paths(list_deck_entries(folder))
    assert st.deck is None and st.today_deck_path() == new


def test_refresh_deck_list_opens_newest_pdf_but_waits_for_a_pptx(state_factory, make_pdf):
    st = state_factory()
    folder = st.config.folder("簡報")
    make_pdf(["Old"], folder=folder, name="Old.pdf")
    new = _pptx(folder, "New.pptx", age=500)  # 最新的是還沒轉的 pptx：開機不轉、也不改開舊的
    st.refresh_deck_list()
    assert st.deck is None and st.deck_paths[0] == new
    make_pdf(["New"], folder=folder, name="New.pdf")  # 背景轉好了
    import os

    os.utime(folder / "New.pdf", (new.stat().st_mtime, new.stat().st_mtime))
    st.refresh_deck_list()
    assert st.deck.name == "New" and not st.presenting

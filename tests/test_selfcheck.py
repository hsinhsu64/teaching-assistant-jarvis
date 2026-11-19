from xiaoshen.launcher import pick_display_position
from xiaoshen.selfcheck import CheckResult, openevidence_result, run_checks


def by_name(results):
    return {c.name: c for c in results}


def test_checks_missing_root_is_fail(state_factory, tmp_path):
    st = state_factory(data_root=tmp_path / "none")
    r = by_name(run_checks(st.config, st))
    assert r["資料夾"].level == "fail"


def test_checks_missing_subfolder_warns(state_factory):
    st = state_factory()
    (st.config.folder("圖庫")).rmdir()
    r = by_name(run_checks(st.config, st))
    assert r["子資料夾"].level == "warn" and "圖庫" in r["子資料夾"].detail


def test_checks_no_deck_warns(state_factory):
    st = state_factory()
    r = by_name(run_checks(st.config, st))
    assert r["簡報"].level == "warn"


def test_checks_scanned_deck_warns(state_with_scanned_deck):
    st = state_with_scanned_deck
    r = by_name(run_checks(st.config, st))
    assert r["簡報"].level == "warn" and "掃描檔" in r["簡報"].detail


def test_checks_good_deck_ok(state):
    state.public_base = "https://x.trycloudflare.com"
    r = by_name(run_checks(state.config, state))
    assert r["簡報"].level == "ok" and r["手機作答"].level == "ok" and r["資料夾"].level == "ok"


def test_checks_bank_warnings(state):
    state.warnings.append("題庫：第 3 列：缺題目")
    r = by_name(run_checks(state.config, state, bank_warnings=["第 3 列：缺題目"]))
    assert r["題庫"].level == "warn" and "第 3 列" in r["題庫"].detail


def test_checks_no_tunnel_fail(state):
    r = by_name(run_checks(state.config, state))
    assert r["手機作答"].level == "fail"


def test_phone_row_pending_while_new_tunnel_is_confirmed(state):
    state.public_base = "https://x.trycloudflare.com"
    state.phone_pending = True
    r = by_name(run_checks(state.config, state))["手機作答"]
    assert r.level == "pending" and "確認" in r.detail and r.hint == ""


def test_check_exception_isolated(state):
    def boom():
        raise RuntimeError("net down")

    results = run_checks(state.config, state, oe_check=boom)
    assert any(c.name == "OpenEvidence" and c.level == "fail" for c in results)
    assert any(c.name == "資料夾" and c.level == "ok" for c in results)


def test_pick_display_position():
    rects = [(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]
    assert pick_display_position(rects, 1) == ((1920, 0), None)
    pos, warn = pick_display_position(rects[:1], 1)
    assert pos == (0, 0) and "主螢幕" in warn


def test_bank_rows_for_unknown_deck_warn(state, bank_question):
    from xiaoshen.quiz import Question

    state.bank[("Missing", 1)] = Question("Missing-p1", "s", ["A", "B"], 0)
    r = by_name(run_checks(state.config, state))
    assert r["題庫"].level == "warn" and "Missing" in r["題庫"].detail


def _confirm(state, http_get, not_before=10.0, give_up_at=120.0):
    """用假時鐘跑 confirm_phone_link，回傳 (結果, 每次查詢的時間點, sleep 的秒數)。"""
    import asyncio

    from xiaoshen.selfcheck import confirm_phone_link

    now, asked, slept = [0.0], [], []

    async def sleep(s):
        slept.append(s)
        now[0] += s

    def get(u):
        asked.append(now[0])
        return http_get(u)

    ok = asyncio.run(confirm_phone_link(state, get, not_before=not_before, give_up_at=give_up_at,
                                        clock=lambda: now[0], sleep=sleep))
    return ok, asked, slept


def _pending_phone(state):
    state.public_base = "https://x.trycloudflare.com"
    state.phone_pending = True
    state.checks = [CheckResult("手機作答", "pending", "確認中").to_dict()]


def test_confirm_phone_link_waits_before_first_lookup(state):
    _pending_phone(state)
    ok, asked, _ = _confirm(state, lambda u: 200)
    assert ok and asked == [10.0]  # 新網址太早查，有些網路會把「查不到」記一陣子


def test_confirm_phone_link_retries_every_3s_then_turns_green(state):
    _pending_phone(state)
    calls = []
    state.on_change = lambda: calls.append(1)
    answers = iter([OSError("getaddrinfo failed"), 502, 200])

    def flaky(u):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    ok, asked, _ = _confirm(state, flaky)
    assert ok and asked == [10.0, 13.0, 16.0]
    row = state.checks[0]
    assert row["level"] == "ok" and row["detail"] == state.public_base
    assert not state.phone_pending and state.phone_url() and calls


def test_confirm_phone_link_gives_up_red_after_deadline(state):
    _pending_phone(state)
    ok, asked, _ = _confirm(state, lambda u: 502)
    assert not ok and asked[-1] <= 120.0 and len(asked) > 30
    row = state.checks[0]
    assert row["level"] == "fail" and "502" in row["detail"] and "30 秒" in row["hint"]
    assert not state.phone_pending and state.phone_url() is None


def test_confirm_phone_link_without_tunnel_does_nothing(state):
    ok, asked, _ = _confirm(state, lambda u: 200)
    assert not ok and asked == []


def test_conversion_check_included(state):
    state.conversion_check = {"name": "簡報轉檔", "level": "warn", "detail": "x"}
    r = by_name(run_checks(state.config, state))
    assert r["簡報轉檔"].level == "warn"


# ---- Phase 2 ----

def test_ai_model_check_ok_warn_fail(state):
    from xiaoshen.llm import LLMError

    r = by_name(run_checks(state.config, state, llm_check=lambda: state.config.model_answer))
    assert r["AI 文字模型"].level == "ok"
    r = by_name(run_checks(state.config, state, llm_check=lambda: "gemini-3.5-flash"))
    assert r["AI 文字模型"].level == "warn" and "gemini-3.5-flash" in r["AI 文字模型"].detail

    def boom():
        raise LLMError("找不到 GEMINI_API_KEY")

    r = by_name(run_checks(state.config, state, llm_check=boom))
    assert r["AI 文字模型"].level == "fail" and "GEMINI_API_KEY" in r["AI 文字模型"].detail


def test_openevidence_check(state):
    r = by_name(run_checks(state.config, state, oe_check=lambda: True))
    assert r["OpenEvidence"].level == "ok"
    r = by_name(run_checks(state.config, state, oe_check=lambda: False))
    assert r["OpenEvidence"].level == "warn" and "搜尋路線停用" in r["OpenEvidence"].detail


def test_references_check(state, make_pdf):
    from xiaoshen.library import Library

    folder = state.config.folder("參考資料")
    lib = Library(folder)
    r = by_name(run_checks(state.config, state, library=lib, library_warnings=[]))
    assert r["參考資料"].level == "warn" and "沒有" in r["參考資料"].detail
    make_pdf(["text " * 5], folder=folder, name="A.pdf")
    make_pdf(["", ""], folder=folder, name="Scan.pdf")
    warns = lib.refresh()
    r = by_name(run_checks(state.config, state, library=lib, library_warnings=warns))
    assert r["參考資料"].level == "warn" and "2 份" in r["參考資料"].detail and "Scan" in r["參考資料"].detail
    (folder / "Scan.pdf").unlink()
    warns = lib.refresh()
    r = by_name(run_checks(state.config, state, library=lib, library_warnings=warns))
    assert r["參考資料"].level == "ok" and "1 份" in r["參考資料"].detail


def test_ai_checks_absent_by_default(state):
    names = set(by_name(run_checks(state.config, state)))
    assert not names & {"AI 文字模型", "OpenEvidence", "參考資料"}


def test_oe_blocked_answer_turns_check_warn(state):
    from fakes import FakeLLM
    from xiaoshen.jobs import JobRunner
    from xiaoshen.library import Library
    from xiaoshen.oe_client import OEBlocked

    state.checks = [{"name": "OpenEvidence", "level": "ok", "detail": "登入有效"}]

    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    jr = JobRunner(state, FakeLLM({}), state.config, Library(state.config.folder("參考資料")), ask=blocked)
    jr.ask("SRNS?", "openevidence", [])
    jr.wait_idle()
    assert state.checks[0]["level"] == "warn" and "重新驗證" in state.checks[0]["detail"]


# ---- Phase 3 ----

def test_voice_checks(state):
    def boom():
        raise RuntimeError("no device")

    ok = by_name(run_checks(state.config, state, mic_check=lambda: 0.02,
                            speaker_check=lambda: True, wake_check=lambda: True, live_check=lambda: 0.8))
    assert ok["麥克風"].level == "ok" and ok["喇叭"].level == "ok"
    assert ok["喚醒詞"].level == "ok" and ok["語音模型"].level == "ok" and "0.8" in ok["語音模型"].detail
    bad = by_name(run_checks(state.config, state, mic_check=boom,
                             speaker_check=lambda: False, wake_check=lambda: False, live_check=boom))
    assert bad["麥克風"].level == "fail" and "語音停用" in bad["麥克風"].detail
    assert bad["喇叭"].level == "warn"
    assert bad["喚醒詞"].level == "fail" and bad["語音模型"].level == "fail"
    assert "簡報與作答照常" in bad["語音模型"].detail
    fell = by_name(run_checks(state.config, state,
                              wake_check=lambda: ("賈維斯", "喚醒詞「小」無法使用（喚醒詞要 2 到 4 個字），已改用預設喚醒詞「賈維斯」")))
    assert fell["喚醒詞"].level == "warn" and "請說「賈維斯」" in fell["喚醒詞"].detail
    assert "「小」無法使用" in fell["喚醒詞"].detail
    quiet = by_name(run_checks(state.config, state, mic_check=lambda: 0.0))
    assert quiet["麥克風"].level == "warn"


def test_voice_checks_absent_by_default(state):
    names = set(by_name(run_checks(state.config, state)))
    assert not names & {"麥克風", "喇叭", "喚醒詞", "語音模型"}


# ---- 開機檢查 OpenEvidence 瀏覽器分頁（使用者要求 2026-11-06）----

def _oe(state, logged_in, tab):
    return by_name(run_checks(state.config, state,
                              oe_check=lambda: logged_in, oe_tab_check=lambda: tab))["OpenEvidence"]


def test_oe_tab_connected_and_logged_in(state):
    r = _oe(state, True, True)
    assert r.level == "ok" and "瀏覽器" in r.detail


def test_oe_tab_missing_reminds_to_open_browser(state):
    r = _oe(state, True, False)
    assert r.level == "warn" and "請打開瀏覽器" in r.detail and "已登入的 OpenEvidence 分頁" in r.detail


def test_oe_tab_connected_but_not_logged_in(state):
    r = _oe(state, False, True)
    assert r.level == "warn" and "登入" in r.detail


def test_oe_nothing_works(state):
    r = _oe(state, False, False)
    assert r.level == "warn" and "搜尋路線停用" in r.detail and "請打開瀏覽器" in r.detail


# ---- 黃燈那一列「重新檢查」（使用者要求 2026-11-06）----

def test_recheck_endpoint_updates_openevidence_line(state):
    from fastapi.testclient import TestClient

    from xiaoshen.hub import Hub
    from xiaoshen.server import create_control_app

    state.checks = [CheckResult("OpenEvidence", "warn", "請打開瀏覽器").to_dict()]
    calls = []

    def recheck():
        calls.append(1)
        return openevidence_result(logged_in=True, tab=True)

    state.oe_recheck = recheck
    with TestClient(create_control_app(state, Hub())) as c:
        r = c.post("/api/check/openevidence").json()
    assert calls == [1] and r["level"] == "ok"
    line = next(x for x in state.snapshot("host")["checks"] if x["name"] == "OpenEvidence")
    assert line["level"] == "ok" and "瀏覽器" in line["detail"]


def test_recheck_endpoint_without_handler(state):
    from fastapi.testclient import TestClient

    from xiaoshen.hub import Hub
    from xiaoshen.server import create_control_app

    state.oe_recheck = None
    with TestClient(create_control_app(state, Hub())) as c:
        assert c.post("/api/check/openevidence").status_code == 503


def test_mac_mic_silent_hint(state, monkeypatch):
    from xiaoshen import selfcheck
    monkeypatch.setattr(selfcheck, "IS_MAC", True)
    r = by_name(run_checks(state.config, state, mic_check=lambda: 0.0))["麥克風"]
    assert r.level == "warn" and r.detail == "麥克風沒有聲音（靜音或沒插好？）"
    assert "隱私權與安全性" in r.hint and "終端機" in r.hint
    monkeypatch.setattr(selfcheck, "IS_MAC", False)
    r = by_name(run_checks(state.config, state, mic_check=lambda: 0.0))["麥克風"]
    assert r.detail == "麥克風沒有聲音（靜音或沒插好？）"
    assert "讓桌面應用程式存取您的麥克風" in r.hint and "終端機" not in r.hint


# ---- 紅燈／黃燈的「怎麼修」協助訊息 ----

def _raise(exc):
    def f(*a):
        raise exc
    return f


def test_ok_results_have_no_hint_and_dict_carries_hint(state):
    state.public_base = "https://x.trycloudflare.com"
    results = run_checks(state.config, state, mic_check=lambda: 0.01,
                         speaker_check=lambda: True, wake_check=lambda: True, live_check=lambda: 0.5)
    assert all(c.hint == "" for c in results if c.level == "ok")
    assert CheckResult("x", "fail", "d", "h").to_dict() == {"name": "x", "level": "fail", "detail": "d", "hint": "h"}


def test_every_problem_has_a_hint_except_openevidence(state_factory, make_pdf, tmp_path):
    from xiaoshen.library import Library

    st = state_factory(data_root=tmp_path / "none")
    st.conversion_check = {"name": "簡報轉檔", "level": "warn", "detail": "轉檔失敗：A（會自動重試）"}
    lib_dir = tmp_path / "refs"
    lib_dir.mkdir()
    results = run_checks(st.config, st, bank_warnings=["第 3 列：缺題目"],
                         llm_check=_raise(RuntimeError("down")), oe_check=lambda: False,
                         library=Library(lib_dir), mic_check=_raise(OSError("no device")),
                         speaker_check=lambda: False, wake_check=lambda: False,
                         live_check=_raise(RuntimeError("down")))
    problems = [c for c in results if c.level != "ok"]
    assert {c.name for c in problems} >= {"資料夾", "子資料夾", "簡報", "題庫", "簡報轉檔", "手機作答", "參考資料",
                                          "AI 文字模型", "麥克風", "喇叭", "喚醒詞", "語音模型", "OpenEvidence"}
    # OpenEvidence 原本就有說明；沒有參考資料是正常用法：這兩列不加「怎麼修」
    assert sorted(c.name for c in problems if not c.hint) == ["OpenEvidence", "參考資料"]


def test_mic_named_device_missing_points_to_settings(state):
    from dataclasses import replace

    config = replace(state.config, audio_device="USBMIC")
    r = by_name(run_checks(config, state,
                           mic_check=_raise(RuntimeError("找不到名稱含「USBMIC」的麥克風"))))["麥克風"]
    assert r.level == "fail" and "USBMIC" in r.detail
    assert "重新啟動" in r.hint and "「設定」" in r.hint and "電腦預設" in r.hint


def test_mic_other_error_hint_per_platform(state, monkeypatch):
    from xiaoshen import selfcheck

    monkeypatch.setattr(selfcheck, "IS_MAC", False)
    r = by_name(run_checks(state.config, state, mic_check=_raise(OSError("x"))))["麥克風"]
    assert "系統 → 音效 → 輸入" in r.hint and "讓桌面應用程式存取您的麥克風" in r.hint
    monkeypatch.setattr(selfcheck, "IS_MAC", True)
    r = by_name(run_checks(state.config, state, mic_check=_raise(OSError("x"))))["麥克風"]
    assert "終端機" in r.hint and "Windows" not in r.hint


def test_speaker_missing_points_to_settings(state):
    r = by_name(run_checks(state.config, state, speaker_check=lambda: False))["喇叭"]
    assert "「設定」" in r.hint and "電腦預設" in r.hint


def test_network_hints_mention_hotspot_and_key_script(state, monkeypatch):
    from xiaoshen import selfcheck

    for mac, script in ((False, "set-key.bat"), (True, "set-key.command")):
        monkeypatch.setattr(selfcheck, "IS_MAC", mac)
        r = by_name(run_checks(state.config, state, llm_check=_raise(RuntimeError("x")),
                               live_check=_raise(RuntimeError("x"))))
        for name in ("AI 文字模型", "語音模型"):
            assert "手機熱點" in r[name].hint and script in r[name].hint
    assert "手機熱點" in r["手機作答"].hint  # 沒有對外通道


def test_folder_hints(state_factory, tmp_path):
    st = state_factory(data_root=tmp_path / "none")
    assert "data_root" in by_name(run_checks(st.config, st))["資料夾"].hint
    st = state_factory()
    (st.config.folder("圖庫")).rmdir()
    r = by_name(run_checks(st.config, st))
    assert str(st.config.data_root) in r["子資料夾"].hint
    assert "「簡報」資料夾" in r["簡報"].hint


def test_scanned_deck_and_wake_hints(state_with_scanned_deck):
    st = state_with_scanned_deck
    r = by_name(run_checks(st.config, st, wake_check=lambda: False))
    assert "匯出" in r["簡報"].hint and "PDF" in r["簡報"].hint
    assert "安裝指令" in r["喚醒詞"].hint


def test_crashed_check_hint_points_to_diagnostics(state):
    r = by_name(run_checks(state.config, state, wake_check=_raise(ValueError("bug"))))
    assert r["喚醒詞"].level == "fail" and "診斷包" in r["喚醒詞"].hint


def test_network_checks_run_at_the_same_time(state):
    """AI、OpenEvidence、語音模型各自要連網路：同時檢查，總時間約等於最慢的那一項。"""
    import threading

    meet = threading.Barrier(3, timeout=5)  # 依序執行的話，第一項就會等不到另外兩項而失敗

    def arrive(value):
        def check():
            meet.wait()
            return value
        return check

    r = by_name(run_checks(state.config, state, llm_check=arrive(state.config.model_answer),
                           oe_check=arrive(True), oe_tab_check=lambda: True, live_check=arrive(1.0)))
    assert r["AI 文字模型"].level == "ok" and r["OpenEvidence"].level == "ok" and r["語音模型"].level == "ok"


def test_audio_device_checks_stay_one_at_a_time(state):
    """麥克風、喇叭、喚醒詞都用到音效裝置：仍然一項一項來，不互相搶裝置。"""
    import threading
    import time as _time

    lock, active, peak = threading.Lock(), [0], [0]

    def device(value):
        def check():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.05)
            with lock:
                active[0] -= 1
            return value
        return check

    r = by_name(run_checks(state.config, state, mic_check=device(0.02), speaker_check=device(True),
                           wake_check=device(("賈維斯", None))))
    assert peak[0] == 1
    assert r["麥克風"].level == "ok" and r["喇叭"].level == "ok" and r["喚醒詞"].level == "ok"


def test_results_keep_the_listed_order(state):
    results = run_checks(state.config, state, llm_check=lambda: state.config.model_answer, oe_check=lambda: True,
                         mic_check=lambda: 0.02, speaker_check=lambda: True, wake_check=lambda: ("賈維斯", None),
                         live_check=lambda: 1.0)
    assert [c.name for c in results] == ["資料夾", "子資料夾", "簡報", "題庫", "手機作答", "AI 文字模型",
                                         "OpenEvidence", "麥克風", "喇叭", "喚醒詞", "語音模型"]


def test_deck_row_ok_when_newest_is_an_unconverted_pptx(state_factory):
    st = state_factory()
    (st.config.folder("簡報") / "New.pptx").write_bytes(b"pptx")
    r = by_name(run_checks(st.config, st))["簡報"]
    assert r.level == "ok" and "New" in r.detail and "背景" in r.detail


def test_bank_knows_decks_that_are_still_pptx(state_factory, bank_question):
    from xiaoshen.quiz import Question

    st = state_factory()
    (st.config.folder("簡報") / "New.pptx").write_bytes(b"pptx")
    st.bank[("New", 1)] = Question("New-p1", "s", ["A", "B"], 0)
    assert by_name(run_checks(st.config, st))["題庫"].level == "ok"


def test_deck_list_is_pdf_only_when_auto_convert_is_off(state_factory, make_pdf):
    st = state_factory(auto_convert_pptx=False)
    folder = st.config.folder("簡報")
    (folder / "New.pptx").write_bytes(b"pptx")
    pdf = make_pdf(["A"], folder=folder, name="A.pdf")
    assert st.list_deck_files() == [pdf]

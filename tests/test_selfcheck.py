from xiaoshen.launcher import pick_display_position
from xiaoshen.selfcheck import CheckResult, openevidence_result, run_checks


def by_name(results):
    return {c.name: c for c in results}


def test_checks_missing_root_is_fail(state_factory, tmp_path):
    st = state_factory(data_root=tmp_path / "none")
    r = by_name(run_checks(st.config, st, http_get=lambda u: 200))
    assert r["資料夾"].level == "fail"


def test_checks_missing_subfolder_warns(state_factory):
    st = state_factory()
    (st.config.folder("圖庫")).rmdir()
    r = by_name(run_checks(st.config, st, http_get=lambda u: 200))
    assert r["子資料夾"].level == "warn" and "圖庫" in r["子資料夾"].detail


def test_checks_no_deck_warns(state_factory):
    st = state_factory()
    r = by_name(run_checks(st.config, st, http_get=lambda u: 200))
    assert r["簡報"].level == "warn"


def test_checks_scanned_deck_warns(state_with_scanned_deck):
    st = state_with_scanned_deck
    r = by_name(run_checks(st.config, st, http_get=lambda u: 200))
    assert r["簡報"].level == "warn" and "掃描檔" in r["簡報"].detail


def test_checks_good_deck_ok(state):
    state.public_base = "https://x.trycloudflare.com"
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200))
    assert r["簡報"].level == "ok" and r["手機作答"].level == "ok" and r["資料夾"].level == "ok"


def test_checks_bank_warnings(state):
    state.warnings.append("題庫：第 3 列：缺題目")
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, bank_warnings=["第 3 列：缺題目"]))
    assert r["題庫"].level == "warn" and "第 3 列" in r["題庫"].detail


def test_checks_no_tunnel_fail(state):
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200))
    assert r["手機作答"].level == "fail"


def test_checks_phone_unreachable_fail(state):
    state.public_base = "https://x.trycloudflare.com"
    seen = []
    r = by_name(run_checks(state.config, state, http_get=lambda u: seen.append(u) or 502))
    assert r["手機作答"].level == "fail" and seen == ["https://x.trycloudflare.com/healthz"]


def test_check_exception_isolated(state):
    def boom(u):
        raise RuntimeError("net down")

    state.public_base = "https://x.trycloudflare.com"
    results = run_checks(state.config, state, http_get=boom)
    assert any(c.name == "手機作答" and c.level == "fail" for c in results)
    assert any(c.name == "資料夾" and c.level == "ok" for c in results)


def test_pick_display_position():
    rects = [(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]
    assert pick_display_position(rects, 1) == ((1920, 0), None)
    pos, warn = pick_display_position(rects[:1], 1)
    assert pos == (0, 0) and "主螢幕" in warn


def test_bank_rows_for_unknown_deck_warn(state, bank_question):
    from xiaoshen.quiz import Question

    state.bank[("Missing", 1)] = Question("Missing-p1", "s", ["A", "B"], 0)
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200))
    assert r["題庫"].level == "warn" and "Missing" in r["題庫"].detail


def test_phone_check_retries_while_new_tunnel_dns_propagates(state):
    state.public_base = "https://x.trycloudflare.com"
    attempts = []

    def flaky(u):
        attempts.append(u)
        if len(attempts) < 3:
            raise OSError("getaddrinfo failed")
        return 200

    r = by_name(run_checks(state.config, state, http_get=flaky, phone_retries=5, retry_delay=0))
    assert r["手機作答"].level == "ok" and len(attempts) == 3


def test_conversion_check_included(state):
    state.conversion_check = {"name": "簡報轉檔", "level": "warn", "detail": "x"}
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200))
    assert r["簡報轉檔"].level == "warn"


# ---- Phase 2 ----

def test_ai_model_check_ok_warn_fail(state):
    from xiaoshen.llm import LLMError

    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, llm_check=lambda: state.config.model_answer))
    assert r["AI 文字模型"].level == "ok"
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, llm_check=lambda: "gemini-3.5-flash"))
    assert r["AI 文字模型"].level == "warn" and "gemini-3.5-flash" in r["AI 文字模型"].detail

    def boom():
        raise LLMError("找不到 GEMINI_API_KEY")

    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, llm_check=boom))
    assert r["AI 文字模型"].level == "fail" and "GEMINI_API_KEY" in r["AI 文字模型"].detail


def test_openevidence_check(state):
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, oe_check=lambda: True))
    assert r["OpenEvidence"].level == "ok"
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, oe_check=lambda: False))
    assert r["OpenEvidence"].level == "warn" and "搜尋路線停用" in r["OpenEvidence"].detail


def test_references_check(state, make_pdf):
    from xiaoshen.library import Library

    folder = state.config.folder("參考資料")
    lib = Library(folder)
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, library=lib, library_warnings=[]))
    assert r["參考資料"].level == "warn" and "沒有" in r["參考資料"].detail
    make_pdf(["text " * 5], folder=folder, name="A.pdf")
    make_pdf(["", ""], folder=folder, name="Scan.pdf")
    warns = lib.refresh()
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, library=lib, library_warnings=warns))
    assert r["參考資料"].level == "warn" and "2 份" in r["參考資料"].detail and "Scan" in r["參考資料"].detail
    (folder / "Scan.pdf").unlink()
    warns = lib.refresh()
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, library=lib, library_warnings=warns))
    assert r["參考資料"].level == "ok" and "1 份" in r["參考資料"].detail


def test_ai_checks_absent_by_default(state):
    names = set(by_name(run_checks(state.config, state, http_get=lambda u: 200)))
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

    ok = by_name(run_checks(state.config, state, http_get=lambda u: 200, mic_check=lambda: 0.02,
                            speaker_check=lambda: True, wake_check=lambda: True, live_check=lambda: 0.8))
    assert ok["麥克風"].level == "ok" and ok["喇叭"].level == "ok"
    assert ok["喚醒詞"].level == "ok" and ok["語音模型"].level == "ok" and "0.8" in ok["語音模型"].detail
    bad = by_name(run_checks(state.config, state, http_get=lambda u: 200, mic_check=boom,
                             speaker_check=lambda: False, wake_check=lambda: False, live_check=boom))
    assert bad["麥克風"].level == "fail" and "語音停用" in bad["麥克風"].detail
    assert bad["喇叭"].level == "warn"
    assert bad["喚醒詞"].level == "fail" and bad["語音模型"].level == "fail"
    assert "簡報與作答照常" in bad["語音模型"].detail
    fell = by_name(run_checks(state.config, state, http_get=lambda u: 200,
                              wake_check=lambda: ("賈維斯", "喚醒詞「小」無法使用（喚醒詞要 2 到 4 個字），已改用預設喚醒詞「賈維斯」")))
    assert fell["喚醒詞"].level == "warn" and "請說「賈維斯」" in fell["喚醒詞"].detail
    assert "「小」無法使用" in fell["喚醒詞"].detail
    quiet = by_name(run_checks(state.config, state, http_get=lambda u: 200, mic_check=lambda: 0.0))
    assert quiet["麥克風"].level == "warn"


def test_voice_checks_absent_by_default(state):
    names = set(by_name(run_checks(state.config, state, http_get=lambda u: 200)))
    assert not names & {"麥克風", "喇叭", "喚醒詞", "語音模型"}


# ---- 開機檢查 OpenEvidence 瀏覽器分頁（使用者要求 2026-11-06）----

def _oe(state, logged_in, tab):
    return by_name(run_checks(state.config, state, http_get=lambda u: 200,
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
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, mic_check=lambda: 0.0))["麥克風"]
    assert r.level == "warn" and "隱私權與安全性" in r.detail and "麥克風沒有聲音" in r.detail
    monkeypatch.setattr(selfcheck, "IS_MAC", False)
    r = by_name(run_checks(state.config, state, http_get=lambda u: 200, mic_check=lambda: 0.0))["麥克風"]
    assert r.detail == "麥克風沒有聲音（靜音或沒插好？）"

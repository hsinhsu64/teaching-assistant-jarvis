"""語音問題含英文醫學用語：搜尋前先確認再搜尋（spec §5.4）。"""

import pytest
from fastapi.testclient import TestClient

from xiaoshen.hub import Hub
from xiaoshen.jobs import JobRunner
from xiaoshen.library import Library
from xiaoshen.live_backend import ToolCall
from xiaoshen.server import create_control_app
from xiaoshen.vocabulary import deck_context
from xiaoshen.voice_tools import TOOLS, system_instruction, VoiceTools, needs_confirm

from fakes import FakeLLM

FIXED = {"question": "Bartter syndrome 的治療？", "unsure": False, "clarify_zh": ""}


@pytest.fixture
def st(state_factory, make_pdf):
    s = state_factory()
    s.play(make_pdf(["Intro " * 5], folder=s.config.folder("簡報"), name="D.pdf"))
    return s


@pytest.fixture
def vt(st):
    lib = Library(st.config.folder("參考資料"))
    lib.refresh()
    tools = VoiceTools(st, JobRunner(st, FakeLLM(FIXED), st.config, lib), lib)
    tools.sent = []
    tools.confirmed = []

    def spy(text, route, refs, spoken=False, confirmed=False):
        tools.sent.append((text, route, refs, spoken))
        tools.confirmed.append(confirmed)
        return ""  # 不真的查；_ask 收到空字串會當成被擋
    tools.jobs.ask = spy
    return tools


def ask(vt, **args):
    return vt.handle(ToolCall("c1", "ask", args))


def test_needs_confirm_only_for_english_words():
    assert needs_confirm("butter syndrome 怎麼治療？")
    assert needs_confirm("小朋友 SRNS 用什麼藥")
    assert not needs_confirm("兒童腎病症候群的一線治療？")
    assert not needs_confirm("查 OpenEvidence：腎病症候群復發怎麼辦")  # 資料庫名不算
    assert not needs_confirm("用 PubMed 查，AI 怎麼看 OE 的結果")


def test_english_question_waits_for_confirmation(vt, st):
    vt.hear("小森，查 butter syndrome 的治療")
    r = ask(vt, question="butter syndrome 的治療？", source="openevidence")
    assert r["status"] == "confirm_query"
    assert r["message"] == "要查的是：Bartter syndrome 的治療？，對嗎？"  # 先校正聽錯的名詞再念
    assert vt.sent == []  # 還沒查
    assert st.snapshot("display")["voice_confirm"] == {"question": "Bartter syndrome 的治療？"}
    assert st.snapshot("host")["voice_confirm"]["question"] == "Bartter syndrome 的治療？"


def test_yes_searches_confirmed_text_without_more_fixing(vt, st):
    vt.hear("查 butter syndrome")
    ask(vt, question="butter syndrome 的治療？")
    vt.hear("對")
    ask(vt, question="butter syndrome 的治療？", confirmed=True)  # 模型可能帶回原句
    assert vt.sent == [("Bartter syndrome 的治療？", "openevidence", [], False)]  # spoken=False：不再自動改字
    assert vt.confirmed == [True]  # 確認過就不再反問（2026-11-06）
    assert st.snapshot("display")["voice_confirm"] is None


def test_confirm_needs_new_speech(vt):
    vt.hear("查 butter syndrome")
    ask(vt, question="butter syndrome 的治療？")
    r = ask(vt, question="Bartter syndrome 的治療？", confirmed=True)  # 同一句話裡模型自己說「對」
    assert r["status"] == "confirm_query" and vt.sent == []


def test_correction_starts_new_confirmation(vt, st):
    vt.hear("查 butter syndrome")
    ask(vt, question="butter syndrome 的治療？")
    vt.hear("不是，是 Gitelman")
    vt.jobs.llm.responses = [{"question": "Gitelman syndrome 的治療？", "unsure": False, "clarify_zh": ""}]
    r = ask(vt, question="Gitelman syndrome 的治療？", confirmed=True)  # 名詞不同：重新確認
    assert r["status"] == "confirm_query" and "Gitelman" in r["message"] and vt.sent == []
    assert st.snapshot("display")["voice_confirm"]["question"] == "Gitelman syndrome 的治療？"


def test_confirmation_expires(vt, st, monkeypatch):
    import xiaoshen.voice_tools as m
    vt.hear("查 butter syndrome")
    ask(vt, question="butter syndrome 的治療？")
    later = m.time.time() + m.CONFIRM_TTL + 1
    monkeypatch.setattr(m.time, "time", lambda: later)
    vt.hear("對")
    r = ask(vt, question="Bartter syndrome 的治療？", confirmed=True)
    assert r["status"] == "expired" and "再說一次" in r["message"] and vt.sent == []
    assert st.snapshot("display")["voice_confirm"] is None


def test_chinese_question_searches_right_away(vt):
    vt.hear("兒童腎病症候群的一線治療")
    ask(vt, question="兒童腎病症候群的一線治療？", source="openevidence")
    assert vt.sent == [("兒童腎病症候群的一線治療？", "openevidence", [], True)]
    assert vt.confirmed == [False]  # 沒經過確認：OpenEvidence 前照舊可能反問


def test_pubmed_also_confirms(vt):
    vt.hear("查 PubMed rituximab")
    r = ask(vt, question="rituximab 用於 SDNS", source="pubmed")
    assert r["status"] == "confirm_query" and vt.sent == []
    vt.hear("對")
    ask(vt, question="rituximab 用於 SDNS", source="pubmed", confirmed=True)
    assert vt.sent[0][1] == "pubmed"


def test_deck_questions_are_not_confirmed(vt):
    vt.hear("這份簡報的 SRNS 重點")
    ask(vt, question="SRNS 重點", source="deck")
    assert vt.sent == [("SRNS 重點", "deck", [], True)]


def test_phi_blocked_before_confirmation(vt, st):
    vt.hear("病歷號 1234567 的 SRNS")
    r = ask(vt, question="病歷號 1234567 的 SRNS 治療")
    assert r["status"] == "blocked" and st.snapshot("display")["voice_confirm"] is None


def test_new_voice_session_is_told_about_pending_question(vt, st):
    vt.hear("查 butter syndrome")
    ask(vt, question="butter syndrome 的治療？")
    ctx = deck_context(st)()
    assert "Bartter syndrome 的治療？" in ctx and "confirmed" in ctx


def test_prompt_and_tool_mention_confirmation():
    params = next(t for t in TOOLS if t["name"] == "ask")["parameters"]["properties"]
    assert params["confirmed"]["type"] == "boolean"
    assert "confirm_query" in system_instruction() and "confirmed=true" in system_instruction()


@pytest.fixture
def client(vt, st):
    with TestClient(create_control_app(st, Hub(), vt.jobs)) as c:
        yield c


def test_host_confirms_edited_question(client, vt, st):
    vt.hear("查 butter syndrome")
    ask(vt, question="butter syndrome 的治療？")
    r = client.post("/api/voice/confirm", json={"question": "Bartter syndrome type 1 的治療？"})
    assert r.status_code == 200
    assert vt.sent == [("Bartter syndrome type 1 的治療？", "openevidence", [], False)]
    assert vt.confirmed == [True]
    assert st.snapshot("host")["voice_confirm"] is None


def test_host_cancels(client, vt, st):
    vt.hear("查 butter syndrome")
    ask(vt, question="butter syndrome 的治療？")
    assert client.post("/api/voice/confirm/cancel").status_code == 200
    assert st.snapshot("host")["voice_confirm"] is None
    assert client.post("/api/voice/confirm", json={"question": "x"}).status_code == 409  # 沒有待確認的

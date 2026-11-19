import pytest

from xiaoshen.jobs import JobRunner
from xiaoshen.library import Library
from xiaoshen.live_backend import ToolCall
from xiaoshen.phi import find_phi
from xiaoshen.voice_tools import TOOLS, system_instruction, VoiceTools

from fakes import FakeLLM

QUOTE = "We recommend calcineurin inhibitors as first-line therapy"


def answered(cards):
    return {"status": "answered", "summary_zh": "未驗證的總結", "clarify_zh": "", "cards": cards}


def card(point, quote=QUOTE, label="今天的簡報", page=2):
    return {"point_zh": point, "quote": quote, "source_label": label, "page": page}


@pytest.fixture
def deck_state(state_factory, make_pdf):
    st = state_factory()
    st.play(make_pdf(["Intro " * 5, QUOTE + " for SRNS.", "Case 3 " * 5],
                     folder=st.config.folder("簡報"), name="D.pdf"))  # 投影中（§6.0）
    return st


def make_tools(st, llm):
    lib = Library(st.config.folder("參考資料"))
    lib.refresh()
    return VoiceTools(st, JobRunner(st, llm, st.config, lib), lib)


def call(_tool, **args):
    return ToolCall("c1", _tool, args)


def test_tool_result_contains_only_verified_cards(deck_state):
    llm = FakeLLM(answered([card("CNI 一線"), card("也是真的"), card("捏造", quote="cyclophosphamide is preferred first-line")]))
    vt = make_tools(deck_state, llm)
    r = vt.handle(call("ask", question="SRNS 一線？", source="deck"))
    assert r["status"] == "answered"
    # 語音不唸出處（太花時間），出處只在投影上
    assert r["message"] == "找到 2 張字卡，已投影" and "cards" not in r  # 預設不念內容
    assert vt.handle(call("read_answer"))["message"] == "CNI 一線。也是真的"  # 要求才念，只念驗證過的
    assert "未驗證的總結" not in str(r)


def test_not_found_message(deck_state):
    vt = make_tools(deck_state, FakeLLM({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []}))
    r = vt.handle(call("ask", question="q", source="deck"))
    # 簡報沒有 → 自動改查 OpenEvidence；OE 也查不到時照實說
    assert r["status"] == "not_found" and r["cards"] == []
    assert r["message"].startswith("資料中沒有提到") and "OpenEvidence" in r["message"]


def test_phi_in_turn_blocks_ask(deck_state):
    llm = FakeLLM(answered([card("x")]))
    vt = make_tools(deck_state, llm)
    vt.turn_text = "病歷號 1234567 這個病人"
    r = vt.handle(call("ask", question="這個病人怎麼辦", source="deck"))
    assert r["status"] == "blocked" and llm.calls == []
    assert deck_state.voice_phi_alert
    r2 = vt.handle(call("make_quiz", n=3, source="deck"))
    assert r2["status"] == "blocked" and llm.calls == []
    vt.begin_turn()
    assert vt.turn_text == ""


def test_phi_simplified_transcript_forms():
    assert find_phi("病历号 654321 的小朋友")
    assert find_phi("12床的病人")
    assert find_phi("床号 7")


def test_ambiguous_reference_returns_candidates(deck_state, make_pdf):
    folder = deck_state.config.folder("參考資料")
    make_pdf(["text " * 5], folder=folder, name="KDIGO 2021 GN.pdf")
    make_pdf(["text " * 5], folder=folder, name="KDIGO 2025 NS.pdf")
    llm = FakeLLM(answered([card("x")]))
    vt = make_tools(deck_state, llm)
    r = vt.handle(call("ask", question="q", source="references", reference_name="KDIGO"))
    assert r["status"] == "choose" and sorted(r["candidates"]) == ["KDIGO 2021 GN", "KDIGO 2025 NS"]
    assert sorted(deck_state.ref_candidates) == ["KDIGO 2021 GN", "KDIGO 2025 NS"]
    assert llm.calls == []
    assert vt.handle(call("ask", question="q", source="references", reference_name="IPNA"))["status"] == "not_found"


def test_selected_reference_used_by_default(deck_state, make_pdf):
    folder = deck_state.config.folder("參考資料")
    make_pdf(["x " * 5, QUOTE + " in children."], folder=folder, name="KDIGO 2021 GN.pdf")
    make_pdf(["other text " * 5], folder=folder, name="IPNA 2020.pdf")
    llm = FakeLLM(answered([card("CNI", label="KDIGO 2021 GN")]))
    vt = make_tools(deck_state, llm)
    assert vt.handle(call("open_reference", name="kdigo"))["status"] == "ok"
    assert deck_state.voice_ref == "KDIGO 2021 GN"
    r = vt.handle(call("ask", question="q", source="references"))
    assert r["status"] == "answered"
    assert "[KDIGO 2021 GN 第 2 頁]" in llm.calls[0]["prompt"] and "IPNA" not in llm.calls[0]["prompt"]


def test_next_page(deck_state):
    vt = make_tools(deck_state, FakeLLM({}))
    # 一定要給一句照念的話：只回頁碼時，語音模型自己編句子，實測曾冒出韓文（2026-11-04 實機）
    assert vt.handle(call("next_page")) == {"status": "ok", "page": 2, "message": "已翻到第 2 頁"}
    assert deck_state.page == 1


def test_prompt_says_page_turn_is_silent():
    # 翻頁成功不出聲；語音層也會把這輪回話靜音，規則先叫模型別說
    assert "翻頁成功不出聲" in system_instruction()


def test_prompt_pins_spoken_language():
    # 實機測試出現韓文回覆：規則要明講只能說繁體中文
    assert "只能用繁體中文" in system_instruction() and "日文" in system_instruction() and "韓文" in system_instruction()


def test_prompt_maps_this_slide_to_page_source():
    # 「說明這張簡報」被查成整份簡報、回答了別頁（實機）；這張／這頁要用 source=page
    assert "這張" in system_instruction() and "source 填 page" in system_instruction()


@pytest.mark.parametrize("action", ["上一頁", "派題", "公布答案", "收卷", "收起抽屜"])
def test_host_only_command_changes_nothing(deck_state, action):
    vt = make_tools(deck_state, FakeLLM({}))
    before = deck_state.snapshot("host")
    r = vt.handle(call("host_only_command", action=action))
    assert r == {"status": "refused", "message": "請用主持人畫面操作"}
    assert deck_state.snapshot("host") == before


def test_make_quiz_drafts_not_published(deck_state):
    q = {"stem": "題", "options": ["A", "B", "C", "D"], "answer_index": 1, "explanation_zh": "解",
         "quote": QUOTE, "source_label": "今天的簡報", "page": 2}
    vt = make_tools(deck_state, FakeLLM({"questions": [q, q]}))
    r = vt.handle(call("make_quiz", n=2, source="deck"))
    assert r["status"] == "drafted" and r["count"] == 2 and "主持人" in r["message"]
    assert deck_state.run is None and len(deck_state.drafts) == 2


def test_unknown_tool_error(deck_state):
    vt = make_tools(deck_state, FakeLLM({}))
    assert vt.handle(call("delete_everything"))["status"] == "error"


def test_tool_schema_and_prompt():
    assert [t["name"] for t in TOOLS] == ["ask", "about_me", "ask_results", "summarize_papers", "read_results", "make_quiz", "open_reference", "next_page", "prev_page",
                                      "goto_page", "host_only_command", "play_deck", "end_deck",
                                      "show_source", "close_source", "summarize_source",
                                      "open_citation", "read_answer", "quit_app"]
    assert "不得用自己的知識" in system_instruction() and "host_only_command" in system_instruction()


# ---- 最終審查修正 ----

def test_phi_in_tool_args_blocked_and_alerts(deck_state):
    llm = FakeLLM(answered([card("x")]))
    vt = make_tools(deck_state, llm)
    r = vt.handle(call("ask", question="病歷號 1234567 的病人怎麼用藥", source="deck"))
    assert r["status"] == "blocked" and llm.calls == [] and deck_state.voice_phi_alert
    deck_state.voice_phi_alert = False
    r = vt.handle(call("make_quiz", n=2, source="deck", focus="0912345678 家長"))
    assert r["status"] == "blocked" and deck_state.voice_phi_alert


def test_selected_reference_used_without_source(deck_state, make_pdf):
    folder = deck_state.config.folder("參考資料")
    make_pdf(["x " * 5, QUOTE + " in children."], folder=folder, name="KDIGO 2021 GN.pdf")
    llm = FakeLLM(answered([card("CNI", label="KDIGO 2021 GN")]))
    vt = make_tools(deck_state, llm)
    vt.handle(call("open_reference", name="KDIGO 2021 GN"))
    r = vt.handle(call("ask", question="q"))  # 模型沒給 source
    assert r["status"] == "answered" and "[KDIGO 2021 GN 第 2 頁]" in llm.calls[0]["prompt"]


def test_reference_name_implies_references(deck_state, make_pdf):
    make_pdf(["x " * 5, QUOTE + " in children."], folder=deck_state.config.folder("參考資料"), name="IPNA 2020.pdf")
    llm = FakeLLM(answered([card("CNI", label="IPNA 2020")]))
    vt = make_tools(deck_state, llm)
    r = vt.handle(call("ask", question="q", reference_name="IPNA 2020"))
    assert r["status"] == "answered" and "[IPNA 2020 第 2 頁]" in llm.calls[0]["prompt"]


def test_deck_source_still_wins_over_selected_reference(deck_state, make_pdf):
    make_pdf(["x " * 5, QUOTE], folder=deck_state.config.folder("參考資料"), name="IPNA 2020.pdf")
    llm = FakeLLM(answered([card("CNI")]))
    vt = make_tools(deck_state, llm)
    vt.handle(call("open_reference", name="IPNA 2020"))
    vt.handle(call("ask", question="q", source="deck"))
    assert "[今天的簡報 第 2 頁]" in llm.calls[0]["prompt"]


def test_voice_quiz_refused_when_host_has_drafts(deck_state, bank_question):
    llm = FakeLLM({"questions": []})
    vt = make_tools(deck_state, llm)
    deck_state.drafts = [bank_question]
    r = vt.handle(call("make_quiz", n=3, source="deck"))
    assert r["status"] == "refused" and "草稿" in r["message"]
    assert deck_state.drafts == [bank_question] and llm.calls == []


def test_voice_fallback_message(deck_state):
    from xiaoshen.oe_client import OEResult
    oe = OEResult("a", "Calcineurin inhibitors are first-line therapy for SRNS [3].",
                  [{"index": 3, "title": "T", "journal": "Lancet", "year": "2023", "doi": "", "first_author": "V"}])
    llm = FakeLLM({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []},
                  {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
                      {"point_zh": "CNI 一線", "quote": "Calcineurin inhibitors are first-line therapy", "citation_index": 3}]})
    lib = Library(deck_state.config.folder("參考資料"))
    vt = VoiceTools(deck_state, JobRunner(deck_state, llm, deck_state.config, lib, ask=lambda q, s: oe), lib)
    r = vt.handle(call("ask", question="SRNS?", source="deck"))
    # 簡報播放中：OE 簡報先問才播；「今天的簡報中沒有提到」警示照念
    assert r["status"] == "oe_confirm" and "今天的簡報中沒有提到" in r["message"] and "要播放嗎" in r["message"]
    assert "cards" not in r


def test_voice_questions_marked_spoken(deck_state):
    seen = {}
    vt = make_tools(deck_state, FakeLLM({}))

    def fake_ask(text, route, refs, spoken=False, confirmed=False):
        seen["spoken"] = spoken
        return ""
    vt.jobs.ask = fake_ask
    vt.handle(call("ask", question="q", source="deck"))
    assert seen["spoken"] is True


def test_voice_result_includes_derivations(deck_state):
    from xiaoshen.persona import Persona, set_persona
    set_persona(Persona("小森"))
    pages_resp = {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "CNI", "quote": QUOTE, "source_label": "今天的簡報", "page": 2}],
        "derivations": [{"conclusion_zh": "所以 CNI 是一線", "uses": [0]}]}
    vt = make_tools(deck_state, FakeLLM(pages_resp))
    r = vt.handle(call("ask", question="q", source="deck"))
    assert "derived" not in r  # 預設不念；要求才念，念時標明未經主治確認
    assert "小森推導（未經主治確認）：所以 CNI 是一線" in vt.handle(call("read_answer"))["message"]


def test_voice_reads_derivations_as_unconfirmed():
    from xiaoshen.answering import derived_label

    assert "未經主治確認" in derived_label()



# ---- 播放／結束簡報（§6.0）----

@pytest.fixture
def hud_tools(deck_state, make_pdf):
    folder = deck_state.config.folder("簡報")
    a = make_pdf(["酸鹼 " * 5], folder=folder, name="酸鹼電解質平衡.pdf")
    b = make_pdf(["練習 " * 5], folder=folder, name="酸鹼電解質平衡 練習.pdf")
    deck_state.set_deck_paths([deck_state.deck.path, a, b])
    deck_state.stop_presenting()
    return make_tools(deck_state, FakeLLM(answered([])))


def test_play_deck_single_match(hud_tools):
    r = hud_tools.handle(call("play_deck", name="D"))
    assert r == {"status": "ok", "message": "開始播放「D」"} and hud_tools.state.presenting


def test_play_deck_choose_on_multiple(hud_tools):
    r = hud_tools.handle(call("play_deck", name="酸鹼"))
    assert r["status"] == "choose" and set(r["candidates"]) == {"酸鹼電解質平衡", "酸鹼電解質平衡 練習"}
    assert not hud_tools.state.presenting and hud_tools.state.deck_candidates


def test_play_deck_not_found(hud_tools):
    assert hud_tools.handle(call("play_deck", name="腎移植"))["status"] == "not_found"


def test_switch_mid_play_needs_confirm(hud_tools):
    hud_tools.handle(call("play_deck", name="D"))
    r = hud_tools.handle(call("play_deck", name="酸鹼電解質平衡"))
    assert r == {"status": "confirm", "message": "要停止「D」、改播「酸鹼電解質平衡」嗎？"}
    assert hud_tools.state.deck.name == "D"
    r = hud_tools.handle(call("play_deck", name="酸鹼電解質平衡", confirm=True))
    assert r["status"] == "ok" and hud_tools.state.deck.name == "酸鹼電解質平衡"


def test_confirm_without_pending_does_not_switch(hud_tools):
    hud_tools.handle(call("play_deck", name="D"))
    r = hud_tools.handle(call("play_deck", name="酸鹼電解質平衡", confirm=True))
    assert r["status"] == "confirm" and hud_tools.state.deck.name == "D"


def test_play_todays_deck(hud_tools):
    assert hud_tools.handle(call("play_deck", name="今天的簡報"))["message"] == "開始播放「D」"


def test_end_deck_and_next_page_on_hud(hud_tools):
    hud_tools.handle(call("play_deck", name="D"))
    assert hud_tools.handle(call("end_deck")) == {"status": "ok", "message": "已回到主畫面"}
    assert hud_tools.handle(call("next_page")) == {"status": "not_presenting", "message": "目前不在簡報中"}
    assert hud_tools.state.page == 0


def test_prompt_and_tools_mention_play_deck():
    names = {t["name"] for t in TOOLS}
    assert {"play_deck", "end_deck"} <= names and "play_deck" in system_instruction() and "end_deck" in system_instruction()


def test_switch_to_broken_deck_reports_error(hud_tools):
    broken = hud_tools.state.config.folder("簡報") / "壞掉的簡報.pdf"
    broken.write_bytes(b"x")
    hud_tools.state.set_deck_paths(hud_tools.state.deck_paths + [broken])
    hud_tools.handle(call("play_deck", name="D"))
    hud_tools.handle(call("play_deck", name="壞掉的簡報"))
    r = hud_tools.handle(call("play_deck", name="壞掉的簡報", confirm=True))
    assert r["status"] == "error" and hud_tools.state.deck.name == "D" and hud_tools.state.presenting


def test_prev_page(deck_state):
    deck_state.goto(2)
    vt = make_tools(deck_state, FakeLLM({}))
    assert vt.handle(call("prev_page")) == {"status": "ok", "page": 2}
    assert deck_state.page == 1


def test_goto_page_one_based(deck_state):
    vt = make_tools(deck_state, FakeLLM({}))
    assert vt.handle(call("goto_page", page=3)) == {"status": "ok", "page": 3}
    assert deck_state.page == 2


def test_goto_page_out_of_range_says_page_count(deck_state):
    vt = make_tools(deck_state, FakeLLM({}))
    n = deck_state.deck.page_count
    r = vt.handle(call("goto_page", page=n + 5))
    assert r == {"status": "out_of_range", "message": f"沒有第 {n + 5} 頁，這份簡報共 {n} 頁"}
    assert deck_state.page == 0
    assert vt.handle(call("goto_page", page=0))["status"] == "out_of_range"


def test_prev_and_goto_on_hud(hud_tools):
    assert hud_tools.handle(call("prev_page")) == {"status": "not_presenting", "message": "目前不在簡報中"}
    assert hud_tools.handle(call("goto_page", page=2)) == {"status": "not_presenting", "message": "目前不在簡報中"}


def test_prompt_routes_page_commands():
    assert "prev_page" in system_instruction() and "goto_page" in system_instruction() and "播放第" in system_instruction()
    host = next(t for t in TOOLS if t["name"] == "host_only_command")
    assert "上一頁" not in host["description"] and "跳頁" not in host["description"]


def test_prompt_ignores_bare_name():
    # 實機：只叫喚醒詞被聽成 ciao，模型回 Hello
    assert "只叫你的名字或喚醒詞" in system_instruction() and "發音相近的字" in system_instruction()


def test_repeat_page_turn_without_new_speech_is_refused(deck_state):
    # 實機「一直往下翻」：每翻一次都要有使用者新說的話；模型自己重複呼叫不翻
    vt = make_tools(deck_state, FakeLLM({}))
    vt.hear("下一頁")
    assert vt.handle(call("next_page"))["status"] == "ok" and deck_state.page == 1
    assert vt.handle(call("next_page")) == {"status": "already_done", "message": ""}
    assert vt.handle(call("goto_page", page=3)) == {"status": "already_done", "message": ""}
    assert deck_state.page == 1
    vt.hear("下一頁")
    assert vt.handle(call("next_page"))["status"] == "ok" and deck_state.page == 2


def test_failed_page_turn_does_not_use_up_the_request(deck_state):
    vt = make_tools(deck_state, FakeLLM({}))
    vt.hear("翻到第 80 頁")
    assert vt.handle(call("goto_page", page=80))["status"] == "out_of_range"
    assert vt.handle(call("goto_page", page=3))["status"] == "ok"


def test_tool_log_has_page_but_no_question_text(deck_state, tmp_path):
    import json

    from xiaoshen import eventlog

    eventlog.configure(tmp_path)
    try:
        vt = make_tools(deck_state, FakeLLM({}))
        vt.handle(call("next_page"))
        vt.handle(call("ask", question="病歷號 1234567 的孩子怎麼了"))
    finally:
        eventlog.configure(None)
    lines = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    tools = [l for l in lines if l["event"] == "tool"]
    assert tools[0]["name"] == "next_page" and tools[0]["status"] == "ok" and tools[0]["page"] == 2
    assert tools[1]["name"] == "ask" and "1234567" not in json.dumps(lines, ensure_ascii=False)


def test_voice_pubmed_fallback_message_and_source(deck_state):
    # 搜尋不限 OpenEvidence；唸出的說明要講對是哪個資料庫
    from xiaoshen.oe_client import OEBlocked
    from xiaoshen.pubmed import PubMedRecord

    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    rec = PubMedRecord("777", "T", ["Lee A"], "Pediatr Nephrol", "2022", "", "", "", "",
                       "Calcineurin inhibitors remain first-line therapy for SRNS.")
    llm = FakeLLM({"query": "SRNS", "broader": "SRNS"},
                  {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
                      {"point_zh": "CNI 一線", "quote": "Calcineurin inhibitors remain first-line therapy", "citation_index": 1}]})
    lib = Library(deck_state.config.folder("參考資料"))
    jr = JobRunner(deck_state, llm, deck_state.config, lib, ask=blocked, pubmed=lambda q: [rec])
    vt = VoiceTools(deck_state, jr, lib)
    # 問題不含英文字：這裡測備援訊息，不測搜尋前確認（另見 test_voice_confirm.py）
    r = vt.handle(call("ask", question="類固醇抗性腎病症候群一線？"))
    assert r["status"] == "answered" and r["message"] == "OpenEvidence 需要重新驗證，以下為 PubMed 結果，找到 1 張字卡，已投影"
    assert vt.handle(call("read_answer", card=1))["message"] == "CNI 一線"


def test_voice_can_ask_pubmed_directly():
    src = next(t for t in TOOLS if t["name"] == "ask")["parameters"]["properties"]["source"]
    assert "pubmed" in src["enum"] and "PubMed" in src["description"]


def test_pubmed_source_not_overridden_by_selected_reference(deck_state):
    vt = make_tools(deck_state, FakeLLM({}))
    deck_state.voice_ref = "KDIGO"
    vt.library.docs["KDIGO"] = object()
    assert vt._route("pubmed", "") == "pubmed"


def test_voice_reads_model_knowledge_warning(deck_state):
    # 文獻都沒有 → Gemini 一般知識：唸出的說明與每點出處都講明未經文獻驗證
    llm = FakeLLM({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []},
                  {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [{"point_zh": "一般知識"}]})
    lib = Library(deck_state.config.folder("參考資料"))

    def blocked(q, s):
        from xiaoshen.oe_client import OEBlocked
        raise OEBlocked("x")

    jr = JobRunner(deck_state, llm, deck_state.config, lib, ask=blocked, knowledge_fallback=True)
    r = VoiceTools(deck_state, jr, lib).handle(call("ask", question="類固醇抗性腎病症候群？"))
    assert r["message"] == "OpenEvidence 無法使用，以下為 Gemini 一般知識，未經文獻驗證，找到 1 張字卡，已投影"
    assert "cards" not in r  # 警示照念，內容預設不念


def test_prompt_says_do_not_read_sources():
    # 唸出處太花時間
    assert "不要念出處" in system_instruction() and "point 與 source" not in system_instruction()


def test_prompt_routes_content_requests_to_ask():
    # 實機 2026-11-05：「根據簡報列出五個討論主題」沒呼叫 ask，語音模型自己回「無法提供」。
    # 整理、列重點、討論主題、摘要也要查資料（回答模組實測做得到：附頁碼的字卡）
    assert "討論主題" in system_instruction() and "整理" in system_instruction() and "不得自己回答無法" in system_instruction()
    desc = next(t for t in TOOLS if t["name"] == "ask")["description"]
    assert "整理" in desc and "討論主題" in desc


# ---- 顯示出處原文（§6.2a）：語音「顯示第 N 張的原文」「打開原文網頁」「關掉原文」----

class FakeViewer:
    def __init__(self):
        self.calls = []

    def show(self, card, kind="auto"):
        self.calls.append(("show", card, kind))
        return {"status": "ok", "message": f"已顯示第 {card} 張的摘要"}

    def close(self):
        self.calls.append(("close",))
        return {"status": "ok", "message": "已關閉原文"}


def test_show_and_close_source_by_voice(deck_state):
    lib = Library(deck_state.config.folder("參考資料"))
    fv = FakeViewer()
    vt = VoiceTools(deck_state, JobRunner(deck_state, FakeLLM({}), deck_state.config, lib), lib, viewer=fv)
    assert vt.handle(call("show_source", card=2))["message"] == "已顯示第 2 張的摘要"
    vt.handle(call("show_source", card=1, kind="web"))
    vt.handle(call("close_source"))
    assert fv.calls == [("show", 2, "auto"), ("show", 1, "web"), ("close",)]


def test_show_source_without_viewer(deck_state):
    r = make_tools(deck_state, FakeLLM({})).handle(call("show_source", card=1))
    assert r["status"] == "error"


def test_prompt_and_tools_for_source_view():
    names = [t["name"] for t in TOOLS]
    assert "show_source" in names and "close_source" in names
    assert "顯示第" in system_instruction() and "原文網頁" in system_instruction() and "關掉原文" in system_instruction()


def test_prompt_close_source_synonyms():
    # 實機：使用者說「關閉全文」「收起字卡」想關原文畫面
    assert "關閉全文" in system_instruction() and "收起字卡" in system_instruction()


# ---- 原文重點摘要簡報（§6.2b）----

class FakeSummaryJobs:
    def __init__(self, result):
        self.result, self.started, self.cancelled = result, 0, 0

    def summarize_source(self, viewer):
        self.started += 1
        return "job1"

    def wait_summary(self, job, timeout=180):
        assert timeout == 180
        return self.result

    def cancel_summary(self, job_id=None):
        self.cancelled += 1
        return True


def _summary_tools(st, result):
    lib = Library(st.config.folder("參考資料"))
    jobs = FakeSummaryJobs(result)
    return VoiceTools(st, jobs, lib, viewer=object()), jobs


def test_summarize_source_without_open_source(deck_state):
    vt, jobs = _summary_tools(deck_state, {"status": "ok", "pages": 3})
    deck_state.source_view = None
    r = vt.handle(call("summarize_source"))
    assert r == {"status": "no_source", "message": "請先打開要整理的原文"} and jobs.started == 0


def test_summarize_source_reports_pages(deck_state):
    vt, _ = _summary_tools(deck_state, {"status": "ok", "pages": 7, "unverified": 1, "path": "x.pdf"})
    deck_state.source_view = {"kind": "web", "card": 1, "url": "https://x"}
    assert vt.handle(call("summarize_source")) == {"status": "ok", "message": "整理好了，共 7 頁"}


def test_summarize_source_error_passed_through(deck_state):
    vt, _ = _summary_tools(deck_state, {"status": "error", "message": "這份是掃描檔，讀不到文字"})
    deck_state.source_view = {"kind": "web", "card": 1, "url": "https://x"}
    assert vt.handle(call("summarize_source"))["message"] == "這份是掃描檔，讀不到文字"


def test_summarize_source_timeout_cancels(deck_state):
    vt, jobs = _summary_tools(deck_state, None)
    deck_state.source_view = {"kind": "web", "card": 1, "url": "https://x"}
    r = vt.handle(call("summarize_source"))
    assert r["message"] == "整理太久，已取消" and jobs.cancelled == 1


def test_play_deck_replays_last_summary(deck_state, make_pdf):
    p = make_pdf(["S1", "S2"], folder=deck_state.config.folder("紀錄") / "摘要簡報", name="S.pdf")
    deck_state.last_summary = p
    vt, _ = _summary_tools(deck_state, None)
    r = vt.handle(call("play_deck", name="摘要簡報"))
    assert r["status"] == "ok" and deck_state.summary_path == p and deck_state.presenting


def test_play_deck_summary_none_yet(deck_state):
    vt, _ = _summary_tools(deck_state, None)
    assert vt.handle(call("play_deck", name="摘要簡報")) == {"status": "not_found", "message": "還沒有摘要簡報"}


def test_end_deck_after_summary_says_back(deck_state, make_pdf):
    p = make_pdf(["S1"], folder=deck_state.config.folder("紀錄") / "摘要簡報", name="S.pdf")
    deck_state.play_summary(p)
    vt, _ = _summary_tools(deck_state, None)
    r = vt.handle(call("end_deck"))
    assert r["status"] == "ok" and "原本" in r["message"] and deck_state.deck.name == "D"


def test_prompt_routes_summary_of_open_source():
    assert "summarize_source" in system_instruction() and "我讀一下" in system_instruction() and "摘要簡報" in system_instruction()
    assert any(t["name"] == "summarize_source" for t in TOOLS)



def test_summarize_source_busy(deck_state):
    vt, jobs = _summary_tools(deck_state, None)
    jobs.summarize_source = lambda viewer: ""
    deck_state.source_view = {"kind": "web", "card": 1, "url": "https://x"}
    r = vt.handle(call("summarize_source"))
    assert r["status"] == "busy" and "稍" in r["message"] and jobs.cancelled == 0


def test_summarize_source_superseded_message(deck_state):
    vt, jobs = _summary_tools(deck_state, {"status": "superseded", "message": "已取消整理"})
    deck_state.source_view = {"kind": "web", "card": 1, "url": "https://x"}
    assert vt.handle(call("summarize_source"))["message"] == "已取消整理" and jobs.cancelled == 0


def test_prompt_rule_one_still_unconditional():
    # 審查 #5：「整理資料內容一律呼叫 ask」不能只在沒開原文時才適用
    assert "投影上沒有打開原文時" not in system_instruction()
    assert "也一律呼叫 ask" in system_instruction() and "summarize_source" in system_instruction()



def test_close_source_ends_playing_summary(deck_state, make_pdf):
    # 2026-11-05 實機：摘要簡報播放中說「關閉原文」「關閉原文摘要」，助理說已關閉但投影片還在
    p = make_pdf(["S1", "S2"], folder=deck_state.config.folder("紀錄") / "摘要簡報", name="S.pdf")
    deck_state.play_summary(p)
    deck_state.source_view = None
    vt, _ = _summary_tools(deck_state, None)
    r = vt.handle(call("close_source"))
    assert r == {"status": "ok", "message": "已關閉摘要簡報"}
    assert deck_state.summary_path is None and deck_state.deck.name == "D"


def test_close_source_closes_source_view_first(deck_state, make_pdf):
    p = make_pdf(["S1"], folder=deck_state.config.folder("紀錄") / "摘要簡報", name="S.pdf")
    deck_state.play_summary(p)

    class Viewer:
        closed = 0

        def close(self):
            Viewer.closed += 1
            deck_state.source_view = None
            return {"status": "ok", "message": "已關閉原文"}

    deck_state.source_view = {"kind": "abstract", "card": 1}
    vt = VoiceTools(deck_state, FakeSummaryJobs(None), Library(deck_state.config.folder("參考資料")), viewer=Viewer())
    assert vt.handle(call("close_source"))["message"] == "已關閉原文"
    assert Viewer.closed == 1 and deck_state.summary_path is not None  # 原文先關；摘要簡報還在


def test_prompt_close_summary_words():
    assert "關閉摘要" in system_instruction()



# ---- §6.2c：回答預設不念；OE 簡報先問才播；要求才念 ----

def test_read_answer_reads_current_oe_page(deck_state, make_pdf):
    p = make_pdf(["OE cover", "Calcineurin inhibitors are first-line therapy [3]."],
                 folder=deck_state.config.folder("紀錄") / "OE簡報", name="oe.pdf")
    deck_state.play_summary(p, kind="oe")
    deck_state.goto(1)
    vt, _ = _summary_tools(deck_state, None)
    r = vt.handle(call("read_answer"))
    assert r["status"] == "ok" and "Calcineurin inhibitors are first-line therapy [3]." in r["message"]


def test_read_answer_out_of_range_and_nothing(deck_state):
    vt, _ = _summary_tools(deck_state, None)
    assert vt.handle(call("read_answer"))["status"] == "not_found"


def test_oe_confirm_then_play_deck(deck_state, make_pdf):
    p = make_pdf(["OE cover"], folder=deck_state.config.folder("紀錄") / "OE簡報", name="oe.pdf")
    deck_state.oe_pending = p

    class Jobs(FakeSummaryJobs):
        def play_pending_oe(self):
            deck_state.oe_pending = None
            return deck_state.play_summary(p, kind="oe")

    vt = VoiceTools(deck_state, Jobs(None), Library(deck_state.config.folder("參考資料")))
    assert vt.handle(call("play_deck", name="OE 簡報"))["status"] == "ok"
    assert deck_state.temp_kind == "oe" and deck_state.oe_pending is None


def test_play_deck_oe_none_yet(deck_state):
    vt, _ = _summary_tools(deck_state, None)
    assert vt.handle(call("play_deck", name="OE 簡報")) == {"status": "not_found", "message": "還沒有 OE 簡報"}


def test_prompt_rule_two_silent_by_default():
    assert "預設不要念字卡內容" in system_instruction() and "read_answer" in system_instruction()
    assert "再依序念出每張字卡" not in system_instruction()



def test_open_citation_by_voice(deck_state):
    # I5：spec「<喚醒詞>，打開第 N 篇文獻」
    calls = []

    class V:
        def open_reference(self, ref):
            calls.append(ref)
            return {"status": "ok", "message": "已打開原文網頁"}

    vt = VoiceTools(deck_state, FakeSummaryJobs(None), Library(deck_state.config.folder("參考資料")), viewer=V())
    assert vt.handle(call("open_citation", number=3))["status"] == "ok" and calls == ["n:3"]
    assert "open_citation" in system_instruction()


def test_instruction_tells_how_to_pronounce_jia():
    """實機聽起來把「賈」唸成「加」：名字有「賈」時提示讀音；沒有就不加這句。"""
    from xiaoshen.persona import Persona, set_persona

    assert "「賈」唸第三聲" in system_instruction() and "不要唸成「加」" in system_instruction()
    set_persona(Persona("小森"))
    assert "唸第三聲" not in system_instruction()


# ---- 語音結束程式：要使用者親口說「確定結束」才關 ----

@pytest.fixture
def quit_tools(deck_state):
    quits = []
    deck_state.quit_handler = lambda delay=None: quits.append(delay)
    vt = make_tools(deck_state, FakeLLM(answered([])))
    vt.clock = lambda: 100.0
    vt.hear("賈維斯，結束程式")
    return vt, quits


def test_quit_asks_for_confirmation_first(quit_tools):
    vt, quits = quit_tools
    r = vt.handle(call("quit_app"))
    assert r["status"] == "confirm_quit" and "確定結束" in r["message"] and quits == []
    assert any(t["name"] == "quit_app" for t in TOOLS) and "quit_app" in system_instruction()


def test_quit_model_cannot_confirm_by_itself(quit_tools):
    vt, quits = quit_tools
    vt.handle(call("quit_app"))
    r = vt.handle(call("quit_app", confirm=True))  # 使用者還沒開口，模型自己說「確定」
    assert r["status"] == "confirm_quit" and quits == []


def test_quit_after_user_says_confirm(quit_tools):
    from xiaoshen.voice_tools import VOICE_QUIT_DELAY

    vt, quits = quit_tools
    vt.handle(call("quit_app"))
    vt.hear("確定結束")
    r = vt.handle(call("quit_app", confirm=True))
    assert r["status"] == "ok" and quits == [VOICE_QUIT_DELAY]
    assert VOICE_QUIT_DELAY >= 2  # 先讓助理把「程式即將結束」說完


def test_quit_cancelled_when_user_does_not_confirm(quit_tools):
    vt, quits = quit_tools
    vt.handle(call("quit_app"))
    vt.hear("不要，取消")
    r = vt.handle(call("quit_app", confirm=True))
    assert r["status"] == "cancelled" and quits == []
    vt.hear("確定結束")  # 取消後要重新說「結束程式」，不能直接確認
    assert vt.handle(call("quit_app", confirm=True))["status"] == "confirm_quit" and quits == []


def test_quit_confirmation_expires(quit_tools):
    from xiaoshen.voice_tools import QUIT_CONFIRM_TTL

    vt, quits = quit_tools
    vt.handle(call("quit_app"))
    vt.hear("確定結束")
    vt.clock = lambda: 100.0 + QUIT_CONFIRM_TTL + 1
    r = vt.handle(call("quit_app", confirm=True))
    assert r["status"] == "expired" and quits == []


def test_quit_without_launcher_points_to_button(quit_tools):
    vt, quits = quit_tools
    vt.state.quit_handler = None
    vt.handle(call("quit_app"))
    vt.hear("確定結束")
    r = vt.handle(call("quit_app", confirm=True))
    assert r["status"] == "error" and "結束程式" in r["message"]


# ---- 還沒轉成 PDF 的 pptx：播放時才轉 ----

def _slow_pptx_tools(hud_tools, make_pdf):
    import threading

    folder = hud_tools.state.config.folder("簡報")
    src = folder / "腎移植.pptx"
    src.write_bytes(b"pptx")
    gate = threading.Event()

    def convert(s):
        assert gate.wait(5)
        return make_pdf(["移植 " * 5], folder=s.parent, name=s.stem + ".pdf")

    hud_tools.state.deck_converter = convert
    hud_tools.state.set_deck_paths([src, *hud_tools.state.deck_paths])
    return gate


def test_play_deck_pptx_says_it_needs_time_then_plays(hud_tools, make_pdf):
    gate = _slow_pptx_tools(hud_tools, make_pdf)
    r = hud_tools.handle(call("play_deck", name="腎移植"))
    assert r == {"status": "converting", "message": "這份簡報要先轉成 PDF，需要一段時間，好了會自動播放"}
    assert not hud_tools.state.presenting
    gate.set()
    hud_tools.state._convert_thread.join(5)
    assert hud_tools.state.presenting and hud_tools.state.deck.name == "腎移植"


def test_play_deck_while_another_pptx_converts_is_busy(hud_tools, make_pdf):
    gate = _slow_pptx_tools(hud_tools, make_pdf)
    folder = hud_tools.state.config.folder("簡報")
    other = folder / "急性腎損傷.pptx"
    other.write_bytes(b"pptx")
    hud_tools.state.set_deck_paths([other, *hud_tools.state.deck_paths])
    hud_tools.handle(call("play_deck", name="腎移植"))
    r = hud_tools.handle(call("play_deck", name="急性腎損傷"))
    assert r["status"] == "busy" and "腎移植" in r["message"]
    gate.set()
    hud_tools.state._convert_thread.join(5)


def test_play_todays_deck_when_newest_is_still_pptx(hud_tools, make_pdf):
    gate = _slow_pptx_tools(hud_tools, make_pdf)
    hud_tools.state.deck = None  # 開機時最新的是還沒轉的 pptx：沒有開著的簡報
    r = hud_tools.handle(call("play_deck", name="今天的簡報"))
    assert r["status"] == "converting"
    gate.set()
    hud_tools.state._convert_thread.join(5)
    assert hud_tools.state.deck.name == "腎移植"


def test_play_deck_pptx_without_powerpoint(hud_tools):
    folder = hud_tools.state.config.folder("簡報")
    src = folder / "腎移植.pptx"
    src.write_bytes(b"pptx")
    hud_tools.state.set_deck_paths([src, *hud_tools.state.deck_paths])
    r = hud_tools.handle(call("play_deck", name="腎移植"))
    assert r["status"] == "error" and "PowerPoint" in r["message"]


# ---- 問題分類型（2026-11-18）：介紹自己、問目前列出的文獻 ----

from xiaoshen import persona  # noqa: E402
from xiaoshen.answering import Answer, citation_label  # noqa: E402
from xiaoshen.voice_tools import about_me_text  # noqa: E402

ABS2 = "This randomized controlled trial enrolled 240 children and compared rituximab with tacrolimus."


def lit_base():
    refs = {1: {"index": 1, "title": "P1", "first_author": "Lee", "journal": "Kidney Int", "year": "2019"},
            2: {"index": 2, "title": "P2", "first_author": "Wang", "journal": "N Engl J Med", "year": "2025"}}
    return Answer("oe1", "SRNS？", "openevidence", "answered", references=refs)


class AbstractViewer:
    def abstract_for(self, ref):
        return ABS2, ref.get("title", "")


def test_about_me_uses_persona(deck_state):
    r = make_tools(deck_state, FakeLLM(answered([]))).handle(call("about_me"))
    # 實機 2026-11-19：回 "ok" 時模型拿到介紹卻沒唸 → 狀態標明要唸
    assert r["status"] == "speak" and r["message"] == about_me_text()
    assert persona.current().display_name in r["message"] and persona.wake_word() in r["message"]
    assert chr(0x5c0f) + chr(0x814e) not in r["message"]  # 舊名不寫死（發行檔案不得出現這兩個字）


def test_ask_results_answered_says_projected_only(deck_state):
    deck_state.results_base = lit_base()
    vt = make_tools(deck_state, FakeLLM({"kind": "answer", "answer_zh": "最新的是第 2 篇（2025）", "refs": [2]}))
    r = vt.handle(call("ask_results", question="哪一篇最新"))
    assert r == {"status": "answered", "message": "已投影"}
    assert vt.handle(call("read_answer"))["message"] == "最新的是第 2 篇（2025）"  # 要求才念


def test_ask_results_needs_reading_message(deck_state):
    deck_state.results_base = lit_base()
    vt = make_tools(deck_state, FakeLLM({"kind": "needs_reading", "answer_zh": "", "refs": []}))
    assert vt.handle(call("ask_results", question="哪幾篇是 RCT")) == {
        "status": "needs_reading", "message": "我讀一下文獻再回答"}


def test_ask_results_no_results_message(deck_state):
    vt = make_tools(deck_state, FakeLLM({"kind": "answer", "answer_zh": "x", "refs": [1]}))
    assert vt.handle(call("ask_results", question="哪一篇最新")) == {
        "status": "no_results", "message": "目前投影上沒有文獻清單"}


def test_ask_results_error_message(deck_state):
    deck_state.results_base = lit_base()
    vt = make_tools(deck_state, FakeLLM({"kind": "answer", "answer_zh": "第 9 篇", "refs": [9]}))
    r = vt.handle(call("ask_results", question="哪一篇最新"))
    assert r["status"] == "error" and r["message"] == "無法從文獻清單回答"


def test_ask_results_phi_blocked(deck_state):
    deck_state.results_base = lit_base()
    llm = FakeLLM({"kind": "answer", "answer_zh": "x", "refs": [1]})
    vt = make_tools(deck_state, llm)
    vt.hear("A123456789 這床的病人")
    assert vt.handle(call("ask_results", question="那篇最新"))["status"] == "blocked"
    assert llm.calls == []


def test_read_results_counts_cards(deck_state):
    base = lit_base()
    deck_state.results_base = base
    llm = FakeLLM({"status": "answered", "summary_zh": "", "clarify_zh": "",
                   "cards": [{"point_zh": "第 2 篇是 RCT", "quote": "This randomized controlled trial enrolled 240",
                              "source_label": citation_label(base.references[2]), "page": 1}]})
    vt = make_tools(deck_state, llm)
    vt.viewer = AbstractViewer()
    assert vt.handle(call("read_results", question="哪幾篇是 RCT")) == {
        "status": "answered", "message": "找到 1 張字卡，已投影"}


def test_read_results_without_viewer(deck_state):
    deck_state.results_base = lit_base()
    vt = make_tools(deck_state, FakeLLM(answered([])))
    assert vt.handle(call("read_results", question="q")) == {"status": "error", "message": "原文功能未啟用"}


def test_read_results_no_results(deck_state):
    vt = make_tools(deck_state, FakeLLM(answered([])))
    vt.viewer = AbstractViewer()
    assert vt.handle(call("read_results", question="q"))["message"] == "目前投影上沒有文獻清單"


def test_question_type_tools_declared():
    assert {"about_me", "ask_results", "read_results"} <= {t["name"] for t in TOOLS}


def test_prompt_mentions_question_types():
    text = system_instruction()
    for word in ("about_me", "ask_results", "read_results", "needs_reading", "今天的簡報"):
        assert word in text


# ---- 後端保險：模型沒指定來源就送 OE 時，用字詞改正（2026-11-18） ----

from xiaoshen.voice_tools import deck_intent, results_intent  # noqa: E402


@pytest.mark.parametrize("q", ["摘要今天的簡報", "這份簡報的重點", "簡報內容有哪些"])
def test_deck_intent_phrases(q):
    assert deck_intent(q)


def test_deck_intent_lets_clinical_through():
    assert not deck_intent("今天的病人怎麼處理")


@pytest.mark.parametrize("q", ["你列出的文獻中哪一篇是最新的", "剛剛的文獻有幾篇", "第 3 篇是哪個期刊",
                               "第三篇的作者是誰", "這幾篇有隨機對照試驗嗎", "這些文獻哪篇最大"])
def test_results_intent_phrases(q):
    assert results_intent(q)


@pytest.mark.parametrize("q", ["第 2 型糖尿病的第一線用藥", "哪一種抗生素較好", "這幾天發燒怎麼處理", "第一線治療是什麼"])
def test_results_intent_lets_clinical_through(q):
    assert not results_intent(q)


def _record_route(vt, name):
    seen = []

    def fake(text, query, ask_back=True):
        seen.append(text)
        return lit_base()

    setattr(vt.jobs, name, fake)
    return seen


def test_ask_redirects_to_deck(deck_state):
    vt = make_tools(deck_state, FakeLLM(answered([card("簡報重點")])))
    oe = _record_route(vt, "_oe")
    r = vt.handle(call("ask", question="摘要今天的簡報"))
    assert r["status"] == "answered" and oe == []
    assert deck_state.answer.route == "deck"


def test_ask_redirects_to_results_when_base(deck_state):
    deck_state.results_base = lit_base()
    vt = make_tools(deck_state, FakeLLM({"kind": "answer", "answer_zh": "最新的是第 2 篇（2025）", "refs": [2]}))
    oe = _record_route(vt, "_oe")
    assert vt.handle(call("ask", question="哪一篇是最新的")) == {"status": "answered", "message": "已投影"}
    assert oe == []


def test_ask_results_phrase_without_base_goes_search(deck_state):
    vt = make_tools(deck_state, FakeLLM(answered([])))
    oe = _record_route(vt, "_oe")
    vt.handle(call("ask", question="第 3 篇是哪個期刊"))
    assert oe == ["第 3 篇是哪個期刊"]


def test_explicit_pubmed_not_redirected(deck_state):
    deck_state.results_base = lit_base()
    vt = make_tools(deck_state, FakeLLM(answered([])))
    pm = _record_route(vt, "_pubmed")
    vt.handle(call("ask", question="第 3 篇是哪個期刊", source="pubmed"))
    assert pm == ["第 3 篇是哪個期刊"]


# ---- 最後審查修正（2026-11-18） ----

@pytest.mark.parametrize("q", ["第 2 型糖尿病哪一篇指引建議用 metformin", "有沒有哪一篇研究比較 tacrolimus 和 cyclosporine",
                               "哪篇指引建議先用類固醇"])
def test_results_intent_lets_which_paper_clinical_through(q):
    assert not results_intent(q)


@pytest.mark.parametrize("q", ["哪一篇最新", "哪篇是最早的", "上面哪一篇樣本最大"])
def test_results_intent_which_paper_with_anchor(q):
    assert results_intent(q)


def test_read_answer_reads_results_card_while_oe_deck_plays(deck_state):
    base = lit_base()
    base.oe_deck = str(deck_state.deck.path)
    deck_state.answer = base
    deck_state.results_base = base
    deck_state.temp_kind = "oe"
    deck_state.summary_path = deck_state.deck.path  # OE 簡報播放中
    vt = make_tools(deck_state, FakeLLM({"kind": "answer", "answer_zh": "最新的是第 2 篇（2025）", "refs": [2]}))
    assert vt.handle(call("ask_results", question="哪一篇最新"))["status"] == "answered"
    assert vt.handle(call("read_answer"))["message"] == "最新的是第 2 篇（2025）"


# ---- 推導說明不投影、不念（2026-11-18 摘要品質） ----

def test_read_answer_skips_derivation_notes(deck_state):
    llm = FakeLLM({**answered([card("CNI 一線")]), "derivation_notes": ["此問題不涉及數值規則，因此沒有進行推導"]})
    vt = make_tools(deck_state, llm)
    vt.handle(call("ask", question="SRNS 一線？", source="deck"))
    msg = vt.handle(call("read_answer"))["message"]
    assert "CNI 一線" in msg and "沒有進行推導" not in msg


def test_display_does_not_render_derivation_notes():
    from xiaoshen.config import REPO_ROOT
    assert "derivation_notes" not in (REPO_ROOT / "web" / "display.js").read_text(encoding="utf-8")
    assert "derivation_notes" in (REPO_ROOT / "web" / "host.js").read_text(encoding="utf-8")  # 主持人保留


# ---- 語音「摘要第 N 篇」：一次 1 篇，做成簡報（2026-11-18 摘要品質） ----

from xiaoshen.source_text import SourceDoc  # noqa: E402
from xiaoshen.voice_tools import paper_numbers, summary_intent  # noqa: E402

SUM_Q = "Glucocorticoids are the first-line treatment for children with nephrotic syndrome in most cases."
SUM_LLM = {"doc_type": "study", "slides": [{"title": "結果", "points": [
    {"zh": "類固醇為第一線", "quotes": [SUM_Q], "page": 1}]}]}


class PaperViewer:
    def __init__(self):
        self.asked = []

    def full_doc(self, ref, citation=""):
        self.asked.append(ref["index"])
        return SourceDoc(ref["title"], citation, [f"Intro. {SUM_Q} More."], paged=True, full_text=True, origin="pmc")

    def abstract_doc(self, ref, citation=""):
        raise AssertionError("not expected")


def paper_base():
    refs = {14: {"index": 14, "title": "P14", "first_author": "Wang", "journal": "JAMA Pediatr", "year": "2025"},
            15: {"index": 15, "title": "P15", "first_author": "Basu", "journal": "JAMA Pediatr", "year": "2018"}}
    return Answer("oe1", "SRNS？", "openevidence", "answered", references=refs)


def paper_tools(st):
    st.results_base = paper_base()
    vt = make_tools(st, FakeLLM(SUM_LLM))
    vt.viewer = PaperViewer()
    return vt


def test_summarize_one_paper(deck_state):
    vt = paper_tools(deck_state)
    r = vt.handle(call("summarize_papers", numbers=[14]))
    # 2026-11-19：立刻回答、背景整理，好了自動播放（語音不用乾等）
    assert r == {"status": "started", "message": "開始整理第 14 篇，好了會自動播放"}
    vt.jobs.wait_idle()
    assert vt.viewer.asked == [14] and deck_state.presenting


def test_summarize_two_papers_does_first_and_says_next(deck_state):
    vt = paper_tools(deck_state)
    r = vt.handle(call("summarize_papers", numbers=[14, 15]))
    assert r["message"] == "一次只整理一篇：先整理第 14 篇，好了會自動播放；第 15 篇等這篇好了再跟我說"
    vt.jobs.wait_idle()
    assert vt.viewer.asked == [14]


def test_summarize_numbers_taken_from_what_user_said(deck_state):
    # 實機 2026-11-18：說「摘要第 1 篇和第 5 篇」，模型只填了第一篇 → 篇號以使用者的話為準
    vt = paper_tools(deck_state)
    vt.hear("摘要第14篇和第15篇")
    r = vt.handle(call("summarize_papers", numbers=[14]))
    assert "第 15 篇等這篇好了再跟我說" in r["message"]
    vt.jobs.wait_idle()


def test_summarize_unknown_number(deck_state):
    vt = paper_tools(deck_state)
    assert vt.handle(call("summarize_papers", numbers=[99])) == {"status": "not_found", "message": "清單上沒有第 99 篇"}


def test_summarize_without_results(deck_state):
    vt = make_tools(deck_state, FakeLLM(SUM_LLM))
    vt.viewer = PaperViewer()
    assert vt.handle(call("summarize_papers", numbers=[14]))["message"] == "目前投影上沒有文獻清單"


@pytest.mark.parametrize("q,nums", [("摘要第14篇和第15篇文獻內容", [14, 15]), ("整理第三篇的重點", [3]),
                                    ("第 14、15 篇的重點", [14, 15])])
def test_summary_intent_and_numbers(q, nums):
    assert summary_intent(q) and paper_numbers(q) == nums


def test_summary_intent_needs_both_words():
    assert not summary_intent("第 3 篇是哪個期刊") and not summary_intent("摘要今天的簡報")


@pytest.mark.parametrize("tool", ["ask", "ask_results"])
def test_guard_routes_summary_requests(deck_state, tool):
    vt = paper_tools(deck_state)
    r = vt.handle(call(tool, question="摘要第14篇和第15篇文獻內容"))
    assert r["message"].startswith("一次只整理一篇：先整理第 14 篇")
    vt.jobs.wait_idle()
    assert vt.viewer.asked == [14]


def test_prompt_mentions_summarize_papers():
    text = system_instruction()
    assert "summarize_papers" in text and "立刻照念 message" in text and "自動播放" in text


def test_ask_summary_without_results_does_not_search(deck_state):
    vt = make_tools(deck_state, FakeLLM(SUM_LLM))
    vt.viewer = PaperViewer()
    oe = _record_route(vt, "_oe")
    r = vt.handle(call("ask", question="摘要第14篇和第15篇文獻內容"))
    assert r["message"] == "目前投影上沒有文獻清單" and oe == []


def test_prompt_says_about_me_must_be_spoken():
    text = system_instruction()
    assert "about_me 回傳後一定要把 message 完整唸出來" in text

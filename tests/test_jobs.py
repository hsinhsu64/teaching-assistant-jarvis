import threading

import pytest

from xiaoshen.answering import CLARIFY_SCHEMA
from xiaoshen.jobs import JobRunner
from xiaoshen.library import Library
from xiaoshen.llm import LLMError
from xiaoshen.oe_client import OEBlocked

from fakes import FakeLLM

QUOTE = "We recommend calcineurin inhibitors as first-line therapy"


def answered(label="今天的簡報", page=2, point="CNI 一線"):
    return {"status": "answered", "summary_zh": "摘要", "clarify_zh": "",
            "cards": [{"point_zh": point, "quote": QUOTE, "source_label": label, "page": page}]}


def quiz_items(k, label="今天的簡報", page=2):
    return {"questions": [{"stem": f"題{i}", "options": ["A", "B", "C", "D"], "answer_index": 1,
                           "explanation_zh": "解析", "quote": QUOTE, "source_label": label, "page": page}
                          for i in range(k)]}


class GateLLM:
    """每次呼叫依序等待對應的 Event 才回傳，用來控制完成順序。"""

    def __init__(self, responses):
        self.responses = responses  # list of (event, response)
        self.calls = 0
        self.lock = threading.Lock()

    def generate_json(self, model, system, prompt, schema, timeout=90):
        with self.lock:
            ev, resp = self.responses[self.calls]
            self.calls += 1
        ev.wait(5)
        return resp


@pytest.fixture
def deck_state(state_factory, make_pdf):
    st = state_factory()
    st.open_deck(make_pdf(["Intro " * 5, QUOTE + " for SRNS.", "Case 3 " * 5],
                          folder=st.config.folder("簡報"), name="D.pdf"))
    return st


def runner(st, llm, ask=None):
    return JobRunner(st, llm, st.config, Library(st.config.folder("參考資料")),
                     **({"ask": ask} if ask else {}))


def test_ask_with_phi_not_sent(deck_state):
    llm = FakeLLM(answered())
    jr = runner(deck_state, llm)
    assert jr.ask("A123456789 這床的病人怎麼辦", "deck", []) == ""
    jr.wait_idle()
    assert llm.calls == []
    assert any("未送出" in w for w in deck_state.warnings)
    assert deck_state.phi_hits == ["A123456789"]
    assert deck_state.answer_job is None


def test_phi_hits_cleared_on_clean_ask(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    jr.ask("A123456789", "deck", [])
    jr.ask("SRNS 一線？", "deck", [])
    jr.wait_idle()
    assert deck_state.phi_hits == []


def test_quiz_focus_with_phi_not_sent(deck_state):
    llm = FakeLLM(quiz_items(1))
    jr = runner(deck_state, llm)
    assert jr.make_quiz(3, "deck", [], "0912345678 家長問的") == ""
    jr.wait_idle()
    assert llm.calls == []


def test_answer_job_sets_drawer(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    job = jr.ask("SRNS 一線？", "deck", [])
    assert job
    jr.wait_idle()
    assert deck_state.drawer == "answer"
    assert deck_state.answer.status == "answered" and deck_state.answer.route == "deck"
    assert deck_state.answer.cards[0].source == "今天的簡報 第 2 頁"
    assert deck_state.answer_job is None


def test_working_drawer_while_running(deck_state):
    ev = threading.Event()
    jr = runner(deck_state, GateLLM([(ev, answered())]))
    jr.ask("SRNS 一線？", "deck", [])
    assert deck_state.drawer == "working"
    snap = deck_state.snapshot("display")
    assert snap["answer_job"]["label"] == "SRNS 一線？"
    ev.set()
    jr.wait_idle()
    assert deck_state.drawer == "answer"


def test_page_route_focuses_current_page_with_whole_deck(deck_state):
    """「目前這一頁」以該頁為主，但可引用整份簡報其他頁的規則。"""
    llm = FakeLLM(answered())
    jr = runner(deck_state, llm)
    deck_state.goto(1)
    jr.ask("這頁重點？", "page", [])
    jr.wait_idle()
    p = llm.calls[0]["prompt"]
    assert "第 2 頁" in p.split("資料：")[0]
    assert "[今天的簡報 第 1 頁]" in p and "[今天的簡報 第 3 頁]" in p
    assert deck_state.answer.cards[0].source == "今天的簡報 第 2 頁"


def test_references_route(deck_state, make_pdf):
    make_pdf(["x " * 5, QUOTE + " in children."], folder=deck_state.config.folder("參考資料"), name="KDIGO.pdf")
    llm = FakeLLM(answered(label="KDIGO"))
    jr = runner(deck_state, llm)
    jr.library.refresh()
    jr.ask("SRNS?", "references", [])
    jr.wait_idle()
    assert deck_state.answer.route == "references" and deck_state.answer.cards[0].source == "KDIGO 第 2 頁"


def test_references_route_empty_library_is_error(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    jr.ask("SRNS?", "references", [])
    jr.wait_idle()
    assert deck_state.answer.status == "error" and "參考資料" in deck_state.answer.error


def test_stale_answer_discarded(deck_state):
    first, second = threading.Event(), threading.Event()
    llm = GateLLM([(first, answered(point="舊")), (second, answered(point="新"))])
    jr = runner(deck_state, llm)
    jr.ask("第一題", "deck", [])
    jr.ask("第二題", "deck", [])
    second.set()
    jr.wait_for(lambda: deck_state.answer is not None)
    assert deck_state.answer.cards[0].point_zh == "新"
    first.set()
    jr.wait_idle()
    assert deck_state.answer.question == "第二題" and deck_state.answer.cards[0].point_zh == "新"


def test_answer_job_failure_shows_message(deck_state):
    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    jr = runner(deck_state, FakeLLM(answered()), ask=blocked)
    jr.ask("SRNS?", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.status == "error" and "重新驗證" in deck_state.answer.error
    assert deck_state.drawer == "answer"


def test_unexpected_exception_contained(deck_state):
    class Boom:
        def generate_json(self, *a, **k):
            raise KeyError("boom")

    jr = runner(deck_state, Boom())
    jr.ask("SRNS?", "deck", [])
    jr.wait_idle()
    assert deck_state.answer.status == "error"


def test_answer_does_not_hide_open_quiz(deck_state, bank_question):
    deck_state.start_run("single", [bank_question])
    jr = runner(deck_state, FakeLLM(answered()))
    jr.ask("SRNS?", "deck", [])
    jr.wait_idle()
    assert deck_state.drawer == "quiz" and deck_state.answer is not None


def test_quiz_drafts_not_on_display(deck_state):
    jr = runner(deck_state, FakeLLM(quiz_items(3)))
    jr.make_quiz(3, "deck", [], "")
    jr.wait_idle()
    assert len(deck_state.drafts) == 3 and deck_state.draft_kind == "exam"
    assert deck_state.draft_notes == ["要求 3 題，通過驗證 3 題"]
    assert deck_state.drawer == "hidden"
    disp = deck_state.snapshot("display")
    assert "drafts" not in disp and "題0" not in str(disp)
    host = deck_state.snapshot("host")
    assert host["drafts"][0]["answer"] == 1 and host["draft_kind"] == "exam"


def test_quiz_failure_note(deck_state):
    jr = runner(deck_state, FakeLLM(exc=LLMError("找不到 GEMINI_API_KEY")))
    jr.make_quiz(3, "deck", [], "")
    jr.wait_idle()
    assert deck_state.draft_notes[0].startswith("出題失敗：") and deck_state.drafts == []


def test_make_quiz_n_out_of_range(deck_state):
    with pytest.raises(ValueError):
        runner(deck_state, FakeLLM(quiz_items(1))).make_quiz(0, "deck", [], "")


def test_single_question_focus_uses_current_page(deck_state):
    llm = FakeLLM(quiz_items(1))
    jr = runner(deck_state, llm)
    deck_state.goto(1)
    jr.make_quiz(1, "deck", [], "")
    jr.wait_idle()
    assert "第 2 頁" in llm.calls[0]["prompt"].split("資料：")[0]
    assert deck_state.draft_kind == "single"


def test_publish_drafts_starts_exam(deck_state):
    jr = runner(deck_state, FakeLLM(quiz_items(3)))
    jr.make_quiz(3, "deck", [], "")
    jr.wait_idle()
    deck_state.remove_draft(deck_state.drafts[0].id)
    assert len(deck_state.drafts) == 2
    deck_state.publish_drafts()
    assert deck_state.run.kind == "exam" and len(deck_state.run.questions) == 2
    assert deck_state.drafts == [] and deck_state.drawer == "quiz"
    with pytest.raises(LookupError):
        deck_state.publish_drafts()


def test_publish_drafts_blocked_while_run_open(deck_state, bank_question):
    jr = runner(deck_state, FakeLLM(quiz_items(2)))
    jr.make_quiz(2, "deck", [], "")
    jr.wait_idle()
    deck_state.start_run("single", [bank_question])
    with pytest.raises(RuntimeError):
        deck_state.publish_drafts()
    assert len(deck_state.drafts) == 2


def test_regenerate_draft_replaces_in_place(deck_state):
    llm = FakeLLM(quiz_items(3), {"questions": [dict(quiz_items(1)["questions"][0], stem="新題")]})
    jr = runner(deck_state, llm)
    jr.make_quiz(3, "deck", [], "")
    jr.wait_idle()
    old = deck_state.drafts[1].id
    jr.regenerate(old)
    jr.wait_idle()
    assert [d.stem for d in deck_state.drafts] == ["題0", "新題", "題2"]
    assert "請出 1 題" in llm.calls[1]["prompt"]


def test_toggle_quotes_controls_display(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    jr.ask("SRNS?", "deck", [])
    jr.wait_idle()
    assert "quote" not in deck_state.snapshot("display")["ai_answer"]["cards"][0]
    assert "quote" in deck_state.snapshot("host")["ai_answer"]["cards"][0]
    deck_state.toggle_quotes()
    assert deck_state.snapshot("display")["ai_answer"]["cards"][0]["quote"] == QUOTE


def test_navigation_works_while_job_running(deck_state):
    ev = threading.Event()
    jr = runner(deck_state, GateLLM([(ev, answered())]))
    jr.ask("SRNS?", "deck", [])
    deck_state.next()
    deck_state.next()
    assert deck_state.page == 2
    ev.set()
    jr.wait_idle()


# ---- Phase 3：語音要等結果 ----

def test_wait_answer_returns_result(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    job = jr.ask("SRNS?", "deck", [])
    a = jr.wait_answer(job, timeout=5)
    assert a is not None and a.status == "answered" and a.cards[0].point_zh == "CNI 一線"


def test_wait_answer_superseded_returns_none(deck_state):
    first, second = threading.Event(), threading.Event()
    jr = runner(deck_state, GateLLM([(first, answered(point="舊")), (second, answered(point="新"))]))
    j1 = jr.ask("第一題", "deck", [])
    j2 = jr.ask("第二題", "deck", [])
    second.set()
    first.set()
    assert jr.wait_answer(j1, timeout=5) is None
    assert jr.wait_answer(j2, timeout=5).cards[0].point_zh == "新"


def test_wait_answer_timeout_returns_none(deck_state):
    ev = threading.Event()
    jr = runner(deck_state, GateLLM([(ev, answered())]))
    job = jr.ask("SRNS?", "deck", [])
    assert jr.wait_answer(job, timeout=0.2) is None
    ev.set()
    jr.wait_idle()


def test_wait_quiz_returns_count(deck_state):
    jr = runner(deck_state, FakeLLM(quiz_items(3)))
    job = jr.make_quiz(3, "deck", [], "")
    assert jr.wait_quiz(job, timeout=5) == (3, ["要求 3 題，通過驗證 3 題"])


# ---- 簡報／參考資料沒有 → 自動改查 OpenEvidence（語音與打字都套用）----

from xiaoshen.oe_client import OEResult  # noqa: E402

OE_TEXT = "Calcineurin inhibitors are first-line therapy for steroid-resistant nephrotic syndrome [3]."
OE_RES = OEResult("a1", OE_TEXT, [{"index": 3, "title": "Childhood Nephrotic Syndrome", "journal": "Lancet",
                                   "year": "2023", "doi": "", "first_author": "Vivarelli"}])
NOT_FOUND = {"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []}
# §6.2c：OpenEvidence 沒有結果＝回傳空白回答（不再由助理判斷字卡能否對上）
OE_EMPTY = OEResult("a1", "", [], raw="")
OE_HIT = OEResult("a1", OE_TEXT, [{"index": 3, "title": "Childhood Nephrotic Syndrome", "journal": "Lancet",
                                   "year": "2023", "doi": "", "first_author": "Vivarelli"}], raw=OE_TEXT)


def oe_only_for(word):
    """問題含 word 才有 OpenEvidence 結果（模擬校正後才查得到）。"""
    def ask(q, s):
        return OE_HIT if word in q else OE_EMPTY
    return ask
OE_CARDS = {"status": "answered", "summary_zh": "CNI", "clarify_zh": "",
            "cards": [{"point_zh": "CNI 是一線", "quote": "Calcineurin inhibitors are first-line therapy", "citation_index": 3}]}


def test_not_found_falls_back_to_openevidence(deck_state):
    asked = []
    llm = FakeLLM(NOT_FOUND, OE_CARDS)
    jr = runner(deck_state, llm, ask=lambda q, s: asked.append(q) or OE_HIT)
    jr.ask("SRNS 一線？", "deck", [])
    jr.wait_idle()
    a = deck_state.answer
    assert asked == ["SRNS 一線？"] and len(llm.calls) == 2
    assert a.status == "answered" and a.route == "openevidence" and a.fallback_from == "今天的簡報"
    assert a.cards == [] and a.oe_deck  # §6.2c：OpenEvidence 結果做成 OE 簡報，不做字卡
    d = a.to_dict(with_quotes=False, for_display=True)
    assert d["fallback_from"] == "今天的簡報"


def test_fallback_when_openevidence_also_fails(deck_state):
    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    jr = runner(deck_state, FakeLLM(NOT_FOUND), ask=blocked)
    jr.ask("SRNS?", "deck", [])
    jr.wait_idle()
    a = deck_state.answer
    assert a.status == "not_found" and a.route == "deck"
    assert "資料中沒有提到" in a.summary_zh and "OpenEvidence" in a.summary_zh


def test_openevidence_route_not_searched_twice(deck_state):
    asked = []
    jr = runner(deck_state, FakeLLM(NOT_FOUND), ask=lambda q, s: asked.append(q) or OE_EMPTY)
    jr.ask("SRNS?", "openevidence", [])
    jr.wait_idle()
    assert asked == ["SRNS?"] and deck_state.answer.status == "not_found"


def test_clarify_does_not_fall_back(deck_state):
    asked = []
    jr = runner(deck_state, FakeLLM({"status": "clarify", "summary_zh": "", "clarify_zh": "成人或兒童？", "cards": []}),
                ask=lambda q, s: asked.append(q) or OE_RES)
    jr.ask("劑量？", "deck", [])
    jr.wait_idle()
    assert asked == [] and deck_state.answer.status == "clarify"


def test_working_label_switches_to_openevidence(deck_state):
    first = threading.Event()
    seen = []

    class Slow:
        calls = 0

        def generate_json(self, *a, **k):
            Slow.calls += 1
            if Slow.calls == 1:
                return NOT_FOUND
            seen.append(dict(deck_state.answer_job))
            return OE_CARDS

    jr = runner(deck_state, Slow(), ask=lambda q, s: OE_RES)
    jr.ask("SRNS?", "deck", [])
    jr.wait_idle()
    assert seen[0]["route"] == "openevidence" and "改查 OpenEvidence" in seen[0]["label"]


# ---- 語音問題的名詞校正 ----

def test_spoken_fallback_searches_interpreted_question(deck_state):
    asked = []
    nf = dict(NOT_FOUND, interpreted_question="Bartter 和 Gitelman 怎麼分？")
    jr = runner(deck_state, FakeLLM(nf, OE_CARDS), ask=lambda q, s: asked.append(q) or OE_RES)
    jr.ask("butter 和 ghee 怎麼分？", "deck", [], spoken=True)
    jr.wait_idle()
    assert asked == ["Bartter 和 Gitelman 怎麼分？"]
    assert deck_state.answer.interpreted == "Bartter 和 Gitelman 怎麼分？"


def test_spoken_openevidence_miss_corrected_with_deck_vocabulary_then_retried(deck_state):
    asked = []
    llm = FakeLLM(NOT_FOUND, {"question": "Calcineurin inhibitors 是一線嗎？"}, OE_CARDS)
    jr = runner(deck_state, llm, ask=lambda q, s: asked.append(q) or oe_only_for("Calcineurin")(q, s))
    jr.ask("calcium neurine 是一線嗎？", "openevidence", [], spoken=True)
    jr.wait_idle()
    assert asked == ["calcium neurine 是一線嗎？", "Calcineurin inhibitors 是一線嗎？"]  # 先照原句查，查不到才校正重查
    assert "SRNS" in llm.calls[1]["prompt"]  # 詞彙來自目前簡報
    assert llm.calls[1]["model"] == deck_state.config.model_fallbacks[0]  # 用快的模型
    assert deck_state.answer.interpreted == "Calcineurin inhibitors 是一線嗎？"


def test_spoken_question_found_first_time_is_not_corrected(deck_state):
    asked = []
    llm = FakeLLM(OE_CARDS)
    jr = runner(deck_state, llm, ask=lambda q, s: asked.append(q) or OE_RES)
    jr.ask("SRNS 一線？", "openevidence", [], spoken=True)
    jr.wait_idle()
    assert asked == ["SRNS 一線？"] and len(llm.calls) == 1 and deck_state.answer.status == "answered"


def test_typed_openevidence_question_not_corrected(deck_state):
    asked = []
    jr = runner(deck_state, FakeLLM(OE_CARDS), ask=lambda q, s: asked.append(q) or OE_RES)
    jr.ask("calcium neurine 是一線嗎？", "openevidence", [])
    jr.wait_idle()
    assert asked == ["calcium neurine 是一線嗎？"]


def test_correction_failure_uses_original_question(deck_state):
    from xiaoshen.llm import LLMError

    asked = []

    class FailFirst:
        n = 0

        def generate_json(self, model, *a, **k):
            FailFirst.n += 1
            if FailFirst.n == 1:
                return NOT_FOUND       # 原句查不到
            raise LLMError("busy")     # 校正失敗：照原句，不重查

    jr = runner(deck_state, FailFirst(), ask=lambda q, s: asked.append(q) or OE_EMPTY)
    jr.ask("calcium neurine 是一線嗎？", "openevidence", [], spoken=True)
    jr.wait_idle()
    assert asked == ["calcium neurine 是一線嗎？"] and deck_state.answer.status == "not_found"


# ---- PubMed：OpenEvidence 之後的下一個資料庫 ----

from xiaoshen.pubmed import PubMedRecord  # noqa: E402

PM_REC = [PubMedRecord("777", "SRNS review.", ["Lee A", "Wu B"], "Pediatr Nephrol", "2022", "37", "", "1-9", "",
                       "Calcineurin inhibitors remain first-line therapy for SRNS in children.")]
PM_QUERY = {"query": "SRNS calcineurin", "broader": "SRNS"}
PM_CARDS = {"status": "answered", "summary_zh": "CNI", "clarify_zh": "",
            "cards": [{"point_zh": "CNI 一線", "quote": "Calcineurin inhibitors remain first-line therapy", "citation_index": 1}]}


def pm_runner(st, llm, ask, searched):
    return JobRunner(st, llm, st.config, Library(st.config.folder("參考資料")), ask=ask,
                     pubmed=lambda q: searched.append(q) or PM_REC)


def test_openevidence_blocked_falls_back_to_pubmed(deck_state):
    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    searched = []
    jr = pm_runner(deck_state, FakeLLM(CLEAR, PM_QUERY, PM_CARDS), blocked, searched)
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    a = deck_state.answer
    assert a.status == "answered" and a.route == "pubmed" and searched == ["SRNS calcineurin"]
    assert a.fallback_note == "OpenEvidence 需要重新驗證，以下為 PubMed 結果"
    assert a.cards[0].source == "Lee A et al. Pediatr Nephrol 2022;37:1-9. PMID 777"


def test_deck_then_openevidence_then_pubmed(deck_state):
    searched = []
    llm = FakeLLM(NOT_FOUND, NOT_FOUND, PM_QUERY, PM_CARDS)
    jr = pm_runner(deck_state, llm, lambda q, s: OE_EMPTY, searched)
    jr.ask("SRNS 一線？", "deck", [])
    jr.wait_idle()
    a = deck_state.answer
    assert a.route == "pubmed" and a.status == "answered"
    assert a.fallback_note == "今天的簡報、OpenEvidence 都沒有結果，以下為 PubMed 結果"
    assert a.to_dict(with_quotes=False, for_display=True)["fallback_note"] == a.fallback_note


def test_pubmed_route_direct(deck_state):
    searched = []
    jr = pm_runner(deck_state, FakeLLM(PM_QUERY, PM_CARDS), lambda q, s: OE_RES, searched)
    jr.ask("SRNS 一線？", "pubmed", [])
    jr.wait_idle()
    assert deck_state.answer.route == "pubmed" and deck_state.answer.fallback_note == ""


def test_deck_fallback_to_openevidence_note(deck_state):
    jr = runner(deck_state, FakeLLM(NOT_FOUND, OE_CARDS), ask=lambda q, s: OE_RES)
    jr.ask("SRNS 一線？", "deck", [])
    jr.wait_idle()
    assert deck_state.answer.fallback_note == "今天的簡報中沒有提到，以下為 OpenEvidence 結果"


def test_openevidence_blocked_warning_kept_when_pubmed_answers(deck_state):
    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    deck_state.checks = [{"name": "OpenEvidence", "level": "ok", "detail": "登入有效"}]
    jr = pm_runner(deck_state, FakeLLM(PM_QUERY, PM_CARDS), blocked, [])
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.route == "pubmed"
    check = next(c for c in deck_state.checks if c["name"] == "OpenEvidence")
    assert check["level"] == "warn"



# ---- PubMed 也沒有 → Gemini 自身知識 ----

GEM = {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [{"point_zh": "一般知識的一點"}]}
PM_NONE = {"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []}


def gem_runner(st, llm, ask, pm=lambda q: []):
    return JobRunner(st, llm, st.config, Library(st.config.folder("參考資料")), ask=ask, pubmed=pm,
                     knowledge_fallback=True)


def test_all_sources_empty_falls_back_to_model_knowledge(deck_state):
    llm = FakeLLM(NOT_FOUND, NOT_FOUND, PM_QUERY, GEM)
    jr = gem_runner(deck_state, llm, lambda q, s: OE_EMPTY)
    jr.ask("SRNS 一線？", "deck", [])
    jr.wait_idle()
    a = deck_state.answer
    assert a.route == "model" and a.status == "answered" and a.cards[0].source == "Gemini 一般知識（未經文獻驗證）"
    assert a.fallback_note == "今天的簡報、OpenEvidence、PubMed 都沒有結果，以下為 Gemini 一般知識，未經文獻驗證"
    assert llm.calls[-1]["model"] == deck_state.config.model_answer


def test_unavailable_sources_also_fall_back_to_model_knowledge(deck_state):
    from xiaoshen.pubmed import PubMedError

    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    def down(q):
        raise PubMedError("PubMed 暫時無法連線")

    jr = gem_runner(deck_state, FakeLLM(PM_QUERY, GEM), blocked, pm=down)
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    a = deck_state.answer
    # 2026-11-05 實機：兩邊都是「無法使用」不是「沒有結果」，要講清楚原因
    assert a.route == "model" and a.fallback_note == "OpenEvidence 需要重新驗證、PubMed 暫時無法連線，以下為 Gemini 一般知識，未經文獻驗證"


def test_openevidence_answer_stops_the_chain(deck_state):
    searched = []
    jr = gem_runner(deck_state, FakeLLM(OE_CARDS), lambda q, s: OE_RES, pm=lambda q: searched.append(q) or [])
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.route == "openevidence" and searched == []


def test_clarify_does_not_continue_down_the_chain(deck_state):
    searched = []
    clarify = {"clarify": True, "question": "成人或兒童？"}  # §6.2c：送 OpenEvidence 前的反問檢查
    jr = gem_runner(deck_state, FakeLLM(clarify), lambda q, s: OE_RES, pm=lambda q: searched.append(q) or [])
    jr.ask("劑量？", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.status == "clarify" and searched == []


def test_model_knowledge_off_by_default(deck_state):
    jr = runner(deck_state, FakeLLM(NOT_FOUND, NOT_FOUND), ask=lambda q, s: OE_EMPTY)
    jr.ask("SRNS?", "deck", [])
    jr.wait_idle()
    assert deck_state.answer.route == "deck" and deck_state.answer.status == "not_found"


# ---- 語音名詞校正不限今天簡報 ----

GIT = {"question": "Gitelman syndrome 的治療有哪些？", "unsure": False, "clarify_zh": ""}


def test_spoken_question_corrected_even_without_deck(state_factory):
    st = state_factory()  # 沒有開簡報：沒有簡報詞彙也要校正
    asked = []
    jr = runner(st, FakeLLM(NOT_FOUND, GIT, OE_CARDS), ask=lambda q, s: asked.append(q) or oe_only_for("Gitelman")(q, s))
    jr.ask("Giotomine syndrome 的治療有哪些？", "openevidence", [], spoken=True)
    jr.wait_idle()
    assert asked == ["Giotomine syndrome 的治療有哪些？", "Gitelman syndrome 的治療有哪些？"]
    assert st.answer.interpreted == "Gitelman syndrome 的治療有哪些？"


def test_spoken_unsure_correction_asks_back_without_searching(deck_state):
    asked = []
    unsure = {"question": "Gitelman syndrome 的治療？", "unsure": True, "clarify_zh": "你是說 Gitelman syndrome 嗎？"}
    jr = runner(deck_state, FakeLLM(NOT_FOUND, unsure), ask=lambda q, s: asked.append(q) or OE_EMPTY)
    jr.ask("Giotomine syndrome 的治療？", "openevidence", [], spoken=True)
    jr.wait_idle()
    a = deck_state.answer
    assert asked == ["Giotomine syndrome 的治療？"] and a.status == "clarify" and a.clarify_zh == "你是說 Gitelman syndrome 嗎？"


def test_spoken_deck_miss_is_corrected_before_openevidence(deck_state):
    asked = []
    jr = runner(deck_state, FakeLLM(NOT_FOUND, NOT_FOUND, GIT, OE_CARDS), ask=lambda q, s: asked.append(q) or oe_only_for("Gitelman")(q, s))
    jr.ask("Giotomine syndrome 的治療有哪些？", "deck", [], spoken=True)
    jr.wait_idle()
    assert asked == ["Giotomine syndrome 的治療有哪些？", "Gitelman syndrome 的治療有哪些？"]
    assert deck_state.answer.interpreted == "Gitelman syndrome 的治療有哪些？"



def test_spoken_pubmed_miss_corrected_then_retried(deck_state):
    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")

    searched = []

    def pm(q):
        searched.append(q)
        return PM_REC if "Gitelman" in q else []

    # OE 被擋 → PubMed 原句（搜尋字串、找不到）→ 校正 → PubMed 重查（搜尋字串、字卡）
    llm = FakeLLM(CLEAR, {"query": "Giotomine", "broader": "Giotomine"}, GIT,
                  {"query": "Gitelman syndrome", "broader": "Gitelman"}, PM_CARDS)
    jr = JobRunner(deck_state, llm, deck_state.config, Library(deck_state.config.folder("參考資料")), ask=blocked,
                   pubmed=pm)
    jr.ask("Giotomine syndrome 的治療有哪些？", "openevidence", [], spoken=True)
    jr.wait_idle()
    a = deck_state.answer
    assert a.route == "pubmed" and a.status == "answered" and a.interpreted == "Gitelman syndrome 的治療有哪些？"
    assert searched == ["Giotomine", "Gitelman syndrome"]


def test_typed_question_never_corrected(deck_state):
    asked = []
    jr = runner(deck_state, FakeLLM(NOT_FOUND), ask=lambda q, s: asked.append(q) or OE_EMPTY)
    jr.ask("Giotomine syndrome？", "openevidence", [])
    jr.wait_idle()
    assert asked == ["Giotomine syndrome？"] and deck_state.answer.status == "not_found"


# ---- 原文重點摘要簡報（§6.2b）：摘要簡報不當作出處 ----

def test_questions_during_summary_use_today_deck(state, make_pdf, config):
    summ = make_pdf(["Summary slide text"], folder=config.folder("紀錄") / "摘要簡報", name="S.pdf")
    today = [state.deck.page_text(i) for i in range(state.deck.page_count)]
    state.play_summary(summ)
    r = runner(state, FakeLLM({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []}))
    for route in ("deck", "page"):
        [(label, pages)] = r.sources(route, [])
        assert [p.split("\n")[0] for p in pages] == [t.split("\n")[0] for t in today]


# ---- 原文重點摘要簡報（§6.2b）：背景工作 ----

from pathlib import Path  # noqa: E402

from xiaoshen.source_text import SourceDoc, SourceUnreadable  # noqa: E402

SUM_P = "Glucocorticoids are the first-line treatment for children with nephrotic syndrome in most cases."
SUM_PAGE = f"Recommendation details. {SUM_P} Further practice points follow in the text."
SUM_RESP = {"slides": [{"title": "初發治療", "points": [{"zh": "類固醇為第一線", "quote": SUM_P, "page": 1}]}]}


class FakeViewer:
    def __init__(self, doc=None, exc=None, gate=None):
        self.doc = doc or SourceDoc("KDIGO 2025", "[4] KDIGO", [SUM_PAGE], paged=True, full_text=True)
        self.exc, self.gate = exc, gate

    def read_full(self):
        if self.gate is not None:
            self.gate.wait(5)
        if self.exc:
            raise self.exc
        return self.doc

    def current_ref(self):
        return None

    def full_doc(self, ref, citation=""):
        return self.read_full()

    def abstract_doc(self, ref, citation=""):
        return SourceDoc(ref.get("title", ""), citation, [SUM_PAGE], paged=False, full_text=False,
                         note="只讀到摘要", origin="abstract")


def test_summary_job_saves_pdf_and_plays(state):
    r = runner(state, FakeLLM(SUM_RESP))
    job = r.summarize_source(FakeViewer())
    res = r.wait_summary(job, timeout=10)
    assert res["status"] == "ok" and res["pages"] == 2 and res["unverified"] == 0
    path = Path(res["path"])
    assert path.exists() and path.parent == state.config.folder("紀錄") / "摘要簡報"
    assert state.summary_path == path and state.presenting is True and state.answer_job is None


def test_new_question_supersedes_summary(state):
    gate = threading.Event()
    r = runner(state, FakeLLM(SUM_RESP))
    job = r.summarize_source(FakeViewer(gate=gate))
    state.answer_job = {"id": "newer", "kind": "answer"}  # 新的提問佔用工作槽（ask 就是這樣取代舊工作）
    gate.set()
    assert r.wait_summary(job, timeout=10)["status"] == "superseded"
    assert state.summary_path is None


def test_cancel_summary(state):
    gate = threading.Event()
    r = runner(state, FakeLLM(SUM_RESP))
    job = r.summarize_source(FakeViewer(gate=gate))
    assert r.cancel_summary() is True and state.answer_job is None
    gate.set()
    assert r.wait_summary(job, timeout=10)["status"] == "superseded" and state.summary_path is None
    assert r.cancel_summary() is False


def test_quiz_start_cancels_summary(state, bank_question):
    gate = threading.Event()
    r = runner(state, FakeLLM(SUM_RESP))
    job = r.summarize_source(FakeViewer(gate=gate))
    state.start_run("single", [bank_question])
    gate.set()
    assert r.wait_summary(job, timeout=10)["status"] == "superseded" and state.summary_path is None


def test_unreadable_source_reports_message(state):
    r = runner(state, FakeLLM(SUM_RESP))
    job = r.summarize_source(FakeViewer(exc=SourceUnreadable("這份是掃描檔，讀不到文字")))
    assert r.wait_summary(job, timeout=10) == {"status": "error", "message": "這份是掃描檔，讀不到文字"}
    assert state.answer_job is None and state.summary_path is None


def test_summary_model_failure_reports_message(state):
    r = runner(state, FakeLLM(exc=LLMError("文字模型逾時（60 秒）")))
    res = r.wait_summary(r.summarize_source(FakeViewer()), timeout=10)
    assert res["status"] == "error" and "整理失敗" in res["message"]


def test_summary_event_logged_without_content(state, tmp_path):
    import json as _json

    from xiaoshen import eventlog

    eventlog.configure(tmp_path / "logs")
    try:
        r = runner(state, FakeLLM(SUM_RESP))
        r.wait_summary(r.summarize_source(FakeViewer()), timeout=10)
    finally:
        eventlog.configure(None)
    lines = [l for f in (tmp_path / "logs").glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    ev = [_json.loads(l) for l in lines if '"summary"' in l]
    assert ev and ev[-1]["event"] == "summary" and ev[-1]["status"] == "ok" and ev[-1]["slides"] == 1
    assert "類固醇" not in "".join(lines) and "Glucocorticoids" not in "".join(lines)



def test_summary_failure_shown_on_host(state):
    # 主持人按「整理重點」失敗時，主持人畫面要看得到原因（語音才會念 message）
    r = runner(state, FakeLLM(SUM_RESP))
    r.wait_summary(r.summarize_source(FakeViewer(exc=SourceUnreadable("這份是掃描檔，讀不到文字"))), timeout=10)
    assert state.snapshot("host")["summary_note"] == "整理重點失敗：這份是掃描檔，讀不到文字"
    assert "summary_note" not in state.snapshot("display")
    r.wait_summary(r.summarize_source(FakeViewer()), timeout=10)
    assert state.snapshot("host")["summary_note"] == ""



def test_second_summary_refused_while_running(state):
    # 審查 #2：同時只能有一份在整理；重複說「整理重點」不能把前一份連同自己一起取消
    gate = threading.Event()
    r = runner(state, FakeLLM(SUM_RESP))
    j1 = r.summarize_source(FakeViewer(gate=gate))
    assert r.summarize_source(FakeViewer()) == ""
    gate.set()
    assert r.wait_summary(j1, timeout=10)["status"] == "ok"


def test_summary_refused_while_question_running(state):
    # 審查 #3：住院醫師的問題還在查，不能被「整理重點」默默丟掉
    state.answer_job = {"id": "q1", "kind": "answer", "label": "q", "started_at": 0}
    r = runner(state, FakeLLM(SUM_RESP))
    assert r.summarize_source(FakeViewer()) == ""
    assert state.answer_job["id"] == "q1"


def test_cancel_summary_only_cancels_its_own_job(state):
    gate = threading.Event()
    r = runner(state, FakeLLM(SUM_RESP))
    j1 = r.summarize_source(FakeViewer(gate=gate))
    assert r.cancel_summary("someone-else") is False and state.answer_job["id"] == j1
    assert r.cancel_summary(j1) is True
    gate.set()


def test_wait_summary_reports_superseded(state):
    gate = threading.Event()
    r = runner(state, FakeLLM(SUM_RESP))
    j1 = r.summarize_source(FakeViewer(gate=gate))
    r.cancel_summary()
    gate.set()
    assert r.wait_summary(j1, timeout=10) == {"status": "superseded", "message": "已取消整理"}


def test_summary_renders_in_render_process_when_available(state):
    # 審查 #4：排版 PDF 會卡住 GIL（30 張約 1 秒），有轉圖子行程就交給它
    from xiaoshen import deck as deckmod

    calls = []

    class FakeRenderer:
        def run(self, fn, *args):
            calls.append(fn.__name__)
            return fn(*args)

    deckmod.set_renderer(FakeRenderer())
    try:
        r = runner(state, FakeLLM(SUM_RESP))
        assert r.wait_summary(r.summarize_source(FakeViewer()), timeout=10)["status"] == "ok"
    finally:
        deckmod.set_renderer(None)
    assert calls == ["render_summary_oe_deck"]  # 2026-11-18：改用 OE 簡報樣式



def test_note_mixes_no_result_and_unavailable(deck_state):
    from xiaoshen.pubmed import PubMedError

    def down(q):
        raise PubMedError("PubMed 暫時無法連線")

    jr = gem_runner(deck_state, FakeLLM(NOT_FOUND, PM_QUERY, GEM), lambda q, s: OE_EMPTY, pm=down)
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.fallback_note == "OpenEvidence 沒有結果、PubMed 暫時無法連線，以下為 Gemini 一般知識，未經文獻驗證"



# ---- OE 簡報（§6.2c）：OpenEvidence 結果完整做成簡報；先檢查要不要反問；PubMed 字卡不限張數 ----

CLEAR = {"clarify": False, "question": ""}
OE_RAW = "# 標題\n\nCalcineurin inhibitors are first-line therapy for steroid-resistant nephrotic syndrome [3]."
OE_DECK_RES = OEResult("a1", OE_RAW, [{"index": 3, "title": "Childhood Nephrotic Syndrome", "journal": "Lancet",
                                       "year": "2023", "doi": "10.1/x", "first_author": "Vivarelli"}], raw=OE_RAW)


def oe_runner(st, llm, ask=lambda q, s: OE_DECK_RES, **kw):
    return JobRunner(st, llm, st.config, Library(st.config.folder("參考資料")), ask=ask, **kw)


def test_clarify_before_openevidence_skips_oe(deck_state):
    asked = []
    jr = oe_runner(deck_state, FakeLLM({"clarify": True, "question": "請問是成人還是兒童？"}),
                   ask=lambda q, s: asked.append(q) or OE_DECK_RES)
    jr.ask("prednisone 劑量？", "openevidence", [])
    jr.wait_idle()
    a = deck_state.answer
    assert a.status == "clarify" and a.clarify_zh == "請問是成人還是兒童？" and asked == []


def test_confirmed_question_skips_clarify_check(deck_state):
    # 使用者已確認過的語音問題不再反問，直接送 OpenEvidence（§5.4）
    asked = []
    llm = FakeLLM({"clarify": True, "question": "請問是成人還是兒童？"})
    jr = oe_runner(deck_state, llm, ask=lambda q, s: asked.append(q) or OE_DECK_RES)
    jr.ask("prednisone 劑量？", "openevidence", [], confirmed=True)
    jr.wait_idle()
    assert asked == ["prednisone 劑量？"] and deck_state.answer.status == "answered"
    assert not any(c["schema"] is CLARIFY_SCHEMA for c in llm.calls)


def test_clarify_check_failure_still_asks_oe(deck_state):
    asked = []
    jr = oe_runner(deck_state, FakeLLM(exc=LLMError("逾時")), ask=lambda q, s: asked.append(q) or OE_DECK_RES)
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    assert asked == ["SRNS 一線？"] and deck_state.answer.status == "answered"


def test_oe_answer_has_no_cards_and_builds_deck(deck_state):
    jr = oe_runner(deck_state, FakeLLM(CLEAR))
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    a = deck_state.answer
    assert a.route == "openevidence" and a.status == "answered" and a.cards == []
    path = Path(a.oe_deck)
    assert path.exists() and path.parent == deck_state.config.folder("紀錄") / "OE簡報"
    assert a.oe_pages >= 2 and a.to_dict()["oe_deck"]["pages"] == a.oe_pages
    assert a.references[3]["title"] == "Childhood Nephrotic Syndrome"


def test_oe_plays_when_not_presenting(deck_state):
    jr = oe_runner(deck_state, FakeLLM(CLEAR))
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.oe_play == "oe_playing"
    assert deck_state.temp_kind == "oe" and deck_state.presenting and deck_state.oe_pending is None


def test_oe_waits_for_confirm_when_presenting(deck_state):
    deck_state.play(deck_state.deck.path)
    jr = oe_runner(deck_state, FakeLLM(CLEAR))
    jr.ask("SRNS 一線？", "openevidence", [], spoken=True)
    jr.wait_idle()
    assert deck_state.answer.oe_play == "oe_confirm" and deck_state.temp_kind is None
    assert deck_state.oe_pending == Path(deck_state.answer.oe_deck)
    assert jr.play_pending_oe() is True
    assert deck_state.temp_kind == "oe" and deck_state.oe_pending is None and deck_state.answer.oe_play == "oe_playing"
    assert jr.play_pending_oe() is False


def test_typed_question_while_presenting_waits_for_host(deck_state):
    deck_state.play(deck_state.deck.path)
    jr = oe_runner(deck_state, FakeLLM(CLEAR))
    jr.ask("SRNS 一線？", "openevidence", [], spoken=False)
    jr.wait_idle()
    assert deck_state.answer.oe_play == "oe_confirm" and deck_state.temp_kind is None
    assert deck_state.snapshot("host")["ai_answer"]["oe_deck"]["play"] == "oe_confirm"


def test_oe_waits_while_quiz_open(deck_state, bank_question):
    deck_state.start_run("single", [bank_question])
    jr = oe_runner(deck_state, FakeLLM(CLEAR))
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.oe_play == "oe_waiting_quiz" and deck_state.temp_kind is None
    assert deck_state.oe_pending is not None and deck_state.drawer == "quiz"


def test_new_question_supersedes_oe_deck(deck_state):
    gate = threading.Event()

    def slow(q, s):
        gate.wait(5)
        return OE_DECK_RES

    jr = oe_runner(deck_state, FakeLLM(CLEAR), ask=slow)
    jr.ask("SRNS 一線？", "openevidence", [])
    deck_state.answer_job = {"id": "newer", "kind": "answer"}
    gate.set()
    jr.wait_idle()
    assert deck_state.temp_kind is None and deck_state.oe_pending is None


def test_empty_oe_answer_falls_to_pubmed(deck_state):
    empty = OEResult("a1", "  ", [], raw="  ")
    searched = []
    jr = oe_runner(deck_state, FakeLLM(CLEAR, PM_QUERY, PM_CARDS), ask=lambda q, s: empty,
                   pubmed=lambda q: searched.append(q) or PM_REC)
    jr.ask("SRNS 一線？", "openevidence", [])
    jr.wait_idle()
    assert deck_state.answer.route == "pubmed" and searched


def test_pubmed_cards_unlimited_and_grouped():
    from xiaoshen.answering import answer_from_pubmed
    from xiaoshen.pubmed import PubMedRecord

    recs = [PubMedRecord(str(100 + i), f"T{i}", ["Lee A"], "J", "2024", "", "", "", "",
                         " ".join(f"Sentence {i} number {k} reports a clinical finding for children." for k in range(3)))
            for i in range(3)]
    cards = [{"point_zh": f"第 {i} 篇第 {k} 點", "quote": f"Sentence {i} number {k} reports a clinical finding",
              "citation_index": i + 1} for k in range(3) for i in range(3)]  # 故意交錯
    llm = FakeLLM({"query": "q", "broader": "q"}, {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": cards})
    a = answer_from_pubmed("q", llm, "m", "fast", search=lambda q: recs)
    assert len(a.cards) == 9
    assert [c.refs[0] for c in a.cards] == [1, 1, 1, 2, 2, 2, 3, 3, 3]


def test_pubmed_prompt_drops_five_limit():
    from xiaoshen.answering import PUBMED_SYSTEM_PROMPT

    assert "最多 5 點" not in PUBMED_SYSTEM_PROMPT and "80 字" in PUBMED_SYSTEM_PROMPT



def test_superseded_question_skips_oe_deck_build(deck_state):
    # I1：被取代的工作不再做簡報（同一分鐘同一題曾覆蓋正在投影的 PDF）
    gate = threading.Event()

    def slow(q, s):
        gate.wait(5)
        return OE_DECK_RES

    jr = oe_runner(deck_state, FakeLLM(CLEAR), ask=slow)
    jr.ask("SRNS 一線？", "openevidence", [])
    deck_state.answer_job = {"id": "newer", "kind": "answer"}
    gate.set()
    jr.wait_idle()
    folder = deck_state.config.folder("紀錄") / "OE簡報"
    assert not folder.exists() or not list(folder.glob("*.pdf"))


# ---- 目前的結果（2026-11-18：問已列出的文獻） ----

ABS2 = "This randomized controlled trial enrolled 240 children and compared rituximab with tacrolimus."


def lit_answer(route="openevidence"):
    from xiaoshen.answering import Answer
    refs = {1: {"index": 1, "title": "P1", "first_author": "Lee", "journal": "Kidney Int", "year": "2019"},
            2: {"index": 2, "title": "P2", "first_author": "Wang", "journal": "N Engl J Med", "year": "2025"}}
    return Answer("oe1", "SRNS？", route, "answered", references=refs)


def test_search_answer_becomes_results_base(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    base = lit_answer()
    jr._oe = lambda text, query, ask_back=True: base
    job = jr.ask("SRNS 治療？", "openevidence", [])
    assert jr.wait_answer(job) is base
    assert deck_state.results_base is base and deck_state.answer is base


def test_deck_answer_keeps_results_base(deck_state):
    base = lit_answer()
    deck_state.results_base = base
    jr = runner(deck_state, FakeLLM(answered()))
    ans = jr.wait_answer(jr.ask("摘要", "deck", []))
    assert ans.status == "answered" and ans.route == "deck"
    assert deck_state.results_base is base and deck_state.answer is ans


def test_answer_results_without_base(deck_state):
    jr = runner(deck_state, FakeLLM({"kind": "answer", "answer_zh": "x", "refs": [1]}))
    assert jr.answer_results("哪篇最新") == {"status": "no_results"}


def test_answer_results_commits_card_and_keeps_base(deck_state):
    base = lit_answer()
    deck_state.results_base = base
    llm = FakeLLM({"kind": "answer", "answer_zh": "最新的是第 2 篇（2025）", "refs": [2]})
    jr = runner(deck_state, llm)
    r = jr.answer_results("哪篇最新")
    assert r["status"] == "answered" and r["answer"] is deck_state.answer
    assert deck_state.answer.cards[0].refs == [2]
    assert deck_state.results_base is base and deck_state.drawer == "answer"
    assert llm.calls[0]["model"] == deck_state.config.model_results


def test_answer_results_needs_reading(deck_state):
    deck_state.results_base = lit_answer()
    jr = runner(deck_state, FakeLLM({"kind": "needs_reading", "answer_zh": "", "refs": []}))
    assert jr.answer_results("哪幾篇是 RCT") == {"status": "needs_reading"}
    assert deck_state.answer is None


def test_answer_results_phi_not_sent(deck_state):
    deck_state.results_base = lit_answer()
    llm = FakeLLM({"kind": "answer", "answer_zh": "x", "refs": [1]})
    jr = runner(deck_state, llm)
    assert jr.answer_results("A123456789 這床的病人那篇呢")["status"] == "blocked"
    assert llm.calls == []


def test_read_results_job_commits(deck_state):
    from xiaoshen.answering import citation_label
    base = lit_answer()
    deck_state.results_base = base
    llm = FakeLLM({"status": "answered", "summary_zh": "", "clarify_zh": "",
                   "cards": [{"point_zh": "第 2 篇是 RCT", "quote": "This randomized controlled trial enrolled 240",
                              "source_label": citation_label(base.references[2]), "page": 1}]})
    jr = runner(deck_state, llm)
    job = jr.read_results("哪幾篇是 RCT", lambda r: (ABS2, r["title"]))
    ans = jr.wait_answer(job)
    assert ans.status == "answered" and ans.cards[0].refs == [2]
    assert deck_state.answer is ans and deck_state.results_base is base


def test_read_results_without_base(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    assert jr.read_results("q", lambda r: ("", "")) == ""


# ---- 2026-11-18 摘要品質：全文來源、OE 整理、OE 簡報樣式 ----

import pymupdf  # noqa: E402

from xiaoshen.answering import Answer  # noqa: E402
from xiaoshen.fulltext import NeedsFallback  # noqa: E402
from xiaoshen.oe_client import OEResult  # noqa: E402

PAPER = {"index": 14, "title": "Tacrolimus or MMF for FRNS", "first_author": "Wang", "journal": "JAMA Pediatr",
         "year": "2025", "doi": "10.1001/x", "pmid": "1"}
SUM_RESP2 = {"doc_type": "study", "slides": [{"title": "結果", "points": [{"zh": "類固醇為第一線", "quotes": [SUM_P],
                                                                         "page": 1}]}]}


class NoFullTextViewer(FakeViewer):
    def current_ref(self):
        return PAPER

    def read_full(self):
        raise NeedsFallback(PAPER)

    def full_doc(self, ref, citation=""):
        raise NeedsFallback(ref)


def _cover(path):
    return pymupdf.open(path)[0].get_text()


def test_summary_uses_full_text_and_oe_layout(state):
    doc = SourceDoc("KDIGO 2025", "[4] KDIGO", [SUM_PAGE], paged=True, full_text=True, origin="pmc")
    r = runner(state, FakeLLM(SUM_RESP2))
    res = r.wait_summary(r.summarize_source(FakeViewer(doc=doc)), timeout=10)
    assert res["status"] == "ok" and res["origin"] == "pmc"
    assert "PubMed Central 全文" in _cover(res["path"]) and "問題：" not in _cover(res["path"])


def test_summary_falls_back_to_oe(state):
    r = runner(state, FakeLLM(SUM_RESP2))
    asked = []

    def oe(text, query, ask_back=True, timeout=None):
        asked.append(query)
        raw = "## 研究設計\n- 隨機對照試驗，240 名兒童 [1]"
        return Answer("o1", text, "openevidence", "answered",
                      oe=OEResult(article_id="a", answer=raw, raw=raw, citations=[{**PAPER, "index": 1}]))

    r._oe = oe
    res = r.wait_summary(r.summarize_source(NoFullTextViewer()), timeout=10)
    assert res["status"] == "ok" and res["origin"] == "oe"
    assert "由 OpenEvidence 整理，未逐句核對原文" in _cover(res["path"])
    assert "Tacrolimus or MMF for FRNS" in asked[0] and "doi:10.1001/x" in asked[0]


def test_summary_oe_blocked_falls_back_to_abstract(state):
    r = runner(state, FakeLLM(SUM_RESP2))
    r._oe = lambda text, query, ask_back=True, timeout=None: Answer("o1", text, "openevidence", "error", error="OpenEvidence 擋下")
    res = r.wait_summary(r.summarize_source(NoFullTextViewer()), timeout=10)
    assert res["status"] == "ok" and res["origin"] == "abstract"
    assert "只讀到摘要" in _cover(res["path"])


def test_summarize_paper_plays_deck(state):
    doc = SourceDoc(PAPER["title"], "[14] Wang 2025", [SUM_PAGE], paged=True, full_text=True, origin="openalex")
    r = runner(state, FakeLLM(SUM_RESP2))
    res = r.wait_summary(r.summarize_paper(PAPER, FakeViewer(doc=doc)), timeout=10)
    assert res["status"] == "ok" and res["origin"] == "openalex"
    assert state.presenting and state.summary_path == Path(res["path"])


def test_oe_paper_fallback_has_bounded_timeout(state):
    r = runner(state, FakeLLM(SUM_RESP2))
    seen = {}

    def oe(text, query, ask_back=True, timeout=None):
        seen["timeout"] = timeout
        return Answer("o1", text, "openevidence", "error", error="逾時")

    r._oe = oe
    res = r.wait_summary(r.summarize_source(NoFullTextViewer()), timeout=10)
    assert res["origin"] == "abstract" and seen["timeout"] is not None and 0 < seen["timeout"] <= 60


def test_oe_answer_about_other_paper_falls_back_to_abstract(state):
    r = runner(state, FakeLLM(SUM_RESP2))
    raw = "## 結果\n- 別篇的內容 [1]"
    other = {"index": 1, "title": "A different trial of cyclophosphamide", "doi": "10.9/other"}
    r._oe = lambda text, query, ask_back=True, timeout=None: Answer(
        "o1", text, "openevidence", "answered", oe=OEResult(article_id="a", answer=raw, raw=raw, citations=[other]))
    res = r.wait_summary(r.summarize_source(NoFullTextViewer()), timeout=10)
    assert res["status"] == "ok" and res["origin"] == "abstract"


def test_oe_not_asked_without_title_or_doi(state):
    r = runner(state, FakeLLM(SUM_RESP2))
    asked = []
    r._oe = lambda *a, **k: asked.append(a) or Answer("o1", "q", "openevidence", "error")

    class Bare(NoFullTextViewer):
        def read_full(self):
            raise NeedsFallback({"index": 3, "title": "", "doi": "", "abstract": "x"})

    r.wait_summary(r.summarize_source(Bare()), timeout=10)
    assert asked == []


# ---- 同一篇做過就直接重播（2026-11-19） ----

def _full(origin="pmc"):
    return SourceDoc(PAPER["title"], "[14] Wang 2025", [SUM_PAGE], paged=True, full_text=True, origin=origin)


def test_summary_reused_for_same_paper(state):
    llm = FakeLLM(SUM_RESP2)
    r = runner(state, llm)
    first = r.wait_summary(r.summarize_paper(PAPER, FakeViewer(doc=_full())), timeout=10)
    state.stop_presenting()
    second = r.wait_summary(r.summarize_paper(PAPER, FakeViewer(doc=_full())), timeout=10)
    assert len(llm.calls) == 1 and second["cached"] is True and second["path"] == first["path"]
    assert state.presenting and state.summary_path == Path(first["path"])


def test_abstract_summary_not_reused(state):
    llm = FakeLLM(SUM_RESP2)
    r = runner(state, llm)
    r._oe = lambda text, query, ask_back=True, timeout=None: Answer("o1", text, "openevidence", "error")
    r.wait_summary(r.summarize_paper(PAPER, NoFullTextViewer()), timeout=10)
    res = r.wait_summary(r.summarize_paper(PAPER, NoFullTextViewer()), timeout=10)
    assert len(llm.calls) == 2 and not res.get("cached")


def test_cached_summary_redone_when_library_pdf_added(state, make_pdf):
    llm = FakeLLM(SUM_RESP2)
    r = runner(state, llm)
    r.wait_summary(r.summarize_paper(PAPER, FakeViewer(doc=_full("openalex"))), timeout=10)
    make_pdf([f"Trial report\ndoi: {PAPER['doi']}"], folder=state.config.folder("參考資料"), name="wang.pdf")
    r.library.refresh()
    res = r.wait_summary(r.summarize_paper(PAPER, FakeViewer(doc=_full("pdf"))), timeout=10)
    assert len(llm.calls) == 2 and not res.get("cached")


def test_cache_ignores_deleted_deck(state):
    llm = FakeLLM(SUM_RESP2)
    r = runner(state, llm)
    first = r.wait_summary(r.summarize_paper(PAPER, FakeViewer(doc=_full())), timeout=10)
    state.stop_presenting()
    Path(first["path"]).unlink()
    res = r.wait_summary(r.summarize_paper(PAPER, FakeViewer(doc=_full())), timeout=10)
    assert len(llm.calls) == 2 and not res.get("cached")


# ---- 2026-11-19：回答一出來就自動存成字卡 PNG（紀錄\字卡），不再有「整理成字卡」按鈕 ----

def test_answer_auto_saved_as_card_png(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    ans = jr.wait_answer(jr.ask("SRNS 一線？", "deck", []))
    jr.wait_idle()
    saved = deck_state.config.folder("紀錄") / "字卡" / ans.card_file
    assert ans.card_file and saved.exists() and saved.read_bytes()[:4] == b"\x89PNG" and "SRNS" in ans.card_file
    assert deck_state.snapshot("host")["ai_answer"]["card_file"] == ans.card_file


def test_not_found_answer_not_saved(deck_state):
    jr = runner(deck_state, FakeLLM({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []}))
    jr._oe = lambda text, query, ask_back=True, timeout=None: Answer("o", text, "openevidence", "not_found")
    jr._pubmed = lambda text, query: Answer("p", text, "pubmed", "not_found")
    jr._fallback_to_model = lambda job_id, text, ans, tried: ans
    ans = jr.wait_answer(jr.ask("q", "deck", []))
    jr.wait_idle()
    assert ans.card_file == "" and not (deck_state.config.folder("紀錄") / "字卡").exists()


def test_oe_deck_answer_not_saved_as_card(deck_state):
    jr = runner(deck_state, FakeLLM(answered()))
    oe_ans = lit_answer()
    oe_ans.oe_deck = "x.pdf"
    jr._oe = lambda text, query, ask_back=True, timeout=None: oe_ans
    jr.wait_answer(jr.ask("q", "openevidence", []))
    jr.wait_idle()
    assert oe_ans.card_file == ""

"""第二次整支審查的修正：推導索引、表格列文獻編號、方括號數字、標示、翻頁收起字卡、PDF 全域鎖。"""

import threading

from xiaoshen.answering import Answer, Card, answer_from_openevidence, answer_from_pages
from xiaoshen.grounding import verify_quote
from xiaoshen.oe_client import OEResult
from xiaoshen.slide_ocr import OCR_MARK

from fakes import FakeLLM


# ---- Critical：推導用到第 6 張以後的字卡 ----

def test_derivation_using_card_beyond_display_limit():
    page = "\n".join(f"Lab item {k} value {100 + k} units" for k in range(8))
    cards = [{"point_zh": f"v{k}", "quote": f"Lab item {k} value {100 + k} units", "source_label": "今天的簡報", "page": 1}
             for k in range(7)]
    resp = {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": cards,
            "derivations": [{"conclusion_zh": "100 + 106 = 206", "uses": [0, 6]}]}
    a = answer_from_pages("q", [("今天的簡報", [page])], FakeLLM(resp), "m", 100_000, route="deck")
    assert a.status == "answered" and len(a.derivations) == 1
    used = [a.cards[i].quote for i in a.derivations[0].uses]
    assert used == ["Lab item 0 value 100 units", "Lab item 6 value 106 units"]
    assert len(a.cards) <= 6  # 前 5 張＋推導用到的那張


# ---- Important 1：表格列只能用同一列的文獻編號 ----

OE_TABLE = ("Bartter and Gitelman differ in several ways [9].\n\n<mdtable>\n| Feature | Bartter | Gitelman |\n|---|---|---|\n"
            "| Urine calcium | High | Low [2] |\n| Serum magnesium | Normal | Low [4] |\n</mdtable>\n\nGenetic testing helps [9].")
CITES = [{"index": n, "title": f"T{n}", "journal": "", "year": "", "doi": "", "first_author": f"A{n}"} for n in (2, 4, 9)]



# §6.2c：OpenEvidence 不再做字卡，表格列的出處驗證測試已移除（表格逐字放進 OE 簡報）


# ---- Important 2：簡報裡的方括號數字是內容，不是文獻編號 ----

def test_bracket_range_on_slides_is_content():
    src = "Target potassium level [3-5] mEq/L for children with CKD."
    assert not verify_quote("Target potassium level [4-6] mEq/L for children", src)
    assert verify_quote("Target potassium level [3-5] mEq/L for children", src)


def test_openevidence_markers_still_ignored():
    oe = OEResult("a", "Calcineurin inhibitors are first-line therapy [3][4] for SRNS in children.", [
        {"index": 3, "title": "T3", "journal": "", "year": "", "doi": "", "first_author": "A"}])
    llm = FakeLLM({"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "CNI", "quote": "Calcineurin inhibitors are first-line therapy for SRNS", "citation_index": 3}]})
    assert answer_from_openevidence("q", "oe.py", llm, "m", ask=lambda q, s: oe).status == "answered"


# ---- Minor 2（升級）：跨進圖片抄錄段的引文也要標「圖片辨識」----

def test_quote_spanning_into_ocr_is_labelled():
    page = f"An Example\n{OCR_MARK}\npH 7.299 HCO3 20.4"
    llm = FakeLLM({"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "p", "quote": f"An Example {OCR_MARK} pH 7.299", "source_label": "今天的簡報", "page": 1}]})
    a = answer_from_pages("q", [("今天的簡報", [page])], llm, "m", 100_000, route="deck")
    assert a.cards and a.cards[0].source.endswith("（圖片辨識）")


# ---- Important 3：說明與推導的標示放在資料裡，不靠模型照念 ----

def test_voice_strings_carry_unconfirmed_label(state_factory, make_pdf):
    from xiaoshen.persona import Persona, set_persona
    set_persona(Persona("小森"))
    from xiaoshen.jobs import JobRunner
    from xiaoshen.library import Library
    from xiaoshen.live_backend import ToolCall
    from xiaoshen.voice_tools import VoiceTools

    st = state_factory()
    st.open_deck(make_pdf(["Na 140 and Cl 106 measured today"], folder=st.config.folder("簡報"), name="D.pdf"))
    resp = {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "Na", "quote": "Na 140 and Cl 106 measured", "source_label": "今天的簡報", "page": 1}],
        "derivations": [{"conclusion_zh": "差值 34", "uses": [0]}], "derivation_notes": ["未說明是否為靜脈血"]}
    lib = Library(st.config.folder("參考資料"))
    vt = VoiceTools(st, JobRunner(st, FakeLLM(resp), st.config, lib), lib)
    r = vt.handle(ToolCall("c", "ask", {"question": "q", "source": "deck"}))
    assert "derived" not in r  # §6.2c：預設不念內容；要求才念，念時照樣標明未經主治確認
    said = vt.handle(ToolCall("c2", "read_answer", {}))["message"]
    assert "小森推導（未經主治確認）：差值 34" in said and "小森說明（未經主治確認）：未說明是否為靜脈血" in said


def test_card_and_display_label_notes():
    from xiaoshen.persona import Persona, set_persona
    set_persona(Persona("小森"))
    from xiaoshen.card import card_html

    a = Answer("x", "q", "deck", "answered", cards=[Card("p", "Na 140", "今天的簡報 第 1 頁")],
               derivation_notes=["未說明是否為靜脈血"])
    assert "小森說明（未經主治確認）：未說明是否為靜脈血" in card_html(a)


# ---- Important 4：翻頁或派題時收起字卡與放大圖 ----

def test_page_turn_and_dispatch_clear_card_and_zoom(state, bank_question):
    state.card = {"answer_id": "a", "path": "x"}
    state.figure_zoom = 0
    state.goto(2)  # 第 3 頁沒有題庫題目，不會自動派題
    assert state.card is None and state.figure_zoom is None
    state.card = {"answer_id": "a", "path": "x"}
    state.figure_zoom = 0
    state.start_run("single", [bank_question])
    assert state.card is None and state.figure_zoom is None


# ---- Important 5：所有 PDF 操作共用一把鎖 ----

def test_all_decks_share_one_pdf_lock(make_pdf, tmp_path):
    from xiaoshen.deck import PDF_LOCK, Deck

    a = Deck(make_pdf(["a"], folder=tmp_path, name="a.pdf"))
    b = Deck(make_pdf(["b"], folder=tmp_path, name="b.pdf"))
    assert a._lock is PDF_LOCK and b._lock is PDF_LOCK
    assert isinstance(PDF_LOCK, type(threading.RLock()))

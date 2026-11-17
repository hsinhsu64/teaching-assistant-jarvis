from xiaoshen.answering import Answer, answer_from_openevidence, answer_from_pages
from xiaoshen.llm import LLMError
from xiaoshen.oe_client import OEBlocked, OEResult

from fakes import FakeLLM

PAGES = [
    "Introduction to glomerular disease in children.",
    "We recommend calcineurin inhibitors as first-line therapy for SRNS.",
    "Rituximab may be considered for frequently relapsing disease.",
]
SOURCES = [("KDIGO 2021 GN", PAGES)]


def card(point, quote, label="KDIGO 2021 GN", page=2):
    return {"point_zh": point, "quote": quote, "source_label": label, "page": page}


def run(resp, sources=SOURCES, max_chars=100_000):
    llm = FakeLLM(resp)
    return answer_from_pages("SRNS 一線？", sources, llm, "m", max_chars), llm


def test_grounded_cards_kept_fabricated_dropped():
    a, _ = run({"status": "answered", "summary_zh": "CNI 一線", "clarify_zh": "", "cards": [
        card("CNI 是一線", "calcineurin inhibitors as first-line therapy"),
        card("FRNS 可用 rituximab", "Rituximab may be considered", page=3),
        card("捏造", "cyclophosphamide is the preferred first-line agent"),
    ]})
    assert a.status == "answered" and a.route == "references"
    assert [c.point_zh for c in a.cards] == ["CNI 是一線", "FRNS 可用 rituximab"]
    assert a.cards[0].source == "KDIGO 2021 GN 第 2 頁"
    assert a.cards[0].quote == "calcineurin inhibitors as first-line therapy"


def test_page_out_of_range_dropped():
    a, _ = run({"status": "answered", "summary_zh": "x", "clarify_zh": "", "cards": [
        card("超界", "calcineurin inhibitors as first-line therapy", page=9),
        card("零頁", "calcineurin inhibitors as first-line therapy", page=0),
        card("錯檔", "calcineurin inhibitors as first-line therapy", label="IPNA"),
    ]})
    assert a.cards == [] and a.status == "not_found"


def test_all_dropped_becomes_not_found():
    a, _ = run({"status": "answered", "summary_zh": "亂講", "clarify_zh": "", "cards": [card("x", "totally made up sentence here")]})
    assert a.status == "not_found" and a.summary_zh == "資料中沒有提到"


def test_model_not_found_passthrough():
    a, _ = run({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []})
    assert a.status == "not_found" and a.summary_zh == "資料中沒有提到"


def test_clarify_passthrough():
    a, _ = run({"status": "clarify", "summary_zh": "", "clarify_zh": "請問是成人還是兒童？", "cards": []})
    assert a.status == "clarify" and a.clarify_zh == "請問是成人還是兒童？"


def test_too_large_refused():
    llm = FakeLLM({})
    a = answer_from_pages("q", SOURCES, llm, "m", max_chars=50)
    assert a.status == "error" and "資料太大" in a.error
    assert llm.calls == []


def test_no_text_refused():
    llm = FakeLLM({})
    a = answer_from_pages("q", [("Scan", ["", " "])], llm, "m", max_chars=1000)
    assert a.status == "error" and "掃描檔" in a.error and llm.calls == []


def test_llm_error_is_error():
    a = answer_from_pages("q", SOURCES, FakeLLM(exc=LLMError("找不到 GEMINI_API_KEY")), "m", 100_000)
    assert a.status == "error" and "GEMINI_API_KEY" in a.error


def test_at_most_five_cards():
    q = "calcineurin inhibitors as first-line therapy"
    a, _ = run({"status": "answered", "summary_zh": "x", "clarify_zh": "", "cards": [card(f"p{i}", q) for i in range(8)]})
    assert len(a.cards) == 5


def test_prompt_contains_page_numbers():
    _, llm = run({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []})
    prompt = llm.calls[0]["prompt"]
    assert "[KDIGO 2021 GN 第 2 頁]" in prompt and "SRNS 一線？" in prompt
    assert "逐字" in llm.calls[0]["system"]


def test_deck_route_label():
    a = answer_from_pages("q", [("今天的簡報", PAGES)], FakeLLM({"status": "answered", "summary_zh": "x", "clarify_zh": "",
        "cards": [card("CNI", "calcineurin inhibitors as first-line therapy", label="今天的簡報")]}), "m", 100_000, route="deck")
    assert a.route == "deck" and a.cards[0].source == "今天的簡報 第 2 頁"


OE = OEResult("art1", "Calcineurin inhibitors (tacrolimus or cyclosporine) are first-line therapy for SRNS [1]. "
              "Rituximab is reserved for refractory cases [2].",
              [{"index": 1, "title": "IPNA SRNS", "journal": "Pediatr Nephrol", "year": "2020", "doi": "", "first_author": "Trautmann"},
               {"index": 2, "title": "KDIGO 2021 GN", "journal": "", "year": "2021", "doi": "", "first_author": ""}])


def oe_card(point, quote, idx):
    return {"point_zh": point, "quote": quote, "citation_index": idx}






def test_openevidence_blocked_is_error():
    def blocked(q, s):
        raise OEBlocked("OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）")
    llm = FakeLLM({})
    a = answer_from_openevidence("q", "oe.py", llm, "m", ask=blocked)
    assert a.status == "error" and "重新驗證" in a.error and llm.calls == []


# ---- 最終審查修正 ----

def test_card_shows_source_text_not_model_text():
    pages = ["Intro.", "We suggest Tacrolimus or cyclosporin is recommended for SRNS."]
    a = answer_from_pages("q", [("G", pages)], FakeLLM({"status": "answered", "summary_zh": "x", "clarify_zh": "",
        "cards": [card("CNI", "tacrolimus or cyclosporine is recommended", label="G")]}), "m", 100_000)
    assert a.cards[0].quote == "Tacrolimus or cyclosporin is recommended"


def test_display_dict_hides_unverified_summary():
    a, _ = run({"status": "answered", "summary_zh": "未驗證的總結", "clarify_zh": "",
                "cards": [card("CNI 是一線", "calcineurin inhibitors as first-line therapy")]})
    assert "summary_zh" not in a.to_dict(with_quotes=False, for_display=True)
    assert a.to_dict(with_quotes=True)["summary_zh"] == "未驗證的總結"
    nf, _ = run({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []})
    assert nf.to_dict(with_quotes=False, for_display=True)["summary_zh"] == "資料中沒有提到"


def test_llm_overload_error_is_friendly_detail_host_only():
    a = answer_from_pages("q", SOURCES, FakeLLM(exc=LLMError("文字模型呼叫失敗：ServerError: 503 UNAVAILABLE. {'error': ...}")),
                          "m", 100_000)
    assert a.status == "error" and a.error == "AI 服務忙碌，請稍後再問"
    assert "503" in a.error_detail
    assert "error_detail" not in a.to_dict(with_quotes=False, for_display=True)
    assert "503" in a.to_dict(with_quotes=True)["error_detail"]




def test_prompts_require_original_language():
    _, llm = run({"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []})
    assert "不可翻譯" in llm.calls[0]["system"]
    assert "quote" in llm.calls[0]["schema"]["properties"]["cards"]["items"]["properties"]






# ---- 語音問題：依資料重新理解聽錯的名詞 ----

def test_spoken_question_reinterpreted_and_shown():
    llm = FakeLLM({"status": "answered", "summary_zh": "s", "clarify_zh": "",
                   "interpreted_question": "Calcineurin inhibitor 是一線嗎？",
                   "cards": [card("CNI 一線", "calcineurin inhibitors as first-line therapy")]})
    a = answer_from_pages("calcium neurine 是一線嗎？", SOURCES, llm, "m", 100_000, spoken=True)
    assert "語音辨識" in llm.calls[0]["system"]
    assert "interpreted_question" in llm.calls[0]["schema"]["properties"]
    assert a.interpreted == "Calcineurin inhibitor 是一線嗎？"
    assert a.to_dict(with_quotes=False, for_display=True)["interpreted"] == "Calcineurin inhibitor 是一線嗎？"


def test_typed_question_not_reinterpreted():
    llm = FakeLLM({"status": "not_found", "summary_zh": "", "clarify_zh": "", "interpreted_question": "x", "cards": []})
    a = answer_from_pages("q", SOURCES, llm, "m", 100_000)
    assert "語音辨識" not in llm.calls[0]["system"]
    assert "interpreted_question" not in llm.calls[0]["schema"]["properties"]
    assert a.interpreted == ""


def test_same_interpretation_not_shown():
    llm = FakeLLM({"status": "not_found", "summary_zh": "", "clarify_zh": "", "interpreted_question": " q ", "cards": []})
    a = answer_from_pages("q", SOURCES, llm, "m", 100_000, spoken=True)
    assert a.interpreted == ""


def test_source_label_with_page_suffix_accepted():
    """實測：模型把標籤寫成「今天的簡報 第 39 頁」；去掉頁碼後綴再比對，頁碼以 page 欄位或後綴為準。"""
    pages = ["intro", "x", "Bartter: 高尿鈣！尿氯會高（可和 vomiting 區分）"]
    llm = FakeLLM({"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "Bartter 高尿鈣", "quote": "高尿鈣！尿氯會高", "source_label": "今天的簡報 第 3 頁", "page": 3},
        {"point_zh": "無頁碼欄位", "quote": "高尿鈣！尿氯會高", "source_label": "今天的簡報 第 3 頁", "page": None},
        {"point_zh": "後綴與頁碼矛盾", "quote": "高尿鈣！尿氯會高", "source_label": "今天的簡報 第 2 頁", "page": 3},
    ]})
    a = answer_from_pages("q", [("今天的簡報", pages)], llm, "m", 100_000, route="deck")
    assert [c.point_zh for c in a.cards] == ["Bartter 高尿鈣", "無頁碼欄位"]
    assert all(c.source == "今天的簡報 第 3 頁" for c in a.cards)


TABLE = {"header": ["Feature", "Bartter", "Gitelman", "References"],
         "rows": [["Urine Ca", "High", "Low", "[1][2]"], ["Mg", "Normal", "Low", "[9]"]]}






def test_quote_from_slide_image_labelled():
    from xiaoshen.slide_ocr import OCR_MARK

    pages = ["intro", "An Example\n" + OCR_MARK + "\npH 血液酸鹼值 L 7.299 HCO3 重碳酸鹽 L 20.4"]
    llm = FakeLLM({"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "pH 偏低", "quote": "pH 血液酸鹼值 L 7.299", "source_label": "今天的簡報", "page": 2},
        {"point_zh": "標題", "quote": "An Example of something", "source_label": "今天的簡報", "page": 2}]})
    a = answer_from_pages("q", [("今天的簡報", pages)], llm, "m", 100_000, route="deck")
    assert [c.source for c in a.cards] == ["今天的簡報 第 2 頁（圖片辨識）"]


# ---- 推導（允許，標明；所依據的數值與規則都要是已驗證字卡）----

ABG_PAGES = ["intro", "AG = Na+ − (Cl- + HCO3-)，正常值 8–12",
             "An Example\n[圖片辨識]\nNa (B) 鈉  140\nCL (B) 氯  106\nHCO3 重碳酸鹽  L  20.4"]


def abg_resp(uses, extra_cards=()):
    return {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [
        {"point_zh": "Na 140", "quote": "Na (B) 鈉  140", "source_label": "今天的簡報", "page": 3},
        {"point_zh": "Cl 106", "quote": "CL (B) 氯  106", "source_label": "今天的簡報", "page": 3},
        {"point_zh": "HCO3 20.4", "quote": "HCO3 重碳酸鹽  L  20.4", "source_label": "今天的簡報", "page": 3},
        {"point_zh": "AG 公式", "quote": "AG = Na+ − (Cl- + HCO3-)", "source_label": "今天的簡報", "page": 2},
        *extra_cards],
        "derivations": [{"conclusion_zh": "AG = 140 − (106 + 20.4) = 13.6，略高於正常值", "uses": uses}]}


def test_derivation_kept_when_all_evidence_verified():
    llm = FakeLLM(abg_resp([0, 1, 2, 3]))
    a = answer_from_pages("AG 是多少？", [("今天的簡報", ABG_PAGES)], llm, "m", 100_000, route="deck")
    assert "derivations" in llm.calls[0]["schema"]["properties"] and "推導" in llm.calls[0]["system"]
    assert len(a.cards) == 4 and len(a.derivations) == 1
    d = a.derivations[0]
    assert d.text.startswith("AG = 140") and d.uses == [0, 1, 2, 3]
    assert d.sources == ["今天的簡報 第 3 頁（圖片辨識）", "今天的簡報 第 2 頁"]
    out = a.to_dict(with_quotes=False, for_display=True)["derivations"]
    assert out == [{"text": d.text, "sources": d.sources, "uses": [0, 1, 2, 3]}]


def test_derivation_dropped_if_any_evidence_fails():
    bad = {"point_zh": "K 9.9", "quote": "K (B) 鉀  9.9", "source_label": "今天的簡報", "page": 3}
    a = answer_from_pages("q", [("今天的簡報", ABG_PAGES)], FakeLLM(abg_resp([0, 1, 4], [bad])), "m", 100_000)
    assert a.derivations == [] and len(a.cards) == 4


def test_derivation_needs_evidence_and_valid_indices():
    for uses in ([], [9], ["x"]):
        a = answer_from_pages("q", [("今天的簡報", ABG_PAGES)], FakeLLM(abg_resp(uses)), "m", 100_000)
        assert a.derivations == [], uses


def test_derivation_without_cards_is_not_found():
    resp = {"status": "answered", "summary_zh": "s", "clarify_zh": "", "cards": [],
            "derivations": [{"conclusion_zh": "憑空推導", "uses": []}]}
    a = answer_from_pages("q", [("今天的簡報", ABG_PAGES)], FakeLLM(resp), "m", 100_000)
    assert a.status == "not_found" and a.derivations == []


def test_focus_page_in_prompt():
    llm = FakeLLM(abg_resp([0]))
    answer_from_pages("這頁的 AG？", [("今天的簡報", ABG_PAGES)], llm, "m", 100_000, route="deck", focus_page=3)
    p = llm.calls[0]["prompt"]
    assert "第 3 頁" in p.split("資料：")[0] and "[今天的簡報 第 2 頁]" in p


def test_derivation_condition_check_and_notes():
    resp = dict(abg_resp([0, 1, 2, 3]))
    resp["derivation_notes"] = ["第 3 頁為靜脈血，第 2 頁規則未註明適用範圍，未推導酸鹼判讀"]
    llm = FakeLLM(resp)
    a = answer_from_pages("q", [("今天的簡報", ABG_PAGES)], llm, "m", 100_000)
    system = llm.calls[0]["system"]
    assert "靜脈" in system and "適用範圍" in system
    assert "derivation_notes" in llm.calls[0]["schema"]["properties"]
    assert a.derivation_notes == ["第 3 頁為靜脈血，第 2 頁規則未註明適用範圍，未推導酸鹼判讀"]
    assert a.to_dict(with_quotes=False, for_display=True)["derivation_notes"] == a.derivation_notes


# ---- PubMed（搜尋不限 OpenEvidence，標期刊 citation）----

from xiaoshen.answering import answer_from_pubmed  # noqa: E402
from xiaoshen.pubmed import PubMedError, PubMedRecord  # noqa: E402

PM = [PubMedRecord("29731287", "Controversies in Management of Hyperkalemia.", ["Long B", "Warix JR"], "J Emerg Med",
                   "2018", "55", "2", "192-205", "10.1016/x",
                   "CONCLUSIONS: Calcium should be given for ECG changes to stabilize the myocardium."),
      PubMedRecord("2", "Insulin for hyperkalemia.", ["Ho K"], "Kidney Int", "2020", "", "", "", "",
                   "Insulin with glucose shifts potassium into cells within 15 minutes.")]
QUERY = {"query": "hyperkalemia AND calcium", "broader": "hyperkalemia treatment"}


def test_pubmed_cards_verified_against_abstracts_with_journal_citation():
    llm = FakeLLM(QUERY, {"status": "answered", "summary_zh": "鈣", "clarify_zh": "", "cards": [
        oe_card("心電圖變化先給鈣", "Calcium should be given for ECG changes to stabilize the myocardium", 1),
        oe_card("胰島素 15 分鐘", "Insulin with glucose shifts potassium into cells within 15 minutes", 2),
        oe_card("捏造", "Dialysis is always first line", 1),
        oe_card("編號錯", "Insulin with glucose shifts potassium into cells within 15 minutes", 1),
    ]})
    searched = []
    a = answer_from_pubmed("高血鉀心電圖變化怎麼處理？", llm, "m", "fast",
                           search=lambda q: searched.append(q) or PM)
    assert a.route == "pubmed" and a.status == "answered"
    assert [c.point_zh for c in a.cards] == ["心電圖變化先給鈣", "胰島素 15 分鐘"]
    assert a.cards[0].source == "Long B et al. J Emerg Med 2018;55(2):192-205. PMID 29731287"
    assert searched == ["hyperkalemia AND calcium"] and llm.calls[0]["model"] == "fast"
    assert "PMID 29731287" in llm.calls[1]["prompt"] and PM[0].abstract in llm.calls[1]["prompt"]
    assert a.references[1]["journal"] == "J Emerg Med" and a.references[1]["authors"] == "Long B, Warix JR"


def test_pubmed_retries_broader_query_when_no_hits():
    searched = []
    llm = FakeLLM(QUERY, {"status": "not_found", "summary_zh": "", "clarify_zh": "", "cards": []})
    a = answer_from_pubmed("q", llm, "m", "fast", search=lambda q: searched.append(q) or ([] if len(searched) == 1 else PM))
    assert searched == ["hyperkalemia AND calcium", "hyperkalemia treatment"] and a.status == "not_found"


def test_pubmed_no_articles_is_not_found():
    a = answer_from_pubmed("q", FakeLLM(QUERY), "m", "fast", search=lambda q: [])
    assert a.status == "not_found" and "PubMed" in a.summary_zh


def test_pubmed_network_failure_is_error():
    def down(q):
        raise PubMedError("PubMed 暫時無法連線")

    a = answer_from_pubmed("q", FakeLLM(QUERY), "m", "fast", search=down)
    assert a.status == "error" and a.error == "PubMed 暫時無法連線"


def test_openevidence_citation_includes_doi():
    from xiaoshen.answering import citation_label

    c = {"index": 3, "first_author": "Vivarelli", "year": "2023", "journal": "Lancet", "doi": "10.1016/S0140"}
    assert citation_label(c) == "[3] Vivarelli 2023, Lancet. doi:10.1016/S0140"


# ---- 最後一層：Gemini 自身知識（標示未經文獻驗證、不准編文獻）----

from xiaoshen.answering import MODEL_KNOWLEDGE_LABEL, answer_from_model_knowledge  # noqa: E402


def test_model_knowledge_answer_is_labeled_and_has_no_citations():
    llm = FakeLLM({"status": "answered", "summary_zh": "s", "clarify_zh": "",
                   "cards": [{"point_zh": "先給 calcium gluconate 穩定心肌"}, {"point_zh": ""}]})
    a = answer_from_model_knowledge("高血鉀心電圖變化怎麼處理？", llm, "pro")
    assert a.route == "model" and a.status == "answered" and llm.calls[0]["model"] == "pro"
    assert [c.point_zh for c in a.cards] == ["先給 calcium gluconate 穩定心肌"]
    assert a.cards[0].source == MODEL_KNOWLEDGE_LABEL == "Gemini 一般知識（未經文獻驗證）"
    assert a.cards[0].quote == "" and a.references == {}
    assert "不得列出" in llm.calls[0]["system"] and "文獻" in llm.calls[0]["system"]


def test_model_knowledge_clarify_and_error():
    a = answer_from_model_knowledge("劑量？", FakeLLM({"status": "clarify", "summary_zh": "", "clarify_zh": "成人或兒童？",
                                                    "cards": []}), "pro")
    assert a.status == "clarify" and a.clarify_zh == "成人或兒童？"
    b = answer_from_model_knowledge("q", FakeLLM(exc=LLMError("quota")), "pro")
    assert b.status == "error"


# ---- 語音名詞校正：不限今天簡報，用醫學知識（實例 Giotomine → Gitelman）----

from xiaoshen.answering import interpret_spoken  # noqa: E402


def test_interpret_spoken_works_without_deck_terms():
    llm = FakeLLM({"question": "Gitelman syndrome 的治療有哪些？", "unsure": False, "clarify_zh": ""})
    r = interpret_spoken("Giotomine syndrome 的治療有哪些？", [], llm, "fast")
    assert r.question == "Gitelman syndrome 的治療有哪些？" and r.clarify_zh == ""
    assert llm.calls[0]["model"] == "fast" and "醫學知識" in llm.calls[0]["system"]


def test_interpret_spoken_unsure_asks_back():
    llm = FakeLLM({"question": "Gitelman syndrome 的治療？", "unsure": True, "clarify_zh": "你是說 Gitelman syndrome 嗎？"})
    r = interpret_spoken("Giotomine syndrome 的治療？", ["SRNS"], llm, "fast")
    assert r.clarify_zh == "你是說 Gitelman syndrome 嗎？" and "SRNS" in llm.calls[0]["prompt"]


def test_interpret_spoken_unsure_but_unchanged_does_not_ask():
    r = interpret_spoken("SRNS 的治療？", [], FakeLLM({"question": "SRNS 的治療？", "unsure": True, "clarify_zh": "?"}), "f")
    assert r.question == "SRNS 的治療？" and r.clarify_zh == ""


def test_interpret_spoken_failure_keeps_original():
    r = interpret_spoken("q", [], FakeLLM(exc=LLMError("x")), "f")
    assert r.question == "q" and r.clarify_zh == ""


# ---- §6.2c：OpenEvidence 結果完整照原文做成 OE 簡報，不再由助理濃縮成字卡（取代舊的 OE 字卡驗證測試）----

def test_openevidence_answer_kept_verbatim_without_cards():
    from xiaoshen.answering import answer_from_openevidence
    from xiaoshen.oe_client import OEResult

    raw = "# T\n\nCalcineurin inhibitors are first-line [3]."
    oe = OEResult("a1", raw, [{"index": 3, "title": "Paper", "journal": "J", "year": "2024", "doi": ""}], raw=raw)
    a = answer_from_openevidence("q", "oe.py", FakeLLM({}), "m", ask=lambda q, s: oe)
    assert a.status == "answered" and a.cards == [] and a.oe is oe and a.references[3]["title"] == "Paper"


def test_openevidence_empty_answer_not_found():
    from xiaoshen.answering import answer_from_openevidence
    from xiaoshen.oe_client import OEResult

    a = answer_from_openevidence("q", "oe.py", FakeLLM({}), "m", ask=lambda q, s: OEResult("a1", " ", [], raw=""))
    assert a.status == "not_found"


def test_openevidence_no_model_call():
    from xiaoshen.answering import answer_from_openevidence
    from xiaoshen.oe_client import OEResult

    llm = FakeLLM({})
    answer_from_openevidence("q", "oe.py", llm, "m", ask=lambda q, s: OEResult("a1", "text", [], raw="text"))
    assert llm.calls == []  # 不改寫原文


def test_clarify_question():
    from xiaoshen.answering import clarify_question
    from xiaoshen.llm import LLMError

    assert clarify_question("劑量？", FakeLLM({"clarify": True, "question": "成人還是兒童？"}), "m") == "成人還是兒童？"
    assert clarify_question("SRNS 一線？", FakeLLM({"clarify": False, "question": ""}), "m") == ""
    assert clarify_question("x", FakeLLM(exc=LLMError("busy")), "m") == ""

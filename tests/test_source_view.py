"""顯示出處原文（spec §6.2a）：文獻摘要＋引用句、簡報／參考資料那一頁、原文網頁。"""

import json
from pathlib import Path

import pytest

from xiaoshen.answering import Answer, Card
from xiaoshen.pubmed import PubMedRecord, lookup_doi, pmc_id
from xiaoshen.source_view import SourceViewer, highlight_phrases

ABS = "BACKGROUND: Hyperkalemia is common. CONCLUSIONS: Calcium should be given for ECG changes to stabilize the myocardium."
PM_REF = {"index": 1, "title": "Controversies in Management of Hyperkalemia.", "journal": "J Emerg Med", "year": "2018",
          "doi": "10.1016/x", "first_author": "Long B", "authors": "Long B, Warix JR", "pmid": "29731287",
          "abstract": ABS}
OE_REF = {"index": 3, "title": "Childhood nephrotic syndrome", "journal": "Lancet", "year": "2023",
          "doi": "10.1016/S0140", "first_author": "Vivarelli", "authors": "Vivarelli M"}


class FakeWeb:
    def __init__(self):
        self.opened, self.closed = [], 0

    def open(self, url):
        self.opened.append(url)

    def close(self):
        self.closed += 1


def answered(route, cards, refs=None):
    return Answer("a1", "q", route, "answered", cards=cards, references=refs or {})


def viewer(state, http=None, web=None, library=None):
    return SourceViewer(state, library, http_get=http or (lambda u: (_ for _ in ()).throw(AssertionError(u))),
                        web=web or FakeWeb())


# ---- PubMed 輔助 ----

EFETCH = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>29731287</PMID><Article>
<Journal><JournalIssue><PubDate><Year>2018</Year></PubDate></JournalIssue><ISOAbbreviation>J Emerg Med</ISOAbbreviation></Journal>
<ArticleTitle>Controversies.</ArticleTitle><ELocationID EIdType="doi" ValidYN="Y">10.1016/S0140</ELocationID><Abstract><AbstractText>Calcium gluconate should be given first for ECG changes.</AbstractText></Abstract>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""


def test_lookup_doi_finds_pubmed_record():
    calls = []

    def get(url):
        calls.append(url)
        return json.dumps({"esearchresult": {"idlist": ["29731287"]}}) if "esearch" in url else EFETCH

    rec = lookup_doi("10.1016/S0140", http_get=get)
    assert rec.pmid == "29731287" and rec.abstract == "Calcium gluconate should be given first for ECG changes."
    assert "%5Bdoi%5D" in calls[0] or "[doi]" in calls[0]


def test_lookup_doi_not_found():
    assert lookup_doi("10.1/none", http_get=lambda u: json.dumps({"esearchresult": {"idlist": []}})) is None


def test_pmc_id():
    resp = {"records": [{"pmid": 40337980, "pmcid": "PMC12060654"}]}
    assert pmc_id("40337980", http_get=lambda u: json.dumps(resp)) == "PMC12060654"
    none = {"records": [{"pmid": 1, "status": "error", "errmsg": "Identifier not found in PMC"}]}
    assert pmc_id("1", http_get=lambda u: json.dumps(none)) == ""


def test_pubmed_reference_keeps_abstract_for_display():
    r = PubMedRecord("9", "T", ["Ho K"], "Lancet", "2021", "", "", "", "", "Abstract text.")
    assert r.reference(1)["abstract"] == "Abstract text."


# ---- 摘要 ----

def test_show_pubmed_abstract_with_quote_marked(state):
    state.answer = answered("pubmed", [Card("先給鈣", "Calcium should be given for ECG changes", "Long B et al. …",
                                            refs=[1])], {1: PM_REF})
    r = viewer(state).show(1)
    assert r["status"] == "ok" and r["message"] == "已顯示第 1 張的摘要"
    v = state.source_view
    assert v["kind"] == "abstract" and v["card"] == 1 and v["title"] == PM_REF["title"]
    s, e = v["mark"]
    assert v["text"][s:e] == "Calcium should be given for ECG changes"
    assert state.snapshot("display")["source_view"]["kind"] == "abstract"


def test_show_openevidence_abstract_looks_up_doi(state):
    state.answer = answered("openevidence", [Card("CNI", "Calcium gluconate should be given first", "[3] Vivarelli 2023, Lancet",
                                                  refs=[3])], {3: dict(OE_REF)})

    def get(url):
        return json.dumps({"esearchresult": {"idlist": ["29731287"]}}) if "esearch" in url else EFETCH

    assert viewer(state, http=get).show(1)["status"] == "ok"
    v = state.source_view
    assert v["text"] == "Calcium gluconate should be given first for ECG changes." and v["mark"] is not None


def test_openevidence_abstract_not_in_pubmed_opens_web_instead(state):
    # 沒有摘要就直接開原文網頁
    state.answer = answered("openevidence", [Card("CNI", "x", "[3] V", refs=[3])], {3: dict(OE_REF)})
    web = FakeWeb()
    r = viewer(state, http=lambda u: json.dumps({"esearchresult": {"idlist": []}}), web=web).show(1)
    assert r["status"] == "ok" and "沒有摘要" in r["message"]
    assert web.opened == ["https://doi.org/10.1016/S0140"] and state.source_view["kind"] == "web"


def test_no_abstract_without_web_window_keeps_old_hint(state):
    state.answer = answered("openevidence", [Card("CNI", "x", "[3] V", refs=[3])], {3: dict(OE_REF)})
    v = SourceViewer(state, None, http_get=lambda u: json.dumps({"esearchresult": {"idlist": []}}), web=None)
    r = v.show(1)
    assert r["status"] == "no_abstract" and state.source_view["kind"] == "abstract"


KDIGO_REF = {"index": 4, "title": "KDIGO 2025 Clinical Practice Guideline", "journal": "", "year": "2025", "doi": "",
             "first_author": "", "authors": "", "pmid": "",
             "href": "https://kdigo.org/wp-content/uploads/2025/04/KDIGO-2025-Guideline-for-Nephrotic-Syndrome-in-Children.pdf"}


def test_guideline_pdf_without_doi_opens_oe_link(state):
    """2026-11-05 實機：字卡出處是 KDIGO 官網 PDF（沒有 DOI／PMID）→ 叫出原文直接開那份 PDF。"""
    for kind in ("auto", "abstract", "web"):
        state.answer = answered("openevidence", [Card("類固醇 8 或 12 週", "x", "[4] KDIGO", refs=[4])],
                                {4: dict(KDIGO_REF)})
        web = FakeWeb()
        r = viewer(state, web=web).show(1, kind)  # 不准上網查：沒有 DOI／PMID 可查
        assert r["status"] == "ok", kind
        assert web.opened == [KDIGO_REF["href"]] and state.source_view["kind"] == "web"


def test_no_abstract_and_no_link_says_so(state):
    ref = dict(KDIGO_REF, href="")
    state.answer = answered("openevidence", [Card("a", "x", "[4] KDIGO", refs=[4])], {4: ref})
    r = viewer(state).show(1)
    assert r["status"] == "no_abstract" and "打開原文網頁" not in r["message"] and "網址" in r["message"]


def test_openevidence_pmid_fetches_abstract_directly(state):
    calls = []

    def get(url):
        calls.append(url)
        return EFETCH

    state.answer = answered("openevidence", [Card("CNI", "Calcium gluconate should be given first", "[3] V", refs=[3])],
                            {3: dict(OE_REF, pmid="29731287")})
    assert viewer(state, http=get).show(1)["status"] == "ok"
    assert state.source_view["kind"] == "abstract" and state.source_view["mark"] is not None
    assert len(calls) == 1 and "efetch" in calls[0]  # 有 PMID 就不必用 DOI 搜尋


def test_web_ignores_non_http_link(state):
    ref = dict(KDIGO_REF, href="file:///C:/Windows/system.ini")
    state.answer = answered("openevidence", [Card("a", "x", "[4] KDIGO", refs=[4])], {4: ref})
    web = FakeWeb()
    r = viewer(state, web=web).show(1, "web")
    assert r["status"] == "no_source" and web.opened == []


# ---- 那一頁 ----

def test_show_deck_page(state):
    state.answer = answered("deck", [Card("CNI 一線", "We recommend calcineurin", "今天的簡報 第 2 頁",
                                          doc="今天的簡報", page=2)])
    r = viewer(state).show(1)
    assert r["status"] == "ok" and r["message"] == "已顯示第 1 張的那一頁"
    v = state.source_view
    assert v["kind"] == "page" and v["page"] == 2 and v["quote"] == "We recommend calcineurin"
    assert v["url"].startswith("/source/page.png?v=")


def test_page_png_has_highlight_and_is_png(state):
    state.answer = answered("deck", [Card("p", "Case 1 Case 1", "今天的簡報 第 1 頁", doc="今天的簡報", page=1)])
    vw = viewer(state)
    vw.show(1)
    png = vw.page_png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    plain = state.deck.render_png(0, width=1600)
    assert png != plain  # 有螢光框


def test_web_request_on_page_card_shows_page(state):
    state.answer = answered("deck", [Card("p", "Case 1 Case 1", "今天的簡報 第 1 頁", doc="今天的簡報", page=1)])
    web = FakeWeb()
    r = viewer(state, web=web).show(1, "web")
    assert state.source_view["kind"] == "page" and web.opened == [] and "簡報" in r["message"]


# ---- 原文網頁 ----

def test_web_prefers_pmc_then_doi_then_pubmed(state):
    state.answer = answered("pubmed", [Card("a", "x", "c", refs=[1])], {1: PM_REF})
    web = FakeWeb()
    pmc = json.dumps({"records": [{"pmid": 29731287, "pmcid": "PMC1"}]})
    viewer(state, http=lambda u: pmc, web=web).show(1, "web")
    assert web.opened == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC1/"] and state.source_view["kind"] == "web"
    none = json.dumps({"records": [{"pmid": 29731287, "status": "error"}]})
    viewer(state, http=lambda u: none, web=web).show(1, "web")
    assert web.opened[-1] == "https://doi.org/10.1016/x"
    ref = dict(PM_REF, doi="")
    state.answer = answered("pubmed", [Card("a", "x", "c", refs=[1])], {1: ref})
    viewer(state, http=lambda u: none, web=web).show(1, "web")
    assert web.opened[-1] == "https://pubmed.ncbi.nlm.nih.gov/29731287/"


def test_close_hides_view_and_closes_web(state):
    state.answer = answered("pubmed", [Card("a", "x", "c", refs=[1])], {1: PM_REF})
    web = FakeWeb()
    vw = viewer(state, http=lambda u: json.dumps({"records": []}), web=web)
    vw.show(1, "web")
    assert vw.close()["status"] == "ok"
    state.wait_web_close()
    assert state.source_view is None and web.closed >= 1


# ---- 不能顯示的情況 ----

def test_model_knowledge_card_has_no_source(state):
    state.answer = answered("model", [Card("一般知識", "", "Gemini 一般知識（未經文獻驗證）")])
    r = viewer(state).show(1)
    assert r == {"status": "no_source", "message": "這張沒有文獻出處"} and state.source_view is None


def test_card_number_out_of_range_and_no_answer(state):
    state.answer = None
    assert viewer(state).show(1)["status"] == "no_answer"
    state.answer = answered("pubmed", [Card("a", "x", "c", refs=[1])], {1: PM_REF})
    assert viewer(state).show(3) == {"status": "not_found", "message": "沒有第 3 張字卡"}


def test_new_answer_closes_source_view(state):
    web = FakeWeb()
    state.web_closer = web.close
    state.source_view = {"kind": "web"}
    state.set_answer(answered("deck", []))
    state.wait_web_close()
    assert state.source_view is None and web.closed == 1


@pytest.mark.parametrize("quote,expect", [
    ("Calcium should be given for ECG changes to stabilize the myocardium",
     ["Calcium should be given", "for ECG changes to", "stabilize the myocardium"]),
    ("高血鉀的緊急處置要先穩定心肌", ["高血鉀的緊急處", "置要先穩定心肌"]),
])
def test_highlight_phrases(quote, expect):
    assert highlight_phrases(quote) == expect



# ---- 整支審查修正（2026-11-05）----

import threading as _th  # noqa: E402


def test_slow_lookup_dropped_when_new_answer_arrives(state):
    # 審查 #1：查 DOI 期間來了新回答／按了關閉，舊結果不能蓋上投影、也不能開網頁
    gate, started = _th.Event(), _th.Event()

    def slow(url):
        started.set()
        gate.wait(5)
        return json.dumps({"esearchresult": {"idlist": ["29731287"]}}) if "esearch" in url else EFETCH

    state.answer = answered("openevidence", [Card("CNI", "Calcium gluconate should be given first", "[3] V", refs=[3])],
                            {3: dict(OE_REF)})
    web = FakeWeb()
    vw = viewer(state, http=slow, web=web)
    out = {}
    t = _th.Thread(target=lambda: out.update(r=vw.show(1)))
    t.start()
    started.wait(5)
    with state.lock:
        state.set_answer(answered("deck", []))
    gate.set()
    t.join(5)
    assert state.source_view is None and out["r"]["status"] == "stale"


def test_slow_web_lookup_does_not_open_after_close(state):
    gate, started = _th.Event(), _th.Event()

    def slow(url):
        started.set()
        gate.wait(5)
        return json.dumps({"records": [{"pmid": 29731287, "pmcid": "PMC1"}]})

    state.answer = answered("pubmed", [Card("a", "x", "c", refs=[1])], {1: dict(PM_REF)})
    web = FakeWeb()
    vw = viewer(state, http=slow, web=web)
    t = _th.Thread(target=lambda: vw.show(1, "web"))
    t.start()
    started.wait(5)
    vw.close()
    gate.set()
    t.join(5)
    assert web.opened == [] and state.source_view is None


def test_deck_switched_after_answer_refuses_wrong_page(state, make_pdf):
    # 審查 #2：回答時的簡報已換掉，不能拿新簡報的同一頁冒充
    ans = answered("deck", [Card("p", "Case 2 Case 2", "今天的簡報 第 2 頁", doc="今天的簡報", page=2)])
    ans.deck_path, ans.deck_hash = str(state.deck.path), state.deck.content_hash
    state.answer = ans
    other = make_pdf(["Other " * 5], folder=state.config.folder("簡報"), name="E.pdf")
    state.play(other)
    state.deck.path.parent.joinpath("D.pdf").unlink()  # 原本的檔案也不在了
    r = viewer(state).show(1)
    assert r["status"] == "no_source" and "換" in r["message"] and state.source_view is None


def test_deck_switched_but_old_file_still_there_shows_old_page(state, make_pdf):
    ans = answered("deck", [Card("p", "Case 2 Case 2", "今天的簡報 第 2 頁", doc="今天的簡報", page=2)])
    ans.deck_path, ans.deck_hash = str(state.deck.path), state.deck.content_hash
    old_png = state.deck.render_png(1, width=1600, phrases=("Case 2 Case 2",))
    state.answer = ans
    state.play(make_pdf(["Other " * 5], folder=state.config.folder("簡報"), name="E.pdf"))
    vw = viewer(state)
    assert vw.show(1)["status"] == "ok" and vw.page_png() == old_png


def test_page_out_of_range_refused(state):
    state.answer = answered("deck", [Card("p", "x", "今天的簡報 第 9 頁", doc="今天的簡報", page=9)])
    assert viewer(state).show(1)["status"] == "no_source"


def test_web_close_runs_outside_state_lock(state):
    # 審查 #3：關網頁視窗（taskkill 可能 10 秒）不能佔著 state.lock
    seen = []

    class LockProbe(FakeWeb):
        def close(self):
            got = state.lock.acquire(blocking=False) if False else None
            seen.append(_th.current_thread() is _th.main_thread())

    state.web_closer = LockProbe().close
    state.source_view = {"kind": "web"}
    with state.lock:
        state.close_source_view()
    state.wait_web_close(2)
    assert seen == [False]  # 在別的執行緒關，不在持有鎖的呼叫者裡


def test_on_demand_lookups_use_short_timeout():
    # 審查 #4：叫出原文時的網路查詢要短逾時（語音那一輪不能卡 40～60 秒）
    from xiaoshen.source_view import LOOKUP_TIMEOUT
    assert LOOKUP_TIMEOUT <= 6


def test_web_without_pmid_opens_doi_directly(state):
    state.answer = answered("openevidence", [Card("a", "x", "c", refs=[3])], {3: dict(OE_REF)})
    web = FakeWeb()
    viewer(state, web=web).show(1, "web")  # http 會拋 AssertionError：不准連網
    assert web.opened == ["https://doi.org/10.1016/S0140"]


def test_lookup_doi_quotes_and_checks_doi():
    # 審查 #5：沒核對 DOI 就用第一筆，可能顯示別篇的摘要
    calls = []

    def get(url):
        calls.append(url)
        return json.dumps({"esearchresult": {"idlist": ["29731287"]}}) if "esearch" in url else EFETCH

    assert lookup_doi("10.1016/x", http_get=get) is None  # EFETCH 那篇沒有這個 DOI
    assert "%2210.1016%2Fx%22" in calls[0] or '"10.1016/x"' in calls[0]


def test_edge_window_open_is_serialized_and_kills_profile_processes(tmp_path):
    # 審查 #6：連按兩次不能多開一個視窗；關閉要連同這個設定檔的所有 Edge 一起關
    from xiaoshen.source_view import EdgeWindow

    launched, killed = [], []

    class P:
        def __init__(self, args):
            self.pid = len(launched) + 100
            launched.append(args)

    win = EdgeWindow(Path("msedge.exe"), tmp_path / "edge-source", (10, 20), popen=P,
                     kill_profile=lambda profile: killed.append(str(profile)))
    ts = [_th.Thread(target=win.open, args=(f"https://x/{i}",)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    win.close()
    assert len(launched) == 2 and len(killed) == 3  # 每次開之前都先清掉，最後再關一次
    assert all("--hide-crash-restore-bubble" in a for a in launched)


@pytest.mark.parametrize("quote,expect", [
    ("a1 b1 c1 d1 e1", ["a1 b1 c1 d1 e1"]),                                 # 尾巴只剩 1 個字：併進前一段
    ("w1 w2 w3 w4 w5 w6 w7 w8 w9", ["w1 w2 w3 w4", "w5 w6 w7 w8 w9"]),
    ("as", []),                                                            # 太短：不標（會到處亂標）
])
def test_highlight_phrases_no_short_chunks(quote, expect):
    # 審查 #7
    assert highlight_phrases(quote) == expect


def test_quiz_start_and_deck_play_close_source_view(state, bank_question):
    # 審查 #9：原文畫面蓋在最上層，開始作答／換簡報時要關掉
    state.source_view = {"kind": "abstract"}
    state.start_run("single", [bank_question])
    assert state.source_view is None
    state.source_view = {"kind": "abstract"}
    state.stop_presenting()
    assert state.source_view is None


def test_show_errors_return_message_not_500(state):
    # 審查 #10：查詢途中的意外錯誤要回訊息，不是讓主持人畫面什麼都沒有
    state.answer = answered("pubmed", [Card("a", "x", "c", refs=[1])], {1: dict(PM_REF, doi="")})

    def boom(url):
        raise RuntimeError("weird")

    r = viewer(state, http=boom).show(1, "web")
    assert r["status"] == "error"


def test_unmarked_abstract_explains_why(state):
    # 審查 #13：OpenEvidence 的引用句是它自己的整理，摘要裡通常找不到逐字相同的句子
    state.answer = answered("pubmed", [Card("a", "Some sentence not present anywhere in the abstract", "c", refs=[1])],
                            {1: dict(PM_REF)})
    viewer(state).show(1)
    assert state.source_view["mark"] is None and "逐字" in state.source_view["note"]


# ---- OE 簡報（§6.2c）：在投影幕點參考文獻 ----

from xiaoshen.oe_client import OEResult  # noqa: E402


def _oe_answer(state, citations, others=()):
    oe = OEResult("a1", "text [3]", list(citations), raw="text [3]", other_citations=list(others))
    state.answer = Answer("a1", "q", "openevidence", "answered", references={c["index"]: c for c in citations}, oe=oe)
    return oe


def test_open_reference_mapped_uses_pmc_order(state):
    _oe_answer(state, [dict(PM_REF, index=3)])
    web = FakeWeb()
    pmc = json.dumps({"records": [{"pmid": 29731287, "pmcid": "PMC9"}]})
    r = viewer(state, http=lambda u: pmc, web=web).open_reference("n:3")
    assert r["status"] == "ok" and web.opened == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC9/"]
    assert state.source_view["kind"] == "web" and state.source_view["ref"] == "n:3"


def test_open_reference_other_citation(state):
    other = {"index": None, "title": "Other", "doi": "", "pmid": "", "href": "https://kdigo.org/x.pdf"}
    _oe_answer(state, [], [other])
    web = FakeWeb()
    assert viewer(state, web=web).open_reference("o:0")["status"] == "ok"
    assert web.opened == ["https://kdigo.org/x.pdf"]


def test_open_reference_without_url(state):
    _oe_answer(state, [{"index": 3, "title": "No link", "doi": "", "pmid": "", "href": ""}])
    web = FakeWeb()
    r = viewer(state, web=web).open_reference("n:3")
    assert r == {"status": "no_source", "message": "這篇沒有可以開的原文網址"} and web.opened == []


def test_open_reference_unknown(state):
    _oe_answer(state, [])
    assert viewer(state).open_reference("n:9")["status"] == "not_found"
    assert viewer(state).open_reference("bad")["status"] == "not_found"


def test_read_full_after_open_reference(state):
    import pymupdf

    other = {"index": None, "title": "KDIGO PDF", "doi": "", "pmid": "", "href": "https://kdigo.org/x.pdf"}
    _oe_answer(state, [], [other])
    v = viewer(state, web=FakeWeb())
    v.open_reference("o:0")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Guideline text page one", fontsize=12)
    data = doc.tobytes()
    full = v.read_full(get_bytes=lambda url: (data, "application/pdf"))
    assert full.title == "KDIGO PDF" and full.full_text and "Guideline text" in full.pages[0]



def test_open_reference_uses_playing_deck_not_current_answer(state, tmp_path):
    # C2：OE 簡報 A 在投影上，之後的回答 B 不能讓點 A 的 [3] 開到 B 的文獻
    from xiaoshen.oe_deck import render_oe_deck

    a_ref = {"index": 3, "title": "Deck A paper", "doi": "10.1/a", "pmid": "", "href": ""}
    oe_a = OEResult("a", "text [3]", [a_ref], raw="text [3]")
    pdf = tmp_path / "a.pdf"
    render_oe_deck("q", oe_a, "", pdf)
    state.play_summary(pdf, kind="oe")
    _oe_answer(state, [{"index": 3, "title": "Answer B paper", "doi": "10.1/b", "pmid": "", "href": ""}])
    web = FakeWeb()
    assert viewer(state, web=web).open_reference("n:3")["status"] == "ok"
    assert web.opened == ["https://doi.org/10.1/a"]
    assert state.source_view["citation"] == "Deck A paper"


def test_edge_window_window_mode_has_no_fullscreen(tmp_path):
    from xiaoshen.source_view import EdgeWindow

    launched = []
    for kiosk in (True, False):
        win = EdgeWindow(Path("msedge.exe"), tmp_path / "s", (10, 20), popen=launched.append,
                         kill_profile=lambda p: None, kiosk=kiosk)
        win.open("https://x/")
    assert "--start-fullscreen" in launched[0]
    assert "--start-fullscreen" not in launched[1]
    assert "--app=https://x/" in launched[1] and "--window-position=10,20" in launched[1]
    assert "--window-size=1280,720" in launched[1]

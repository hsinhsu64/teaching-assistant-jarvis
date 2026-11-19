"""原文重點摘要簡報（spec §6.2b）：讀出投影上那份原文的全文。"""

import json

import pymupdf
import pytest

from xiaoshen.answering import Answer, Card
from xiaoshen.fulltext import NeedsFallback
from xiaoshen.pubmed import pmc_fulltext
from xiaoshen.source_text import SourceUnreadable, pdf_pages
from xiaoshen.source_view import SourceViewer


def _pdf(texts: list[str]) -> bytes:
    doc = pymupdf.open()
    for t in texts:
        page = doc.new_page()
        if t:
            page.insert_text((72, 72), t, fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


PMC_XML = """<pmc-articleset><article><front><article-meta>
<title-group><article-title>T</article-title></title-group>
<abstract><p>A</p></abstract></article-meta></front>
<body><sec><title>Methods</title><p>M1</p></sec></body></article></pmc-articleset>"""

IDCONV_PMC = json.dumps({"records": [{"pmid": 1, "pmcid": "PMC1"}]})


class FakeWeb:
    def open(self, url):
        pass

    def close(self):
        pass


def _no_http(url):
    raise AssertionError(f"unexpected request {url}")


def _state_with(state, ref, view):
    state.answer = Answer("a1", "q", "openevidence", "answered",
                          cards=[Card("重點", "x", "[1] KDIGO", refs=[1])], references={1: ref})
    state.source_view = view
    return state


def test_pdf_pages_keeps_page_order():
    pages = pdf_pages(_pdf(["alpha", "beta", "gamma"]))
    assert [p.strip() for p in pages] == ["alpha", "beta", "gamma"]


def test_scanned_pdf_reports_unreadable():
    with pytest.raises(SourceUnreadable, match="掃描檔"):
        pdf_pages(_pdf(["", ""]))


def test_not_a_pdf_reports_unreadable():
    with pytest.raises(SourceUnreadable, match="讀不到這份 PDF"):
        pdf_pages(b"<html>login</html>")


def test_pmc_fulltext_reads_body_sections():
    text = pmc_fulltext("PMC1", http_get=lambda u: PMC_XML)
    order = [text.index(s) for s in ("T", "A", "Methods", "M1")]
    assert order == sorted(order)


def test_pmc_fulltext_without_body_is_empty():
    xml = "<pmc-articleset><article><front><article-meta><abstract><p>A</p></abstract></article-meta></front></article></pmc-articleset>"
    assert pmc_fulltext("PMC1", http_get=lambda u: xml) == ""


def test_read_full_web_pdf(state):
    ref = {"title": "KDIGO 2025", "doi": "", "pmid": "", "href": "https://kdigo.org/x.pdf"}
    _state_with(state, ref, {"kind": "web", "card": 1, "citation": "[1] KDIGO", "url": "https://kdigo.org/x.pdf"})
    v = SourceViewer(state, None, http_get=_no_http, web=FakeWeb())
    doc = v.read_full(get_bytes=lambda url: (_pdf(["p1", "p2", "p3"]), "application/pdf"))
    assert doc.paged is True and doc.full_text is True and len(doc.pages) == 3
    assert doc.title == "KDIGO 2025"


def test_web_pdf_link_returning_html_falls_back_to_abstract(state):
    ref = {"title": "Paper", "doi": "", "pmid": "", "abstract": "ABS", "href": "https://x/y.pdf"}
    _state_with(state, ref, {"kind": "web", "card": 1, "citation": "[1] P", "url": "https://x/y.pdf"})
    v = SourceViewer(state, None, http_get=_no_http, web=FakeWeb())
    # 2026-11-18：找不到全文改丟 NeedsFallback，由摘要工作改請 OE 整理，再不行才讀摘要
    with pytest.raises(NeedsFallback):
        v.read_full(get_bytes=lambda url: (b"<html>login</html>", "text/html"))
    doc = v.abstract_doc(ref, "[1] P")
    assert doc.full_text is False and doc.pages == ["ABS"] and "只讀到摘要" in doc.note and doc.origin == "abstract"


def test_read_full_abstract_prefers_pmc(state):
    ref = {"title": "Paper", "doi": "10.1/x", "pmid": "1", "abstract": "ABS"}
    _state_with(state, ref, {"kind": "abstract", "card": 1, "citation": "[1] P", "text": "ABS"})

    def get(url):
        return IDCONV_PMC if "idconv" in url else PMC_XML

    doc = SourceViewer(state, None, http_get=get, web=FakeWeb()).read_full(get_bytes=_no_http)
    assert doc.full_text is True and doc.paged is False and "M1" in doc.pages[0]


def test_read_full_abstract_without_pmc_uses_abstract(state):
    ref = {"title": "Paper", "doi": "10.1/x", "pmid": "1", "abstract": "ABS"}
    _state_with(state, ref, {"kind": "abstract", "card": 1, "citation": "[1] P", "text": "ABS"})
    none = json.dumps({"records": [{"pmid": 1, "status": "error"}]})
    v = SourceViewer(state, None, http_get=lambda u: none, web=FakeWeb())
    with pytest.raises(NeedsFallback) as e:
        v.read_full(get_bytes=_no_http)
    assert e.value.ref["title"] == "Paper" and v.current_ref()["title"] == "Paper"


def test_web_pmc_url_reads_pmc_fulltext(state):
    ref = {"title": "Paper", "doi": "", "pmid": "1"}
    url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/"
    _state_with(state, ref, {"kind": "web", "card": 1, "citation": "[1] P", "url": url})
    doc = SourceViewer(state, None, http_get=lambda u: PMC_XML, web=FakeWeb()).read_full(get_bytes=_no_http)
    assert doc.full_text is True and "M1" in doc.pages[0]


def test_read_full_page_view_uses_whole_deck(state, make_pdf):
    from xiaoshen.source_view import DECK_LABEL

    path = make_pdf(["Page one alpha", "Page two beta", "Page three gamma"])
    state.open_deck(path)
    state.answer = Answer("a1", "q", "deck", "answered",
                          cards=[Card("重點", "Page two beta", "今天的簡報 p.2", doc=DECK_LABEL, page=2)])
    v = SourceViewer(state, None, http_get=_no_http, web=FakeWeb())
    assert v.show(1)["status"] == "ok" and state.source_view["kind"] == "page"
    doc = v.read_full(get_bytes=_no_http)
    assert doc.paged is True and doc.full_text is True and len(doc.pages) == 3


def test_no_abstract_and_no_fulltext_is_unreadable(state):
    ref = {"title": "Paper", "doi": "", "pmid": "", "href": "https://x/page"}
    _state_with(state, ref, {"kind": "web", "card": 1, "citation": "[1] P", "url": "https://x/page"})
    v = SourceViewer(state, None, http_get=_no_http, web=FakeWeb())
    with pytest.raises(NeedsFallback):  # 先改請 OE 整理；OE 也不行才讀摘要
        v.read_full(get_bytes=lambda url: (b"<html></html>", "text/html"))
    with pytest.raises(SourceUnreadable, match="讀不到這篇的全文或摘要"):
        v.abstract_doc(ref, "[1] P")


def test_read_full_without_source_view(state):
    state.source_view = None
    with pytest.raises(SourceUnreadable, match="請先打開要整理的原文"):
        SourceViewer(state, None, http_get=_no_http, web=FakeWeb()).read_full(get_bytes=_no_http)



def test_pdf_pages_holds_pdf_lock(monkeypatch):
    # 審查 #4：MuPDF 不能多執行緒同時操作；和其他排版一樣要拿 PDF_LOCK
    from xiaoshen import source_text

    held = []

    class SpyLock:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(source_text, "PDF_LOCK", SpyLock())
    pdf_pages(_pdf(["alpha"]))
    assert held == [True]

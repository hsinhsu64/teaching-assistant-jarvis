import json

import pytest

from xiaoshen.fulltext import (ORIGIN_LABEL, europepmc_fulltext, fetch_full_text, find_library_pdf,
                               openalex_pdf_url)
from xiaoshen.library import Library

TITLE = "Clinical Practice Guideline for the Diagnosis and Treatment of Urinary Tract Infection in Children"
DOI = "10.1542/peds.2025-012345"
JATS = ("<article><front><article-meta><title-group><article-title>T</article-title></title-group>"
        "<abstract><p>Abstract text.</p></abstract></article-meta></front>"
        "<body><sec><title>Methods</title><p>Body text from Europe PMC.</p></sec></body></article>")
JATS_NO_BODY = "<article><front><article-meta><abstract><p>Only abstract.</p></abstract></article-meta></front></article>"


def ref(**kw):
    return {"index": 1, "title": TITLE, "first_author": "Roberts", "journal": "Pediatrics", "year": "2025",
            "doi": DOI, "pmid": "123", **kw}


@pytest.fixture
def lib(state_factory):
    st = state_factory()
    return Library(st.config.folder("參考資料"))


def fake_get(routes: dict, seen: list | None = None):
    def get(url):
        if seen is not None:
            seen.append(url)
        for key, body in routes.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise OSError("no route")
    return get


def no_bytes(url):
    raise AssertionError("should not download")


def test_library_pdf_matched_by_doi(lib, make_pdf):
    make_pdf([f"Some guideline\ndoi: {DOI.upper()}", "Recommendations"], folder=lib.folder, name="uti.pdf")
    lib.refresh()
    assert find_library_pdf(ref(), lib).name == "uti"


def test_library_pdf_matched_by_exact_title(lib, make_pdf):
    make_pdf([TITLE + "\nAmerican Academy of Pediatrics", "Key action statements"], folder=lib.folder, name="g.pdf")
    lib.refresh()
    assert find_library_pdf(ref(doi=""), lib).name == "g"


def test_library_pdf_similar_title_not_matched(lib, make_pdf):
    make_pdf(["KDIGO 2024 Clinical Practice Guideline for the Management of Glomerular Diseases"],
             folder=lib.folder, name="kdigo2024.pdf")
    lib.refresh()
    r = ref(title="KDIGO 2021 Clinical Practice Guideline for the Management of Glomerular Diseases", doi="")
    assert find_library_pdf(r, lib) is None


def test_order_library_first(lib, make_pdf):
    make_pdf([f"doi: {DOI}", "Full guideline text"], folder=lib.folder, name="uti.pdf")
    lib.refresh()
    seen = []
    doc = fetch_full_text(ref(), lib, fake_get({}, seen), no_bytes, lambda pmcid: "PMC text")
    assert doc.origin == "pdf" and doc.full_text and doc.paged and seen == []
    assert "Full guideline text" in doc.pages[1]


def test_pmc_used_when_no_library_pdf(lib):
    get = fake_get({"idconv": json.dumps({"records": [{"pmcid": "PMC9"}]})})
    doc = fetch_full_text(ref(), lib, get, no_bytes, lambda pmcid: "PMC body" if pmcid == "PMC9" else "")
    assert doc.origin == "pmc" and doc.pages == ["PMC body"] and not doc.paged


def test_pmc_then_europepmc_then_openalex(lib):
    pdf = b"%PDF-1.4 fake"
    get = fake_get({"idconv": json.dumps({"records": [{"pmcid": "PMC9"}]}),
                    "fullTextXML": JATS_NO_BODY,
                    "api.openalex.org": json.dumps({"best_oa_location": {"pdf_url": "https://oa.example/u.pdf"}})})

    def get_bytes(url):
        assert url == "https://oa.example/u.pdf"
        return pdf, "application/pdf"

    import xiaoshen.fulltext as ft
    orig = ft.pdf_pages
    ft.pdf_pages = lambda data: ["OA page 1", "OA page 2"]
    try:
        doc = fetch_full_text(ref(), lib, get, get_bytes, lambda pmcid: "")
    finally:
        ft.pdf_pages = orig
    assert doc.origin == "openalex" and doc.pages == ["OA page 1", "OA page 2"] and doc.paged


def test_europepmc_used_when_pmc_empty(lib):
    get = fake_get({"idconv": json.dumps({"records": [{"pmcid": "PMC9"}]}), "fullTextXML": JATS})
    doc = fetch_full_text(ref(), lib, get, no_bytes, lambda pmcid: "")
    assert doc.origin == "europepmc" and "Body text from Europe PMC." in doc.pages[0]


def test_openalex_html_not_pdf_skipped(lib):
    get = fake_get({"idconv": json.dumps({"records": []}),
                    "api.openalex.org": json.dumps({"best_oa_location": {"pdf_url": "https://x/landing"}})})
    assert fetch_full_text(ref(), lib, get, lambda url: (b"<html>", "text/html"), lambda pmcid: "") is None


def test_all_fail_returns_none(lib):
    assert fetch_full_text(ref(), lib, fake_get({}), no_bytes, lambda pmcid: "") is None


def test_europepmc_without_body_is_empty():
    assert europepmc_fulltext("PMC9", fake_get({"fullTextXML": JATS_NO_BODY})) == ""


def test_openalex_url_missing():
    assert openalex_pdf_url(DOI, fake_get({"api.openalex.org": json.dumps({"best_oa_location": None})})) == ""


def test_origin_labels():
    assert ORIGIN_LABEL["pdf"] == "使用者提供的 PDF" and ORIGIN_LABEL["oe"] == "OpenEvidence 整理"


# ---- 最後審查修正：參考資料比對不能認錯 ----

def test_library_doi_needs_boundary(lib, make_pdf):
    make_pdf(["Some paper doi: 10.1016/j.kint.2021.05.0215"], folder=lib.folder, name="other.pdf")
    lib.refresh()
    assert find_library_pdf(ref(doi="10.1016/j.kint.2021.05.021", title="x"), lib) is None


def test_library_doi_on_page_two_not_matched(lib, make_pdf):
    make_pdf(["A narrative review of pediatric UTI", f"References\n12. Roberts. doi:{DOI}"], folder=lib.folder,
             name="review.pdf")
    lib.refresh()
    assert find_library_pdf(ref(title="Some other title that is long enough"), lib) is None


def test_library_title_cited_in_body_not_matched(lib, make_pdf):
    body = "Reaffirmation of the guideline.\n" + "This statement reviews the evidence published since.\n" * 8
    make_pdf([body + "It reaffirms\n" + TITLE + "."], folder=lib.folder, name="reaffirm.pdf")
    lib.refresh()
    assert find_library_pdf(ref(doi=""), lib) is None


def test_library_doi_match_preferred_over_title_match(lib, make_pdf):
    make_pdf([TITLE + "\nan older copy without DOI"], folder=lib.folder, name="a_title.pdf")
    make_pdf([f"Guideline\ndoi: {DOI}"], folder=lib.folder, name="b_doi.pdf")
    lib.refresh()
    assert find_library_pdf(ref(), lib).name == "b_doi"


def test_library_title_rejected_when_page_has_other_doi(lib, make_pdf):
    make_pdf([TITLE + "\ndoi: 10.9999/another.edition"], folder=lib.folder, name="other_edition.pdf")
    lib.refresh()
    assert find_library_pdf(ref(), lib) is None

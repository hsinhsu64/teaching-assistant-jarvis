import pymupdf

from xiaoshen.oe_deck import parse_blocks
from xiaoshen.source_text import SourceDoc
from xiaoshen.summary import Summary, SummaryPoint, SummarySlide
from xiaoshen.summary_oe import SUMMARY_LABEL, render_summary_oe_deck, summary_markdown, summary_note

REF = {"title": "UTI guideline", "first_author": "Roberts", "journal": "Pediatrics", "year": "2025", "doi": "10.1/x"}


def summ(origin="pmc", truncated_at=None, unverified=False):
    doc = SourceDoc("UTI guideline", "[1] Roberts 2025, Pediatrics", ["page one", "page two"], paged=True,
                    full_text=origin != "abstract", origin=origin)
    pts = [SummaryPoint("發燒嬰兒需驗尿", ["Urinalysis is recommended."], 1, True),
           SummaryPoint("抗生素 7–14 天", ["Treat 7 to 14 days."], 2, not unverified)]
    if unverified:
        pts[1] = SummaryPoint("抗生素 7–14 天", ["Treat 7 to 14 days."], None, False)
    return Summary(doc, [SummarySlide("診斷", pts[:1]), SummarySlide("治療", pts[1:])], truncated_at,
                   "gemini-3.1-pro-preview", "guideline")


def test_markdown_parses_into_heading_and_bullets():
    blocks = parse_blocks(summary_markdown(summ()), [])
    assert ("h", "診斷") in blocks and ("h", "治療") in blocks
    bullets = [b for b in blocks if b[0] == "ul"]
    assert any("發燒嬰兒需驗尿" in item and "第 1 頁" in item for b in bullets for item in b[1])


def test_unverified_point_marked():
    md = summary_markdown(summ(unverified=True))
    assert "抗生素 7–14 天（未能對照原文）" in md


def test_note_shows_origin_model_and_counts():
    note = summary_note(summ(origin="pdf"))
    assert "使用者提供的 PDF" in note and "gemini-3.1-pro-preview" in note and "2/2 點已逐字對照原文" in note
    assert "只讀到第 1 頁" in summary_note(summ(truncated_at=1))
    assert "只讀到摘要" in summary_note(summ(origin="abstract"))


def test_render_creates_summary_deck(tmp_path):
    path = tmp_path / "s.pdf"
    pages = render_summary_oe_deck(summ(), REF, path)
    assert pages >= 3 and path.read_bytes().startswith(b"%PDF")
    doc = pymupdf.open(path)
    cover = doc[0].get_text()
    assert "UTI guideline" in cover and "PubMed Central 全文" in cover
    assert "問題：" not in cover and "未改寫" not in cover  # 不是 OE 原文簡報的封面
    assert SUMMARY_LABEL in doc[1].get_text()  # 頁尾標示是整理過的摘要
    assert "診斷" in doc[1].get_text()

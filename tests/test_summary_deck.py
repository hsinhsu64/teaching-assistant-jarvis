"""原文重點摘要簡報（spec §6.2b）：做成 16:9 PDF 簡報。"""

from datetime import datetime

import pymupdf

from xiaoshen.source_text import SourceDoc
from xiaoshen.summary import Summary, SummaryPoint, SummarySlide
from xiaoshen.summary_deck import render_summary_pdf, summary_filename


def _summary(slides, full_text=True, truncated_at=None, paged=True):
    doc = SourceDoc("KDIGO 2025 Guideline", "[4] KDIGO", ["p1", "p2", "p3"], paged=paged, full_text=full_text,
                    note="" if full_text else "只讀到摘要")
    return Summary(doc, slides, truncated_at, "gemini-3.5-flash")


def _point(zh="類固醇為第一線治療", verified=True, page=1):
    return SummaryPoint(zh, "Glucocorticoids are the first-line treatment for children.", page if verified else None,
                        verified)


def _texts(path):
    doc = pymupdf.open(path)
    try:
        return [p.get_text() for p in doc]
    finally:
        doc.close()


def test_cover_and_one_page_per_short_slide(tmp_path):
    s = _summary([SummarySlide("初發治療", [_point()]), SummarySlide("復發", [_point(page=2)])])
    path = tmp_path / "s.pdf"
    assert render_summary_pdf(s, path) == 3
    pages = _texts(path)
    assert len(pages) == 3
    assert "Gemini Flash 整理" in pages[0] and "KDIGO 2025 Guideline" in pages[0]
    assert "初發治療" in pages[1] and "(p. 1)" in pages[1]
    doc = pymupdf.open(path)
    assert round(doc[0].rect.width) == 960 and round(doc[0].rect.height) == 540
    doc.close()


def test_long_slide_continues_with_suffix(tmp_path):
    long_zh = "這是一個很長的重點，說明類固醇劑量、療程與副作用監測，需要完整呈現給住院醫師討論。"
    s = _summary([SummarySlide("類固醇節制劑", [_point(long_zh + str(i)) for i in range(40)])])
    path = tmp_path / "s.pdf"
    n = render_summary_pdf(s, path)
    pages = _texts(path)
    assert n == len(pages) > 2
    assert "（續）" in pages[2] and "類固醇節制劑" in pages[2]
    body = "".join(pages[1:])
    assert all(f"{long_zh}{i}" in body.replace("\n", "") for i in range(40))  # 一點都不能漏


def test_unverified_label_present(tmp_path):
    ok = tmp_path / "ok.pdf"
    render_summary_pdf(_summary([SummarySlide("A", [_point()])]), ok)
    assert "未能對照原文" not in "".join(_texts(ok)[1:])
    bad = tmp_path / "bad.pdf"
    render_summary_pdf(_summary([SummarySlide("A", [_point(), _point("捏造", verified=False)])]), bad)
    pages = _texts(bad)
    assert "未能對照原文" in pages[1]
    assert "共 2 點，其中 1 點未能對照原文" in pages[0].replace("\n", "")  # 第一頁列出點數


def _cover(path):
    return _texts(path)[0].replace("\n", "")


def test_cover_says_all_points_checked(tmp_path):
    # 2026-11-05 實機：「未能對照原文：0 點」被讀成「沒辦法對照原文」→ 改成正面說法
    path = tmp_path / "s.pdf"
    render_summary_pdf(_summary([SummarySlide("A", [_point(), _point(page=2)])]), path)
    cover = _cover(path)
    assert "全部 2 點都已逐字對照原文" in cover and "未能對照原文" not in cover


def test_cover_tells_how_many_pages_and_how_to_turn(tmp_path):
    # 2026-11-05 實機：只看到封面，不知道後面還有 8 頁
    path = tmp_path / "s.pdf"
    n = render_summary_pdf(_summary([SummarySlide("A", [_point()]), SummarySlide("B", [_point(page=2)])]), path)
    assert n == 3 and "共 3 頁，說「下一頁」或按遙控器翻頁" in _cover(path)


def test_abstract_only_and_truncation_noted(tmp_path):
    a = tmp_path / "a.pdf"
    render_summary_pdf(_summary([SummarySlide("A", [_point(page=None)])], full_text=False, paged=False), a)
    assert "只讀到摘要" in _texts(a)[0]
    t = tmp_path / "t.pdf"
    render_summary_pdf(_summary([SummarySlide("A", [_point()])], truncated_at=2), t)
    assert "只讀到第 1–2 頁" in _texts(t)[0]


def test_summary_filename_sanitised():
    assert summary_filename("KDIGO: 2025/Guide?", datetime(2026, 11, 5, 13, 20)) == "2026-11-05_1320_KDIGO_ 2025_Guide_.pdf"
    long = summary_filename("x" * 100, datetime(2026, 11, 5, 13, 20))
    assert long == "2026-11-05_1320_" + "x" * 40 + ".pdf"


def test_pdf_stays_small(tmp_path):
    # 實測：沒有子集化時每頁都嵌入整套中文字型，21 頁 228 MB，投影載入與存檔都變很慢
    s = _summary([SummarySlide(f"章{i}", [_point(f"重點 {i}-{k} 類固醇劑量") for k in range(6)]) for i in range(8)])
    path = tmp_path / "s.pdf"
    render_summary_pdf(s, path)
    assert path.stat().st_size < 3_000_000
    assert "重點 7-5 類固醇劑量" in "".join(_texts(path)).replace("\n", "")


def test_every_page_has_page_number(tmp_path):
    # 使用者要求 2026-11-05：每張摘要簡報都標頁碼
    path = tmp_path / "s.pdf"
    n = render_summary_pdf(_summary([SummarySlide("A", [_point()]), SummarySlide("B", [_point(page=2)])]), path)
    pages = _texts(path)
    assert n == 3
    for i, text in enumerate(pages, 1):
        assert f"{i} / {n}" in text, i

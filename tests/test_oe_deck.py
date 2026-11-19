"""OE 簡報（spec §6.2c）：OpenEvidence 回答完整照原文做成簡報，圖表放原位，參考文獻可點。"""

import json
import re

import pymupdf

from xiaoshen.oe_client import OEResult
from xiaoshen.oe_deck import deck_links, oe_deck_filename, render_oe_deck


def _png(path):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), 0)
    pix.clear_with(200)
    pix.save(str(path))
    return str(path)


def _cit(i, title, doi="", pmid="", href=""):
    return {"index": i, "title": title, "journal": "Kidney Int", "year": "2025", "doi": doi,
            "first_author": "Floege", "authors": "Floege J", "pmid": pmid, "href": href}


RAW = """# 兒童腎病症候群最新治療指引

**KDIGO 於 2025 年發布了最新的兒童腎病症候群臨床實踐指引**，取代 2012 年的舊版建議[3][4]。

**初發腎病症候群的類固醇治療**

對於類固醇敏感型腎病症候群初次發作，建議口服類固醇治療 8 週或 12 週[3]。
- 每日口服 prednisone 60 mg/m²/天，連續 4 或 6 週
- 之後改為隔日 40 mg/m²，連續 4 或 6 週[4]

**類固醇抗藥型腎病症候群**

CNI 是目前第一線用藥[7]。"""


def _oe(raw=RAW, citations=None, others=None, figures=None, missing=0):
    return OEResult("a1", raw, citations if citations is not None else
                    [_cit(3, "KDIGO 2025 Guideline", doi="10.1016/j.kint.2024.11.007"),
                     _cit(4, "KDOQI Commentary", pmid="37341661")],
                    raw=raw, other_citations=others or [], deck_figures=figures or [], missing_figures=missing)


def _texts(path):
    doc = pymupdf.open(path)
    try:
        return [p.get_text().replace("\n", "") for p in doc]
    finally:
        doc.close()


def test_headings_start_new_slides(tmp_path):
    p = tmp_path / "oe.pdf"
    info = render_oe_deck("最新的兒童腎病症候群治療指引", _oe(), "", p)
    pages = _texts(p)
    assert info.pages == len(pages)
    steroid = next(i for i, t in enumerate(pages) if "初發腎病症候群的類固醇治療" in t)
    srns = next(i for i, t in enumerate(pages) if "類固醇抗藥型腎病症候群" in t)
    assert 0 < steroid < srns


def test_verbatim_text_present(tmp_path):
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(), "", p)
    body = "".join(_texts(p)).replace(" ", "")
    for sentence in ["取代2012年的舊版建議", "建議口服類固醇治療8週或12週", "每日口服prednisone60mg/m²/天，連續4或6週",
                     "之後改為隔日40mg/m²，連續4或6週", "CNI是目前第一線用藥"]:
        assert sentence in body, sentence
    assert "OpenEvidence 原文" in "".join(_texts(p))


def test_cover_has_question_note_and_page_hint(tmp_path):
    p = tmp_path / "oe.pdf"
    info = render_oe_deck("最新的兒童腎病症候群治療指引", _oe(), "今天的簡報中沒有提到，以下為 OpenEvidence 結果", p)
    cover = _texts(p)[0]
    assert "最新的兒童腎病症候群治療指引" in cover and "今天的簡報中沒有提到" in cover
    assert f"共 {info.pages} 頁，說「下一頁」或按遙控器翻頁" in cover


def test_figure_slide_and_missing_count(tmp_path):
    fig = {"name": "Visual Abstract", "caption": "Tacrolimus vs MMF", "path": _png(tmp_path / "va.png"), "index": 3,
           "marker": "![Visual Abstract: Tacrolimus vs MMF](https://img/va.jpg)"}
    raw = RAW.replace("**類固醇抗藥型腎病症候群**", f"{fig['marker']}\n\n**類固醇抗藥型腎病症候群**")
    p = tmp_path / "oe.pdf"
    info = render_oe_deck("q", _oe(raw=raw, figures=[fig], missing=1), "", p)
    pages = _texts(p)
    fig_page = next(i for i, t in enumerate(pages) if "Tacrolimus vs MMF" in t)
    doc = pymupdf.open(p)
    assert doc[fig_page].get_images()
    doc.close()
    assert info.figures == 1 and info.missing_figures == 1 and "1 張圖無法載入" in pages[0]
    assert "https://img/va.jpg" not in "".join(pages)


def test_table_rendered_and_long_table_continues(tmp_path):
    rows = "\n".join(f"| 列 {i} | 劑量 {i} mg |" for i in range(60))
    raw = RAW + f"\n\n<mdtable>\n| 項目 | 劑量 |\n|---|---|\n{rows}\n</mdtable>"
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(raw=raw), "", p)
    pages = _texts(p)
    body = "".join(pages)
    assert all(f"列 {i}" in body for i in range(60))  # 一列都不能漏
    table_pages = [t for t in pages if "列 " in t]
    assert len(table_pages) >= 2 and all("項目" in t for t in table_pages)  # 換頁時表頭重複


def test_long_table_continues(tmp_path):
    test_table_rendered_and_long_table_continues(tmp_path)


def test_reference_pages_and_link_map(tmp_path):
    p = tmp_path / "oe.pdf"
    info = render_oe_deck("q", _oe(others=[_cit(None, "Other paper", href="https://x/other")]), "", p)
    pages = _texts(p)
    first_body = next(i for i, t in enumerate(pages) if "[3]" in t and i > 0)
    assert any(l["ref"] == "n:3" for l in deck_links(p, first_body))
    ref_page = next(i for i, t in enumerate(pages) if "參考文獻" in t and "KDOQI Commentary" in t)
    refs = {l["ref"] for l in deck_links(p, ref_page)}
    assert {"n:3", "n:4", "o:0"} <= refs
    assert "OE 也引用了以下文獻（未標編號）" in "".join(pages)  # 2026-11-19 改名，較好懂
    assert info.refs == 3
    for l in deck_links(p, ref_page):
        assert 0 <= l["x0"] < l["x1"] <= 1 and 0 <= l["y0"] < l["y1"] <= 1
    doc = pymupdf.open(p)
    uris = {lk.get("uri") for lk in doc[ref_page].get_links()}
    doc.close()
    assert "https://doi.org/10.1016/j.kint.2024.11.007" in uris and "https://pubmed.ncbi.nlm.nih.gov/37341661/" in uris


def test_unmapped_marker_has_no_link(tmp_path):
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(), "", p)
    pages = _texts(p)
    idx = next(i for i, t in enumerate(pages) if "[7]" in t)
    assert all(l["ref"] != "n:7" for l in deck_links(p, idx))


def test_oe_deck_without_mapped_citations(tmp_path):
    p = tmp_path / "oe.pdf"
    info = render_oe_deck("q", _oe(citations=[]), "", p)
    assert info.refs == 0 and not any("參考文獻" in t for t in _texts(p)[1:])
    p2 = tmp_path / "oe2.pdf"
    info2 = render_oe_deck("q", _oe(citations=[], others=[_cit(None, "Only other", href="https://x")]), "", p2)
    assert info2.refs == 1 and any("OE 也引用了以下文獻" in t for t in _texts(p2))


def test_followups_not_in_deck(tmp_path):
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(), "", p)
    assert "follow-up" not in "".join(_texts(p))


def test_every_page_numbered(tmp_path):
    p = tmp_path / "oe.pdf"
    info = render_oe_deck("q", _oe(), "", p)
    for i, t in enumerate(_texts(p), 1):
        assert f"{i} / {info.pages}" in t


def test_pdf_small(tmp_path):
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(), "", p)
    assert p.stat().st_size < 3_000_000


def test_sidecar_written_next_to_pdf(tmp_path):
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(), "", p)
    side = json.loads((tmp_path / "oe.links.json").read_text(encoding="utf-8"))
    assert set(side) == {"pages", "refs"} and all(re.fullmatch(r"\d+", k) for k in side["pages"])
    assert deck_links(tmp_path / "missing.pdf", 0) == []


def test_oe_deck_filename():
    from datetime import datetime

    assert oe_deck_filename("劑量?/成人", datetime(2026, 11, 5, 19, 5), "ab12") == "2026-11-05_190500_ab12_劑量__成人.pdf"


# ---- 最終審查修正 ----

def test_long_list_is_split_not_dropped(tmp_path):
    # C1：30 個長項目曾整段消失（insert_htmlbox 放不下就什麼都不寫）
    items = [f"第 {i} 項：" + "類固醇治療期間需監測血壓、血糖與生長，並注意感染風險與骨質變化。" * 3 for i in range(30)]
    raw = "**清單**\n\n" + "\n".join(f"- {x}" for x in items)
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(raw=raw, citations=[]), "", p)
    body = "".join(_texts(p)).replace(" ", "")
    assert all(f"第{i}項" in body for i in range(30))


def test_long_paragraph_is_split_not_blank(tmp_path):
    sentences = [f"這是第 {i} 句，說明某一項治療建議的細節。" for i in range(120)]
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(raw="**段落**\n\n" + "".join(sentences), citations=[]), "", p)
    body = "".join(_texts(p)).replace(" ", "")
    assert all(f"第{i}句" in body for i in range(120))


def test_long_bold_line_is_text_not_title(tmp_path):
    line = "**" + "這是一整行粗體的長句子，內容是重要的臨床建議，不能被當成標題而被縮小或截掉。" * 4 + "**"
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(raw="# 標題\n\n" + line, citations=[]), "", p)
    assert "不能被當成標題而被縮小或截掉" in "".join(_texts(p)).replace(" ", "")


def test_unplaced_figure_still_shown(tmp_path):
    # I6：figures.json 有、但原文沒有對應位置的圖，不能默默消失
    fig = {"name": "Figure 9", "caption": "放在最後", "path": _png(tmp_path / "f9.png"), "index": None, "marker": ""}
    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(figures=[fig]), "", p)
    assert any("Figure 9" in t for t in _texts(p))


def test_sidecar_keeps_citation_details(tmp_path):
    # C2：點擊時用投影上那份簡報自己的文獻，不用「目前的回答」
    from xiaoshen.oe_deck import deck_ref

    p = tmp_path / "oe.pdf"
    render_oe_deck("q", _oe(others=[_cit(None, "Other", href="https://x/o")]), "", p)
    assert deck_ref(p, "n:3")["title"] == "KDIGO 2025 Guideline"
    assert deck_ref(p, "o:0")["href"] == "https://x/o"
    assert deck_ref(p, "n:99") is None


def test_oe_deck_filename_unique():
    from datetime import datetime

    a = oe_deck_filename("q", datetime(2026, 11, 5, 19, 5, 7), "id1")
    b = oe_deck_filename("q", datetime(2026, 11, 5, 19, 5, 7), "id2")
    assert a != b and a.startswith("2026-11-05_190507_")



def test_sentences_keep_bold_balanced():
    from xiaoshen.oe_deck import _sentences

    parts = _sentences("前言。**重點一。重點二。**結尾。")
    assert all(p.count("**") % 2 == 0 for p in parts) and "".join(parts).replace("**", "") == "前言。重點一。重點二。結尾。"

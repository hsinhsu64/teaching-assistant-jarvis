"""原文重點摘要簡報（spec §6.2b）：把 Summary 排成 16:9 PDF。

張數與字數不限：一頁放不下就接到下一頁（標題加「（續）」），不縮小字級。
對不上原文的點以橘色標「未能對照原文」。
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

import pymupdf

from . import persona
from .deck import PDF_LOCK
from .summary import Summary, SummaryPoint

WIDTH, HEIGHT = 960, 540
NAVY = (0.09, 0.16, 0.29)
ORANGE = "#d9822b"
GREEN = "#2e7d32"
UNVERIFIED = "未能對照原文"
CONTINUED = "（續）"
TITLE_BAR = pymupdf.Rect(0, 0, WIDTH, 72)
BODY = pymupdf.Rect(44, 92, WIDTH - 44, HEIGHT - 40)
FOOTER = pymupdf.Rect(44, HEIGHT - 32, WIDTH - 44, HEIGHT - 8)
_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')
_e = html.escape


def summary_filename(title: str, now: datetime) -> str:
    safe = _UNSAFE.sub("_", title.strip())[:40]
    return f"{now:%Y-%m-%d_%H%M}_{safe}.pdf"


def _point_html(p: SummaryPoint) -> str:
    where = f" (p. {p.page})" if p.page else ""
    flag = "" if p.verified else f'<span style="color:{ORANGE}; font-weight:bold">［{UNVERIFIED}］</span> '
    color = "#222" if p.verified else ORANGE
    return (f'<li style="margin-bottom:10px"><div style="font-size:20px; color:{color}">{flag}{_e(p.zh)}</div>'
            f'<div style="font-size:14px; color:#666">{_e(p.quote)}{where}</div></li>')


def _body_html(points: list[SummaryPoint]) -> str:
    return ('<div style="font-family: sans-serif; line-height:1.35"><ul style="margin:0">'
            + "".join(_point_html(p) for p in points) + "</ul></div>")


def _fits(points: list[SummaryPoint]) -> bool:
    scratch = pymupdf.open()
    try:
        page = scratch.new_page(width=WIDTH, height=HEIGHT)
        spare, _ = page.insert_htmlbox(BODY, _body_html(points), scale_low=1)
        return spare >= 0
    finally:
        scratch.close()


def _paginate(points: list[SummaryPoint]) -> list[list[SummaryPoint]]:
    pages: list[list[SummaryPoint]] = [[]]
    for p in points:
        if pages[-1] and not _fits(pages[-1] + [p]):
            pages.append([])
        pages[-1].append(p)
    return pages


def _frame(page: pymupdf.Page, title: str, footer: str, number: int, total: int) -> None:
    page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
    page.draw_rect(TITLE_BAR, color=None, fill=NAVY)
    spare, _ = page.insert_htmlbox(TITLE_BAR + (36, 14, -36, -8),
                                   f'<div style="font-family: sans-serif; font-size:26px; font-weight:bold; color:#fff">'
                                   f"{_e(title)}</div>", scale_low=0.6)
    if spare < 0:  # 放不下時 PyMuPDF 什麼都不寫：寧可失敗也不默默少字（審查 C1）
        raise ValueError(f"標題放不下：{title[:30]}")
    # 頁碼（每張都標）；放左下角：投影右側的回答抽屜會蓋住右半邊
    page.insert_htmlbox(pymupdf.Rect(FOOTER.x0, FOOTER.y0 - 4, FOOTER.x0 + 80, FOOTER.y1),
                        f'<div style="font-family: sans-serif; font-size:15px; color:#555">{number} / {total}</div>')
    page.insert_htmlbox(FOOTER + (88, 0, 0, 0),
                        f'<div style="font-family: sans-serif; font-size:11px; color:#888">{_e(footer)}</div>')


def _cover_html(s: Summary, total_pages: int) -> str:
    d = s.doc
    total = sum(len(sl.points) for sl in s.slides)
    lines = [f'<p style="font-size:26px; font-weight:bold; margin:0 0 8px">{_e(d.title)}</p>',
             f'<p style="font-size:16px; color:#555; margin:0 0 18px">{_e(d.citation)}</p>',
             '<p style="font-size:18px; margin:4px 0">Gemini Flash 整理</p>',
             f'<p style="font-size:18px; margin:4px 0">讀取範圍：{"全文" if d.full_text else "只讀到摘要"}</p>']
    if s.truncated_at:
        lines.append(f'<p style="font-size:18px; margin:4px 0; color:{ORANGE}">原文太長，只讀到第 1–{s.truncated_at} 頁</p>')
    # 2026-11-05 實機：「未能對照原文：0 點」被讀成「沒辦法對照原文」→ 全部對得上時用正面說法
    if s.unverified:
        lines.append(f'<p style="font-size:18px; margin:4px 0">共 {total} 點，其中 '
                     f'<span style="color:{ORANGE}; font-weight:bold">{s.unverified} 點{UNVERIFIED}</span>（橘色標示）</p>')
    else:
        lines.append(f'<p style="font-size:18px; margin:4px 0; color:{GREEN}">全部 {total} 點都已逐字對照原文</p>')
    # 實機：只看到封面，不知道後面還有內容
    lines.append(f'<p style="font-size:18px; margin:14px 0 4px; font-weight:bold">'
                 f'共 {total_pages} 頁，說「下一頁」或按遙控器翻頁</p>')
    lines.append(f'<p style="font-size:13px; color:#888; margin:18px 0 0">{datetime.now():%Y-%m-%d %H:%M}</p>')
    return '<div style="font-family: sans-serif; color:#222">' + "".join(lines) + "</div>"


def render_summary_pdf(summary: Summary, path: Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    footer = f"{persona.current().display_name} · 原文重點摘要 · {summary.doc.title}"
    with PDF_LOCK:
        doc = pymupdf.open()
        try:
            # 先分好頁才知道總頁數（封面要寫「共 N 頁」）
            pages = [(sl.title, k, chunk) for sl in summary.slides for k, chunk in enumerate(_paginate(sl.points))]
            total = 1 + len(pages)
            cover = doc.new_page(width=WIDTH, height=HEIGHT)
            _frame(cover, "原文重點摘要", footer, 1, total)
            cover.insert_htmlbox(BODY, _cover_html(summary, total), scale_low=0.6)
            for n, (title, k, chunk) in enumerate(pages, 2):
                page = doc.new_page(width=WIDTH, height=HEIGHT)
                _frame(page, title + (CONTINUED if k else ""), footer, n, total)
                # 單一點就超過一整頁時才允許縮小（否則會被截掉）
                page.insert_htmlbox(BODY, _body_html(chunk), scale_low=1 if _fits(chunk) else 0.5)
            # 每次排版都會嵌入整套中文字型（實測 21 頁 228 MB）：存檔前子集化、去重、壓縮
            doc.subset_fonts()
            doc.save(str(path), garbage=4, deflate=True)
            return doc.page_count
        finally:
            doc.close()

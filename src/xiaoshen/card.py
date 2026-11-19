"""把一個已驗證的回答整理成一張字卡（投影＋存 PNG，可貼進投影片）。

重點用英文原文——也就是已通過出處驗證的引文本身，後面標 [n]；下方列參考文獻。
排版用 PyMuPDF 的 HTML 排版直接畫成圖，投影與存檔是同一張。
"""

from __future__ import annotations

import html
from pathlib import Path

import pymupdf

from .deck import PDF_LOCK
from . import persona
from .answering import Answer, derived_label, display_quote

WIDTH, HEIGHT = 1280, 720
ACCENT = "#e8743b"
ROUTE_TITLE = {"openevidence": "OpenEvidence 查證", "pubmed": "PubMed 查證", "model": "Gemini 一般知識（未經文獻驗證）",
               "references": "參考資料", "deck": "今天的簡報"}


def _authors(s: str) -> str:
    names = [n.strip() for n in (s or "").split(",") if n.strip()]
    return ", ".join(names[:3]) + (", et al." if len(names) > 3 else "")


def _refs_and_markers(a: Answer) -> tuple[list[tuple[int, str, str]], list[list[int]]]:
    """回傳（參考文獻 [(編號, 第一行, 第二行)], 每張卡的編號清單）。
    OpenEvidence：沿用文獻編號；簡報／參考資料：依出現順序把「XX 第 N 頁」編成 1, 2, 3…"""
    refs: list[tuple[int, str, str]] = []
    markers: list[list[int]] = []
    if a.references:
        for c in a.cards:
            markers.append([r for r in c.refs if r in a.references])
        for n in sorted({r for m in markers for r in m}):
            ref = a.references[n]
            detail = ". ".join(x for x in (ref.get("journal"), ref.get("year")) if x)
            second = " ".join(x for x in (detail + "." if detail else "", _authors(ref.get("authors", ""))) if x)
            refs.append((n, ref.get("title") or "", second))
        return refs, markers
    numbering: dict[str, int] = {}
    for c in a.cards:
        if c.source not in numbering:
            numbering[c.source] = len(numbering) + 1
            refs.append((numbering[c.source], c.source, ""))
        markers.append([numbering[c.source]])
    return refs, markers


def card_html(a: Answer) -> str:
    e = html.escape
    refs, markers = _refs_and_markers(a)
    title = a.interpreted or a.question
    derived = "".join(
        f'<li style="margin-bottom:10px"><b>{derived_label()}</b>{e(d.text)} <span style="color:{ACCENT}">'
        + "".join(f"[{n}]" for n in sorted({n for u in d.uses for n in markers[u]})) + "</span></li>"
        for d in a.derivations)  # 「為什麼沒推導」的說明只給主持人畫面
    points = "".join(
        f'<li style="margin-bottom:10px">{e(display_quote(c.quote))} '
        f'<span style="color:{ACCENT}">{"".join(f"[{n}]" for n in m)}</span></li>'
        for c, m in zip(a.cards, markers))
    ref_html = "".join(
        f'<p style="margin:6px 0"><b>{n}.</b> {e(first)}'
        + (f'<br/><span style="color:#777">{e(second)}</span>' if second else "") + "</p>"
        for n, first, second in refs)
    return f"""
<div style="font-family: sans-serif; color:#222">
<table style="width:100%; font-size:17px"><tr>
  <td style="color:#444">{html.escape(persona.current().display_name)} · 字卡</td>
  <td style="text-align:right; color:#444">{e(ROUTE_TITLE.get(a.route, ""))}</td></tr></table>
<div style="border-top:3px solid {ACCENT}; margin:6px 0 14px"></div>
<p style="font-size:28px; font-weight:bold; margin:0 0 14px">{e(title)}</p>
<ul style="font-size:22px; line-height:1.45; margin:0 0 16px">{points}{derived}</ul>
<div style="background:#f5f3ef; padding:10px 14px; font-size:14px">
  <p style="margin:0 0 4px; font-weight:bold; color:#555">REFERENCES</p>{ref_html}</div>
</div>"""


def render_card(a: Answer, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with PDF_LOCK:
        _render(a, path)
    return path


def _render(a: Answer, path: Path) -> None:
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=WIDTH, height=HEIGHT)
        page.draw_rect(page.rect, color=None, fill=(0.96, 0.95, 0.93))
        box = pymupdf.Rect(50, 36, WIDTH - 50, HEIGHT - 36)
        page.draw_rect(box, color=None, fill=(1, 1, 1))
        page.insert_htmlbox(box + (30, 24, -30, -20), card_html(a), scale_low=0.4)  # 太長就縮小字級
        page.get_pixmap(dpi=144).save(str(path))
    finally:
        doc.close()

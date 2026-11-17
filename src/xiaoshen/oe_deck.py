"""OE 簡報（spec §6.2c）：OpenEvidence 的回答完整照原文做成 16:9 簡報。

不改寫、不翻譯、不濃縮；每頁標「OpenEvidence 原文」。依小標題分頁，圖表放在原文中的位置，
最後幾頁是參考文獻。正文的 [n] 只有能可靠對應到文獻時才加連結（寧可沒有出處也不標錯）；
對應不上的文獻列在「其他引用文獻（無法確認對應編號）」，可點但不標編號。

連結兩份：PDF 內的連結（DOI → PubMed → OpenEvidence 給的連結，不需上網；會後發給住院醫師也能點），
與旁邊的 <pdf>.links.json（投影幕點擊區：哪一頁、哪個框、哪一篇），由伺服器開啟時再查 PMC 全文。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pymupdf

from . import persona
from .deck import PDF_LOCK
from .oe_client import OEResult, _cells
from .summary_deck import _UNSAFE, BODY, CONTINUED, HEIGHT, WIDTH, _frame

ORIGINAL = "OpenEvidence 原文"
OTHER_REFS = "其他引用文獻（無法確認對應編號）"
GAP = 8                       # 區塊之間的距離（pt）
FIG_CAPTION_H = 46            # 圖下方說明的高度
_IMAGE_LINE = re.compile(r"^\s*!\[[^\]]*]\(([^)\s]+)[^)]*\)\s*$")
_VISUAL_LINE = re.compile(r"^\s*<visual>\s*([^<\[]+?)\s*(?:\[\d+\])?\s*</visual>\s*$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
_BOLD_LINE = re.compile(r"^\s*\*\*([^*]+)\*\*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_MARK = re.compile(r"\[(\d+)\]")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+")
MAX_HEADING = 60  # 比這長的「標題」其實是內容：當段落排，不縮成標題（審查 C1）


class DeckLayoutError(Exception):
    """內容放不下（PyMuPDF 會默默不寫）：整份簡報算失敗，不交出少字的簡報。"""


@dataclass
class OEDeckInfo:
    pages: int
    figures: int
    missing_figures: int
    refs: int


def oe_deck_filename(question: str, now: datetime, uid: str = "") -> str:
    """含秒與回答 id：同一分鐘同一題問兩次也不會覆蓋正在投影的那份（審查 I1）。"""
    safe = _UNSAFE.sub("_", question.strip())[:40]
    return f"{now:%Y-%m-%d_%H%M%S}_{uid + '_' if uid else ''}{safe}.pdf"


def offline_url(ref: dict) -> str:
    """PDF 內的連結：DOI → PubMed → OpenEvidence 給的連結（不上網查 PMC）。"""
    if ref.get("doi"):
        return f"https://doi.org/{ref['doi']}"
    if ref.get("pmid"):
        return f"https://pubmed.ncbi.nlm.nih.gov/{ref['pmid']}/"
    href = str(ref.get("href") or "")
    return href if href.lower().startswith(("http://", "https://")) else ""


def reference_text(ref: dict) -> str:
    who = ref.get("first_author") or ""
    tail = " ".join(x for x in (ref.get("journal"), ref.get("year")) if x)
    parts = [(ref.get("title") or "").rstrip("."), f"{who} et al" if who else "", tail,
             f"doi:{ref['doi']}" if ref.get("doi") else ""]
    return ". ".join(p for p in parts if p) + "."


# ---- 解析 OpenEvidence 的 markdown ----

def _inline(text: str) -> str:
    """行內格式：**粗體**、[n] 引用編號、[文字](網址) 只留文字；其他標籤拿掉。"""
    text = _MD_LINK.sub(lambda m: m.group(1), _TAG.sub("", text))
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return _MARK.sub(r'<span style="color:#2f5fa7">[\1]</span>', out)


def _plain(text: str) -> str:
    return re.sub(r"\*\*", "", _MD_LINK.sub(lambda m: m.group(1), _TAG.sub("", text))).strip()


def parse_blocks(raw: str, figures: list[dict]) -> list[tuple]:
    """回傳區塊：("h", 標題) ("p", html) ("ul", [html…]) ("table", 表頭, 列) ("fig", 圖)。"""
    by_url = {}
    for f in figures:
        m = _IMAGE_LINE.match(f.get("marker") or "")
        if m:
            by_url[m.group(1)] = f
    by_name = {f["name"]: f for f in figures}
    blocks: list[tuple] = []
    para: list[str] = []
    bullets: list[str] = []
    used: set[str] = set()

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()
        if bullets:
            blocks.append(("ul", list(bullets)))
            bullets.clear()

    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("<mdtable>"):
            flush()
            j = i + 1
            body: list[str] = []
            while j < len(lines) and "</mdtable>" not in lines[j]:
                body.append(lines[j])
                j += 1
            rows = [l for l in body if l.strip().startswith("|")]
            if len(rows) >= 3 and re.fullmatch(r"[\s|:\-]+", rows[1]):
                header = _cells(rows[0])
                data = [_cells(r) for r in rows[2:]]
                width = max([len(header)] + [len(r) for r in data])
                header += [""] * (width - len(header))
                blocks.append(("table", header, [r + [""] * (width - len(r)) for r in data]))
            elif body:
                para.append(" ".join(l.strip() for l in body if l.strip()))
                flush()
            i = j + 1
            continue
        if (m := _IMAGE_LINE.match(line)) or (m := _VISUAL_LINE.match(line)):
            flush()
            fig = by_url.get(m.group(1)) or by_name.get(m.group(1).strip())
            if fig is not None and fig["name"] not in used:
                used.add(fig["name"])
                blocks.append(("fig", fig))
        elif ((m := _HEADING.match(line)) or (m := _BOLD_LINE.match(line))) and len(_plain(m.group(1))) <= MAX_HEADING:
            flush()
            blocks.append(("h", _plain(m.group(1))))
        elif m := _BULLET.match(line):
            if para:
                flush()
            bullets.append(m.group(1).strip())
        elif not line.strip():
            flush()
        else:
            if bullets:
                flush()
            para.append(line.strip())
        i += 1
    flush()
    # 原文中找不到位置的圖照樣顯示（放在最後，審查 I6）
    blocks += [("fig", f) for f in figures if f["name"] not in used]
    return blocks


# ---- 排版 ----

_STYLE = 'font-family: sans-serif; color:#222; line-height:1.4'


def _block_html(block: tuple, rows: list[list[str]] | None = None) -> str:
    kind = block[0]
    if kind == "p":
        return f'<div style="{_STYLE}; font-size:19px">{_inline(block[1])}</div>'
    if kind == "ul":
        items = "".join(f'<li style="margin-bottom:4px">{_inline(b)}</li>' for b in block[1])
        return f'<div style="{_STYLE}; font-size:19px"><ul style="margin:0">{items}</ul></div>'
    if kind == "table":
        cell = 'style="border:1px solid #bbb; padding:4px 6px; font-size:15px"'
        head = "".join(f"<th {cell} bgcolor='#e8edf3'>{_inline(h)}</th>" for h in block[1])
        body = "".join("<tr>" + "".join(f"<td {cell}>{_inline(c)}</td>" for c in r) + "</tr>" for r in (rows or block[2]))
        return (f'<div style="{_STYLE}"><table style="border-collapse:collapse; width:100%">'
                f"<tr>{head}</tr>{body}</table></div>")
    raise ValueError(kind)


class _Measure:
    """在暫存頁上量 HTML 區塊的高度（和正式排版同寬、同字型）。"""

    def __init__(self) -> None:
        self.doc = pymupdf.open()

    def height(self, html_text: str, width: float = BODY.width) -> float:
        page = self.doc.new_page(width=WIDTH, height=HEIGHT * 4)
        rect = pymupdf.Rect(0, 0, width, HEIGHT * 4)
        spare, _ = page.insert_htmlbox(rect, html_text, scale_low=1)
        self.doc.delete_page(-1)
        return HEIGHT * 4 if spare < 0 else rect.height - spare

    def close(self) -> None:
        self.doc.close()


def _sentences(text: str) -> list[str]:
    """依句號切開；粗體 ** 跨句時補齊，避免剩下不成對的 **。"""
    parts = [s for s in _SENTENCE_END.split(text) if s and s.strip()]
    out, open_bold = [], False
    for s in parts:
        still_open = open_bold ^ (s.count("**") % 2 == 1)
        out.append(("**" if open_bold else "") + s + ("**" if still_open else ""))
        open_bold = still_open
    return out or [text]


def _split_tall(block: tuple, m: _Measure) -> list[tuple]:
    """一個區塊比一整頁高：清單一項一塊、段落依句子分段（不縮小字級、不丟字，審查 C1）。"""
    kind = block[0]
    if kind == "ul":
        return [("ul", [item]) for item in block[1]]
    if kind != "p" or m.height(_block_html(block)) <= BODY.height:
        return [block]
    chunks: list[str] = []
    for s in _sentences(block[1]):
        trial = (chunks[-1] + s) if chunks else s
        if chunks and m.height(_block_html(("p", trial))) <= BODY.height:
            chunks[-1] = trial
        else:
            chunks.append(s)
    return [("p", c) for c in chunks]


def _layout_body(blocks: list[tuple], m: _Measure, title: str) -> list[tuple]:
    """把區塊依高度排進頁面。回傳頁：("blocks", 標題, [(html, 高度)…]) 或 ("fig", 標題, 圖)。"""
    pages: list[tuple] = []
    current: list[tuple[str, float]] = []
    used = 0.0
    section = title
    cont = False

    def new_page() -> None:
        nonlocal current, used, cont
        if current:
            pages.append(("blocks", section + (CONTINUED if cont else ""), current))
            cont = True
        current, used = [], 0.0

    def add(html_text: str, h: float) -> None:
        nonlocal used
        if current and used + h > BODY.height:
            new_page()
        current.append((html_text, min(h, BODY.height)))
        used += h + GAP

    for block in blocks:
        kind = block[0]
        if kind == "h":
            new_page()
            section, cont = block[1], False
        elif kind == "fig":
            new_page()
            pages.append(("fig", section, block[1]))
            cont = True
        elif kind == "table":
            rows = list(block[2])
            while rows:
                take = len(rows)
                while take > 1 and m.height(_block_html(block, rows[:take])) > BODY.height - (used if current else 0):
                    take -= 1
                    if take == 1 and current:  # 這頁剩下的空間連一列都放不下：換頁再排
                        new_page()
                        take = len(rows)
                chunk = rows[:take]
                html_text = _block_html(block, chunk)
                add(html_text, m.height(html_text))
                rows = rows[take:]
                if rows:
                    new_page()
        else:
            whole = _block_html(block)
            h = m.height(whole)
            pieces = _split_tall(block, m) if (h > BODY.height or block[0] == "ul" and h > BODY.height / 2) else [block]
            for piece in pieces:
                html_text = _block_html(piece)
                add(html_text, m.height(html_text))
    new_page()
    return pages


def _layout_refs(entries: list[tuple[str, str, str]], m: _Measure) -> list[tuple]:
    """參考文獻頁：entries = [(ref 鍵, html, 網址)]；每頁 ("refs", 標題, [(ref, html, url, 高度)…])。"""
    pages: list[tuple] = []
    current: list[tuple] = []
    used = 0.0
    for ref, html_text, url in entries:
        h = m.height(html_text)
        if current and used + h > BODY.height:
            pages.append(("refs", "參考文獻" + (CONTINUED if pages else ""), current))
            current, used = [], 0.0
        current.append((ref, html_text, url, h))
        used += h + GAP
    if current:
        pages.append(("refs", "參考文獻" + (CONTINUED if pages else ""), current))
    return pages


def _cover_html(question: str, title: str, note: str, total: int, missing: int) -> str:
    lines = [f'<p style="font-size:26px; font-weight:bold; margin:0 0 10px">{html.escape(title)}</p>',
             f'<p style="font-size:18px; margin:4px 0">問題：{html.escape(question)}</p>',
             f'<p style="font-size:18px; margin:4px 0">{ORIGINAL}（完整照原文，{html.escape(persona.current().display_name)}未改寫）</p>']
    if note:
        lines.append(f'<p style="font-size:18px; margin:4px 0; color:#8a5a00">{html.escape(note)}</p>')
    if missing:
        lines.append(f'<p style="font-size:16px; margin:4px 0; color:#8a5a00">{missing} 張圖無法載入</p>')
    lines.append(f'<p style="font-size:18px; margin:14px 0 4px; font-weight:bold">共 {total} 頁，說「下一頁」或按遙控器翻頁</p>')
    lines.append(f'<p style="font-size:13px; color:#888; margin:18px 0 0">{datetime.now():%Y-%m-%d %H:%M}</p>')
    return '<div style="font-family: sans-serif; color:#222">' + "".join(lines) + "</div>"


def _put(page: pymupdf.Page, rect: pymupdf.Rect, html_text: str, scale_low: float = 1) -> None:
    spare, _ = page.insert_htmlbox(rect, html_text, scale_low=scale_low)
    if spare < 0:  # PyMuPDF 放不下就什麼都不寫：寧可整份失敗也不默默少字（審查 C1）
        raise DeckLayoutError("OpenEvidence 原文有一段放不進投影片")


def _norm(rect: pymupdf.Rect) -> dict:
    return {"x0": rect.x0 / WIDTH, "y0": rect.y0 / HEIGHT, "x1": rect.x1 / WIDTH, "y1": rect.y1 / HEIGHT}


def render_oe_deck(question: str, oe: OEResult, note: str, path: Path) -> OEDeckInfo:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = oe.raw or oe.answer
    blocks = parse_blocks(raw, oe.deck_figures)
    title = question
    if blocks and blocks[0][0] == "h":  # 第一個標題當封面標題
        title = blocks.pop(0)[1]
    mapped = {c["index"]: c for c in oe.citations if c.get("index") is not None}
    entries = [(f"n:{i}", f'<div style="{_STYLE}; font-size:15px"><b>[{i}]</b> {html.escape(reference_text(c))}</div>',
                offline_url(c)) for i, c in sorted(mapped.items())]
    # 點擊時用「這份簡報」自己的文獻（不是目前的回答），點了不會開到別份的同編號文獻（審查 C2）
    ref_details = {f"n:{i}": c for i, c in mapped.items()}
    ref_details.update({f"o:{k}": c for k, c in enumerate(oe.other_citations)})
    if oe.other_citations:
        entries.append(("", f'<div style="{_STYLE}; font-size:16px; font-weight:bold; margin-top:6px">{OTHER_REFS}</div>', ""))
        entries += [(f"o:{k}", f'<div style="{_STYLE}; font-size:15px">• {html.escape(reference_text(c))}</div>',
                     offline_url(c)) for k, c in enumerate(oe.other_citations)]
    footer = f"{persona.current().display_name} · {ORIGINAL} · {question}"
    sidecar: dict[str, list[dict]] = {}
    shown_figs = 0
    with PDF_LOCK:
        m = _Measure()
        doc = pymupdf.open()
        try:
            body_pages = _layout_body(blocks, m, title)
            ref_pages = _layout_refs(entries, m)
            total = 1 + len(body_pages) + len(ref_pages)
            cover = doc.new_page(width=WIDTH, height=HEIGHT)
            _frame(cover, title, footer, 1, total)
            cover.insert_htmlbox(BODY, _cover_html(question, title, note, total, oe.missing_figures), scale_low=0.6)
            for n, spec in enumerate(body_pages + ref_pages, 2):
                page = doc.new_page(width=WIDTH, height=HEIGHT)
                _frame(page, spec[1], footer, n, total)
                links: list[dict] = []
                if spec[0] == "fig":
                    fig = spec[2]
                    img = pymupdf.Rect(BODY.x0, BODY.y0, BODY.x1, BODY.y1 - FIG_CAPTION_H)
                    try:
                        page.insert_image(img, filename=fig["path"], keep_proportion=True)
                        shown_figs += 1
                    except Exception:
                        pass
                    cap = html.escape(f"{fig['name']}：{fig['caption']}" if fig.get("caption") else fig["name"])
                    mark = f' <span style="color:#2f5fa7">[{fig["index"]}]</span>' if fig.get("index") in mapped else ""
                    page.insert_htmlbox(pymupdf.Rect(BODY.x0, BODY.y1 - FIG_CAPTION_H + 4, BODY.x1, BODY.y1),
                                        f'<div style="{_STYLE}; font-size:14px; text-align:center">{cap}{mark}</div>',
                                        scale_low=0.5)
                elif spec[0] == "blocks":
                    y = BODY.y0
                    for html_text, h in spec[2]:
                        rect = pymupdf.Rect(BODY.x0, y, BODY.x1, min(BODY.y1, y + h + 2))
                        # 只有切不開的單一句／單一列比一整頁還高時才縮小；縮了還放不下就失敗
                        _put(page, rect, html_text, 1 if h < BODY.height else 0.5)
                        y += h + GAP
                if spec[0] in ("blocks", "fig"):  # 正文的 [n]：能可靠對應才加連結
                    for i, c in mapped.items():
                        for r in page.search_for(f"[{i}]"):
                            url = offline_url(c)
                            if url:
                                page.insert_link({"kind": pymupdf.LINK_URI, "from": r, "uri": url})
                            links.append({**_norm(r), "ref": f"n:{i}"})
                else:  # 參考文獻頁：每一篇一個框
                    y = BODY.y0
                    for ref, html_text, url, h in spec[2]:
                        rect = pymupdf.Rect(BODY.x0, y, BODY.x1, min(BODY.y1, y + h + 2))
                        _put(page, rect, html_text, 1 if h < BODY.height else 0.5)
                        if ref:
                            if url:
                                page.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": url})
                            links.append({**_norm(rect), "ref": ref})
                        y += h + GAP
                if links:
                    sidecar[str(n - 1)] = links
            doc.subset_fonts()
            doc.save(str(path), garbage=4, deflate=True)
            pages = doc.page_count
        finally:
            doc.close()
            m.close()
    _sidecar_path(path).write_text(json.dumps({"pages": sidecar, "refs": ref_details}, ensure_ascii=False),
                                   encoding="utf-8")
    return OEDeckInfo(pages, shown_figs, oe.missing_figures, len(mapped) + len(oe.other_citations))


def _sidecar_path(path: Path) -> Path:
    return Path(path).with_suffix(".links.json")


def _sidecar(path: Path) -> dict:
    try:
        data = json.loads(_sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def deck_links(path: Path, index: int) -> list[dict]:
    """投影幕第 index 頁（0 起）可點的框：[{x0, y0, x1, y1（0–1）, ref}]。"""
    data = _sidecar(path)
    return list((data.get("pages", data) or {}).get(str(index)) or [])


def deck_ref(path: Path, ref: str) -> dict | None:
    """這份簡報裡 ref 對應的文獻（n:<編號>／o:<序號>）。"""
    c = (_sidecar(path).get("refs") or {}).get(ref)
    return c if isinstance(c, dict) else None

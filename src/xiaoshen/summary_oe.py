"""原文重點摘要用 OE 簡報的版面呈現（封面、分節內容頁、文獻頁）。"""

from __future__ import annotations

from pathlib import Path

from .fulltext import ORIGIN_LABEL
from .oe_client import OEResult
from .oe_deck import render_oe_deck
from .summary import Summary

SUMMARY_LABEL = "原文重點摘要"
UNVERIFIED = "（未能對照原文）"
DOC_TYPE_LABEL = {"study": "原始研究", "guideline": "臨床指引", "review": "綜論"}


def summary_markdown(s: Summary) -> str:
    lines: list[str] = []
    for slide in s.slides:
        lines += [f"## {slide.title}", ""]
        for p in slide.points:
            where = f"（第 {p.page} 頁）" if p.page else ""
            lines.append(f"- {p.zh}{where if p.verified else UNVERIFIED}")
        lines.append("")
    return "\n".join(lines)


def summary_note(s: Summary) -> str:
    origin = s.doc.origin or ("abstract" if not s.doc.full_text else "")
    total = sum(len(sl.points) for sl in s.slides)
    parts = [f"資料來源：{ORIGIN_LABEL.get(origin, '原文')}", f"整理：{s.model}",
             f"{total - s.unverified}/{total} 點已逐字對照原文"]
    if s.truncated_at:
        parts.append(f"只讀到第 {s.truncated_at} 頁")
    return "｜".join(parts)


def cover_lines(s: Summary) -> list[str]:
    kind = DOC_TYPE_LABEL.get(s.doc_type, "")
    return [x for x in (s.doc.citation, f"文件類型：{kind}" if kind else "", summary_note(s), s.doc.note) if x]


def render_summary_oe_deck(s: Summary, ref: dict | None, path: Path) -> int:
    citations = [{**ref, "index": 1}] if ref else []
    oe = OEResult(article_id="", answer="", citations=citations, raw=summary_markdown(s))
    info = render_oe_deck(s.doc.title, oe, "", path, label=SUMMARY_LABEL, title=s.doc.title,
                          cover_lines=cover_lines(s))
    return info.pages

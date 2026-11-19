"""OpenEvidence：以 subprocess 呼叫使用者既有的 oe.py。

被 CAPTCHA／401／403 擋時拋 OEBlocked，由使用者在瀏覽器通過驗證後重新匯出 cookies。
本程式不嘗試繞過驗證。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BLOCKED_MSG = "OpenEvidence 需要重新驗證（請在瀏覽器通過驗證後重新匯出 cookies）"

_VISUAL_RE = re.compile(r"<visual>.*?</visual>", re.S)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_MD_IMAGE_URL_RE = re.compile(r"!\[[^\]]*]\(([^)\s]+)[^)]*\)")
_FOLLOWUP_RE = re.compile(r"<follow-up-(question|topic)\b[^>]*>.*?</follow-up-\1>", re.S)
_VISUAL_ANY_RE = re.compile(r"<visual>\s*([^<\[]+?)\s*(?:\[(\d+)\])?\s*</visual>")
_BLOCKED_RE = re.compile(r"captcha|\b40[13]\b", re.I)


class OEBlocked(Exception):
    pass


class OEError(Exception):
    pass


@dataclass
class OEResult:
    article_id: str
    answer: str
    citations: list[dict] = field(default_factory=list)
    figures: list[dict] = field(default_factory=list)  # {name, caption, path, index}：已下載、文中有標出處的附圖
    tables: list[dict] = field(default_factory=list)   # {header, rows}：OpenEvidence 原文表格（逐字）
    # OE 簡報（§6.2c）：完整原文（含圖片、表格標記，去掉追問建議）、對應不上編號的文獻、所有已下載的圖
    raw: str = ""
    other_citations: list[dict] = field(default_factory=list)
    deck_figures: list[dict] = field(default_factory=list)  # {name, caption, path, index|None, marker}
    missing_figures: int = 0


def _clean_answer(text: str) -> str:
    text = _VISUAL_RE.sub("", text)
    text = _MD_IMAGE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_HTTP_RE = re.compile(r"https?://", re.I)


def _citation(i: int, detail: dict) -> dict:
    href = str(detail.get("href") or "").strip()
    year = re.search(r"\d{4}", detail.get("dt_published") or "")
    authors = detail.get("authors_string") or ""
    first = re.split(r"[,;]", authors)[0].strip().split(" ")[0] if authors else ""
    return {
        "index": i,
        "title": detail.get("title") or "",
        "journal": detail.get("journal_name") or "",
        "year": year.group(0) if year else "",
        "doi": detail.get("doi") or "",
        "first_author": first,
        "authors": authors,
        # 顯示出處原文用（§6.2a）：有 PMID 直接抓摘要；指引 PDF 等沒有 DOI 的文件只能靠 OpenEvidence 給的連結
        "pmid": str(detail.get("pmid") or ""),
        "href": href if _HTTP_RE.match(href) else "",
    }


_HTML_RE = re.compile(r"<[^>]+>|\*\*")
_MARK_RE = re.compile(r"\[(\d+)\]")


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
MIN_SPAN_KEY = 4  # 太短的片段（例如只有「。」）會對到錯的行，不拿來對應


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", _MD_LINK_RE.sub(lambda m: m.group(1), _HTML_RE.sub("", s))).strip()


def citations_by_marker(article: dict) -> list[dict]:
    """OpenEvidence 文中的 [n] 是它自己的編號，不是文獻清單的順序。

    實測（2026-11-01，英文與中文答案各一）：每一段（span）的 citations 與 raw_text 中該段
    [n] 的「出現順序（含重複）」一一對應，例如 [3][4][7][9][9][7] 對 6 筆文獻。
    段落編號數與文獻數不符、或同一編號在兩段對到不同文獻 → 該編號不採用（寧可沒有出處也不標錯）。
    """
    sa = (article.get("output") or {}).get("structured_article") or {}
    lines = [_plain(l) for l in (sa.get("raw_text") or "").splitlines() if l.strip()]
    mapping: dict[int, dict] = {}
    bad: set[int] = set()
    for sec in sa.get("articlesection_set") or []:
        for para in sec.get("articleparagraph_set") or []:
            for span in para.get("articlespan_set") or []:
                cits = span.get("citations") or []
                key = _plain(span.get("text") or "").lstrip("。.，,；;：: ")[:40]
                if not cits or len(key) < MIN_SPAN_KEY:
                    continue  # 找不到可靠的行就不用這段，也不因它判定衝突
                line = next((l for l in lines if key in l), "")
                marks = [int(x) for x in _MARK_RE.findall(line)]
                if len(marks) != len(cits):
                    bad.update(marks)
                    continue
                for m, c in zip(marks, cits):
                    detail = (c.get("metadata") or {}).get("citation_detail") or {}
                    entry = _citation(m, detail)
                    if m in mapping and mapping[m]["title"] != entry["title"]:
                        bad.add(m)
                    mapping.setdefault(m, entry)
    return [mapping[m] for m in sorted(mapping) if m not in bad]


_MDTABLE_RE = re.compile(r"<mdtable>(.*?)</mdtable>", re.S)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables_from_answer(text: str) -> list[dict]:
    """OpenEvidence 用 <mdtable> 包住 markdown 表格；逐字取出（不經 AI 改寫）。不像表格的略過。"""
    tables = []
    for block in _MDTABLE_RE.findall(text):
        lines = [l for l in block.strip().splitlines() if l.strip().startswith("|")]
        if len(lines) < 3 or not re.fullmatch(r"[\s|:\-]+", lines[1]):
            continue
        header = _cells(lines[0])
        rows = [_cells(l) for l in lines[2:]]
        width = max([len(header)] + [len(r) for r in rows])
        # 實測：OpenEvidence 有時每列多一欄文獻編號卻沒有表頭 → 補表頭「出處」，短的列補空格
        header += ["出處"] * (width - len(header))
        tables.append({"header": header, "rows": [r + [""] * (width - len(r)) for r in rows]})
    return tables


_VISUAL_MARK_RE = re.compile(r"<visual>([^\[<]+?)\[(\d+)\]</visual>")


def figures_by_marker(article: dict, figures: list[dict]) -> list[dict]:
    """附圖的出處：answer 原文 <visual>Figure 1.1[12]</visual> 的 [12]。沒有出處或沒下載到的圖不採用。"""
    sa = (article.get("output") or {}).get("structured_article") or {}
    marks: dict[str, int] = {}
    for name, idx in _VISUAL_MARK_RE.findall(sa.get("raw_text") or ""):
        marks.setdefault(name.strip(), int(idx))
    out = []
    for f in figures:
        name = (f.get("name") or "").strip()
        path = f.get("localPath") or ""
        if name in marks and path and Path(path).is_file():
            out.append({"name": name, "caption": f.get("caption") or "", "path": path, "index": marks[name]})
    return out


def _run(script: Path, args: list[str], timeout: float, runner, env: dict | None = None):
    """經 oe_relay 執行 oe.py：瀏覽器分頁的 relay 有連線就走分頁（不被機器人驗證擋），否則照舊用 cookies。"""
    from . import oe_relay

    cmd = [sys.executable, oe_relay.__file__, *args]
    env = dict(env or os.environ, XIAOSHEN_OE_SCRIPT=str(script))
    try:
        return runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise OEError("OpenEvidence 逾時") from e
    except OSError as e:
        raise OEError(f"無法執行 OpenEvidence 工具：{e}") from e


def strip_followups(text: str) -> str:
    """去掉 OpenEvidence 的追問建議（<follow-up-question>、<follow-up-topic>）。"""
    return re.sub(r"\n{3,}", "\n\n", _FOLLOWUP_RE.sub("", text)).strip()


def other_citations(article: dict, mapped: list[dict]) -> list[dict]:
    """文中出現、但編號對應不上的文獻（依標題去重）：OE 簡報列在「OE 也引用了以下文獻（未標編號）」，不標編號。"""
    sa = (article.get("output") or {}).get("structured_article") or {}
    have = {c["title"] for c in mapped}
    out: list[dict] = []
    for sec in sa.get("articlesection_set") or []:
        for para in sec.get("articleparagraph_set") or []:
            for span in para.get("articlespan_set") or []:
                for c in span.get("citations") or []:
                    detail = (c.get("metadata") or {}).get("citation_detail") or {}
                    entry = _citation(None, detail)
                    if entry["title"] and entry["title"] not in have:
                        have.add(entry["title"])
                        out.append(entry)
    return out


_CITE_MARKER = re.compile(r"\[(\d+(?:\s*[,\-]\s*\d+)*)\]")


def _paper_key(c: dict) -> str:
    doi = str(c.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    title = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(c.get("title") or "").lower())
    return "title:" + title if len(title) >= 20 else ""


def _renumber(text: str, mapping: dict[int, int]) -> str:
    def fix(m: re.Match) -> str:
        nums: list[int] = []
        for part in re.split(r"\s*,\s*", m.group(1)):
            if "-" in part:
                a, b = (int(x) for x in re.split(r"\s*-\s*", part)[:2])
                nums += list(range(a, b + 1)) if a <= b else [a, b]
            else:
                nums.append(int(part))
        if not any(n in mapping for n in nums):
            return m.group(0)  # 沒有重複的編號：照原文
        out = list(dict.fromkeys(mapping.get(n, n) for n in nums))
        return "[" + ", ".join(str(n) for n in out) + "]"
    return _CITE_MARKER.sub(fix, text)


def merge_duplicate_citations(oe: OEResult) -> OEResult:
    """OE 常把同一篇（同 DOI，沒有 DOI 時同篇名）分成好幾個編號引用：一律改成最小的那個編號，
    文獻只留一筆；內文、圖、表的編號一起改（2026-11-19）。"""
    first: dict[str, int] = {}
    mapping: dict[int, int] = {}
    for c in sorted((c for c in oe.citations if c.get("index") is not None), key=lambda c: c["index"]):
        key = _paper_key(c)
        if not key:
            continue
        if key in first:
            mapping[c["index"]] = first[key]
        else:
            first[key] = c["index"]
    if mapping:
        oe.citations = [c for c in oe.citations if c.get("index") not in mapping]
        oe.answer, oe.raw = _renumber(oe.answer, mapping), _renumber(oe.raw, mapping)
        for f in list(oe.figures) + list(oe.deck_figures):
            if f.get("index") in mapping:
                f["index"] = mapping[f["index"]]
        oe.tables = tables_from_answer(oe.answer)
    oe.other_citations = [c for c in oe.other_citations if not _paper_key(c) or _paper_key(c) not in first]
    return oe


def deck_figures(article: dict, figures: list[dict], answer_raw: str) -> tuple[list[dict], int]:
    """OE 簡報用的圖：所有已下載的圖，附在原文中的位置（marker）與出處編號（沒有就 None）。回傳（圖, 沒下載到的張數）。"""
    sa = (article.get("output") or {}).get("structured_article") or {}
    marks: dict[str, int | None] = {}
    for name, idx in _VISUAL_ANY_RE.findall(sa.get("raw_text") or ""):
        marks.setdefault(name.strip(), int(idx) if idx else None)
    images = {m.group(1): m.group(0) for m in _MD_IMAGE_URL_RE.finditer(answer_raw)}
    out, missing = [], 0
    for f in figures:
        name, path = (f.get("name") or "").strip(), f.get("localPath") or ""
        if not (path and Path(path).is_file()):
            missing += 1
            continue
        marker = images.get(f.get("url") or "", "")
        out.append({"name": name, "caption": f.get("caption") or "", "path": path, "index": marks.get(name),
                    "marker": marker})
    return out, missing


def ask_openevidence(question: str, script: Path, timeout: float = 180, runner=subprocess.run) -> OEResult:
    artifact_dir = tempfile.mkdtemp(prefix="xiaoshen-oe-")
    env = dict(os.environ, OE_ARTIFACT_DIR=artifact_dir, PYTHONIOENCODING="utf-8")
    proc = _run(script, ["ask", question, "--timeout", str(int(timeout)), "--no-bibtex"], timeout + 30, runner, env)
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None
    if proc.returncode != 0 or not isinstance(data, dict):
        if _BLOCKED_RE.search(stdout + stderr):
            raise OEBlocked(BLOCKED_MSG)
        raise OEError(f"OpenEvidence 失敗：{(stderr or stdout).strip()[:200]}")
    answer = _clean_answer(data.get("extracted_answer_raw") or "")
    if not answer:
        raise OEError("OpenEvidence 沒有回答（可能仍在處理或逾時）")
    raw = strip_followups(data.get("extracted_answer_raw") or "")
    citations: list[dict] = []
    figures: list[dict] = []
    others: list[dict] = []
    dfigs: list[dict] = []
    missing = 0
    artifacts = data.get("artifacts") or {}
    path = artifacts.get("articlePath")
    if path and Path(path).exists():
        article = json.loads(Path(path).read_text(encoding="utf-8"))
        citations = citations_by_marker(article)
        others = other_citations(article, citations)
        fig_path = artifacts.get("figuresJsonPath")
        if fig_path and Path(fig_path).exists():
            fig_json = json.loads(Path(fig_path).read_text(encoding="utf-8"))
            figures = figures_by_marker(article, fig_json)
            dfigs, missing = deck_figures(article, fig_json, raw)
    return merge_duplicate_citations(OEResult(str(data.get("article_id") or ""), answer, citations, figures,
                                              tables_from_answer(answer), raw=raw, other_citations=others,
                                              deck_figures=dfigs, missing_figures=missing))


def oe_auth_ok(script: Path, runner=subprocess.run) -> bool:
    try:
        proc = _run(script, ["auth-status"], 30, runner)
        return json.loads(proc.stdout or "").get("authenticated") is True
    except (OEError, json.JSONDecodeError, AttributeError):
        return False

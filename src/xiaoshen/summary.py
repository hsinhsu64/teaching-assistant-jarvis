"""原文重點摘要簡報（spec §6.2b）：Gemini 依文件類型整理整份原文，每一點由程式逐字核對原文。

對不上原文的點照樣保留，標「未能對照原文」；頁碼以程式找到的那一頁為準。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .grounding import match_quote
from .llm import LLMError
from .source_text import SourceDoc

_SYSTEM = (
    "你是醫學教學助理，要把一份醫學文件整理成給與會人員的簡報，內容要完整、紮實，不能只寫背景。\n"
    "先判斷文件類型（doc_type）：study＝原始研究，guideline＝臨床指引或共識，review＝綜論。依類型必須涵蓋：\n"
    "- study：背景與目的、研究設計、對象（人數、年齡、納入條件）、介入與對照、主要結果（寫出數字與統計）、"
    "次要結果、安全性（不良事件）、結論與限制。\n"
    "- guideline：每一條建議都要列出（照原文編號與順序，不要合併或省略），寫出證據等級或建議強度、適用對象；"
    "另列診斷、治療、追蹤的重點。\n"
    "- review：照原文章節，每節的重點與數字。\n"
    "規則：\n"
    "1. 依文件順序、按章節分組成多張投影片；投影片張數與每張的點數都不限，不要省略重點。\n"
    "2. 每張投影片的 title 用中文；每一點 zh 用中文寫成有內容的整理（不是逐句翻譯），藥名、劑量、疾病名維持英文。\n"
    "3. 每一點的 quotes 放 1～3 句支持這一點的原文，每句都從原文逐字複製一整句（保留原文語言，不翻譯、不改寫）；"
    "page 填第一句所在的頁碼（標示為 [第 N 頁]；沒有頁碼填 0）。\n"
    "4. 只能整理文件裡寫的內容，不得加入文件以外的知識。"
)

MAX_QUOTES = 3


def system_prompt() -> str:
    return _SYSTEM


SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["study", "guideline", "review"]},
        "slides": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "points": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"zh": {"type": "string"}, "quotes": {"type": "array", "items": {"type": "string"}},
                                   "page": {"type": "integer"}},
                    "required": ["zh", "quotes", "page"],
                }},
            },
            "required": ["title", "points"],
        }},
    },
    "required": ["slides"],
}

# 輸出被截斷（JSON 不完整）時才分段重做；逾時、金鑰錯誤等分段也不會好
_TRUNCATED_HINTS = ("JSON", "格式")


class SummaryError(Exception):
    """整理失敗；訊息給主持人看或由助理念出。"""


@dataclass
class SummaryPoint:
    zh: str
    quotes: list[str]
    page: int | None
    verified: bool


@dataclass
class SummarySlide:
    title: str
    points: list[SummaryPoint]


@dataclass
class Summary:
    doc: SourceDoc
    slides: list[SummarySlide]
    truncated_at: int | None
    model: str
    doc_type: str = ""

    @property
    def unverified(self) -> int:
        return sum(1 for s in self.slides for p in s.points if not p.verified)


def _label(doc: SourceDoc, i: int) -> str:
    return f"[第 {i + 1} 頁]" if doc.paged else "[全文]"


def _prompt(doc: SourceDoc, indices: list[int]) -> str:
    body = "\n\n".join(f"{_label(doc, i)}\n{doc.pages[i]}" for i in indices)
    return f"文件：{doc.title}\n出處：{doc.citation}\n\n{body}"


def _chunks(doc: SourceDoc, indices: list[int], chunk_chars: int) -> list[list[int]]:
    out: list[list[int]] = [[]]
    size = 0
    for i in indices:
        n = len(doc.pages[i])
        if out[-1] and size + n > chunk_chars:
            out.append([])
            size = 0
        out[-1].append(i)
        size += n
    return out


def _call(llm, model: str, doc: SourceDoc, indices: list[int], timeout: float, thinking: str = "") -> dict:
    kw = {"thinking": thinking} if thinking else {}
    return llm.generate_json(model, system_prompt(), _prompt(doc, indices), SCHEMA, timeout=timeout, **kw)


def _quotes(raw: dict) -> list[str]:
    qs = raw.get("quotes")
    if not isinstance(qs, list):
        qs = [raw.get("quote")] if raw.get("quote") else []  # 舊格式：單一 quote
    return [str(q) for q in qs if str(q or "").strip()][:MAX_QUOTES]


def _verify(doc: SourceDoc, raw: dict) -> SummaryPoint:
    """每一句引文都要逐字對上原文才算核對過；頁碼取第一句所在的頁。"""
    zh, quotes = str(raw.get("zh") or ""), _quotes(raw)
    claimed = raw.get("page")
    order = list(range(len(doc.pages)))
    if isinstance(claimed, int) and 1 <= claimed <= len(doc.pages):
        order.remove(claimed - 1)
        order.insert(0, claimed - 1)
    pages = []
    for q in quotes:
        hit = next((i for i in order if match_quote(q, doc.pages[i])), None)
        if hit is None:
            return SummaryPoint(zh, quotes, None, False)
        pages.append(hit)
    if not quotes:
        return SummaryPoint(zh, quotes, None, False)
    return SummaryPoint(zh, quotes, pages[0] + 1 if doc.paged else None, True)


def summarize(doc: SourceDoc, llm, model: str, timeout: float, max_chars: int, chunk_chars: int,
              thinking: str = "") -> Summary:
    deadline = time.monotonic() + timeout
    indices, total = [], 0
    for i, page in enumerate(doc.pages):
        if indices and total + len(page) > max_chars:
            break
        indices.append(i)
        total += len(page)
    truncated_at = indices[-1] + 1 if len(indices) < len(doc.pages) else None
    doc_type = ""
    try:
        data = _call(llm, model, doc, indices, timeout, thinking)
        raw, doc_type = list(data.get("slides") or []), str(data.get("doc_type") or "")
    except LLMError as e:
        if not any(h in str(e) for h in _TRUNCATED_HINTS):
            raise SummaryError(f"整理失敗：{e}") from e
        raw = []
        chunks = _chunks(doc, indices, chunk_chars)
        for k, chunk in enumerate(chunks):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SummaryError("整理太久，已取消") from e
            try:
                data = _call(llm, model, doc, chunk, remaining / (len(chunks) - k), thinking)
                raw += list(data.get("slides") or [])
                doc_type = doc_type or str(data.get("doc_type") or "")
            except LLMError as e2:
                raise SummaryError("整理失敗，請再試一次") from e2
    slides = [SummarySlide(str(s.get("title") or ""), [_verify(doc, p) for p in (s.get("points") or [])])
              for s in raw]
    slides = [s for s in slides if s.points]
    if not slides:
        raise SummaryError("模型沒有整理出重點")
    return Summary(doc, slides, truncated_at, model, doc_type)

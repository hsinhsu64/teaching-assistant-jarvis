"""問「目前的結果」：投影上已列出的文獻（最近一次 OpenEvidence／PubMed 回答的文獻清單）。

看清單就能答的（年份、期刊、作者、篇數）交給快模型，答案引用的編號必須在清單上，出處用清單現成的標籤；
要讀內容才能答的回 needs_reading，改讀摘要，走 answer_from_pages 的引文驗證。不得用模型知識補答。
"""

from __future__ import annotations

import re
from typing import Callable

from .answering import (LITERATURE_ROUTES, Answer, Card, _new_id, answer_from_pages, citation_label,
                        friendly_error)
from .llm import LLM, LLMError

MAX_READ = 10  # 讀內容時最多讀前幾篇的摘要
NO_ABSTRACTS = "抓不到這些文獻的摘要"
CANNOT_ANSWER = "無法從文獻清單回答"
_MENTION = re.compile(r"第\s*(\d+)\s*篇|\[(\d+)\]")

LIST_SYSTEM = (
    "你會拿到一份已列出的文獻清單（編號、第一作者、年份、期刊、標題）和一個問題。"
    "只能根據清單上的這些欄位回答，例如哪一篇最新、第幾篇是哪個期刊、誰是第一作者、總共幾篇。"
    "不得用你自己的知識補充。若問題要讀文獻的內容才能回答（研究設計、病人數、結果、結論等），"
    "kind 填 needs_reading，其他欄位留空。能回答時 kind 填 answer，answer_zh 用一兩句繁體中文（台灣用語）回答，"
    "提到文獻時寫出編號（例如「第 2 篇」），refs 填答案用到的文獻編號。"
)
LIST_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["answer", "needs_reading"]},
    "answer_zh": {"type": "string"},
    "refs": {"type": "array", "items": {"type": "integer"}}},
    "required": ["kind", "answer_zh", "refs"]}


def is_results_base(ans: Answer | None) -> bool:
    """可以當作「目前的結果」的回答：查到文獻的 OpenEvidence／PubMed 回答。"""
    return (ans is not None and ans.route in LITERATURE_ROUTES and ans.status == "answered"
            and bool(ans.references))


def result_refs(base: Answer) -> list[dict]:
    return [base.references[i] for i in sorted(base.references)]


def _line(r: dict) -> str:
    meta = " ".join(str(x) for x in (r.get("first_author"), r.get("year"), r.get("journal")) if x)
    return f"[{r['index']}] {meta}：{r.get('title') or ''}".rstrip("：")


def _new(question: str, base: Answer) -> Answer:
    return Answer(_new_id(), question, base.route, "error")


def answer_from_list(question: str, base: Answer, llm: LLM, model: str, timeout: float = 30) -> Answer | str:
    """回 Answer（answered／error）或 "needs_reading"。"""
    ans = _new(question, base)
    prompt = f"問題：{question}\n\n文獻清單：\n" + "\n".join(_line(r) for r in result_refs(base))
    try:
        data = llm.generate_json(model, LIST_SYSTEM, prompt, LIST_SCHEMA, timeout=timeout)
    except LLMError as e:
        ans.error, ans.error_detail = friendly_error(e), str(e)
        return ans
    if data.get("kind") == "needs_reading":
        return "needs_reading"
    text = str(data.get("answer_zh") or "").strip()
    refs = [r for r in data.get("refs") or [] if isinstance(r, int) and not isinstance(r, bool)]
    mentioned = [int(n) for n in _MENTION.findall(text) for n in n if n]  # 答案文字裡寫到的編號也要在清單上
    refs = list(dict.fromkeys(refs + mentioned))
    if not text or not refs or any(r not in base.references for r in refs):
        ans.error = CANNOT_ANSWER
        return ans
    ans.status = "answered"
    ans.cards = [Card(text, "", "；".join(citation_label(base.references[r]) for r in refs), refs=refs)]
    ans.references = {r: base.references[r] for r in refs}
    return ans


def answer_from_abstracts(question: str, base: Answer, fetch: Callable[[dict], tuple[str, str]], llm: LLM,
                          model: str, max_chars: int) -> Answer:
    """讀前 MAX_READ 篇的摘要後回答；字卡出處標回文獻編號（可再叫出摘要／原文網頁）。"""
    sources, by_label, missing = [], {}, []
    for r in result_refs(base)[:MAX_READ]:
        try:
            abstract, _title = fetch(r)
        except Exception:
            abstract = ""
        if not abstract.strip():
            missing.append(r["index"])
            continue
        label = citation_label(r)
        sources.append((label, [abstract]))
        by_label[label] = r["index"]
    if not sources:
        ans = _new(question, base)
        ans.error = NO_ABSTRACTS
        return ans
    ans = answer_from_pages(question, sources, llm, model, max_chars, route="references", spoken=True)
    ans.route = base.route
    used: list[int] = []
    for c in ans.cards:
        idx = by_label.get(c.doc)
        c.source, c.doc, c.page = c.doc, "", 0
        c.refs = [idx] if idx is not None else []
        used += c.refs
    ans.references = {i: base.references[i] for i in sorted(set(used))}
    if missing:
        ans.fallback_note = "第 " + "、".join(str(i) for i in missing) + " 篇抓不到摘要"
    return ans

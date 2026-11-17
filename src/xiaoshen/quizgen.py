"""依來源頁面出 N 題選擇題；每題的出處由程式驗證，未通過者丟棄。結果先成為主持人畫面的草稿。"""

from __future__ import annotations

import uuid

from . import persona
from .answering import Source, check_sources, locate, pages_block, source_name
from .llm import LLM
from .quiz import MAX_OPTIONS, MIN_OPTIONS, Question

MIN_N, MAX_N = 1, 20

_SYSTEM_PROMPT = (
    "你是{ctx}的出題助手。只能根據提供的資料出單選題；"
    "每題都必須附上逐字複製自資料、能直接支持正解的原文句子或片語（quote）與其出處（資料標籤與頁碼）；"
    "原文保持資料原本的語言，不可翻譯、不可改寫數字或用字，不可只引標題或單一名詞；"
    "資料中沒有的內容不得出題。題幹、解析用繁體中文，藥名、劑量、疾病名維持英文；"
    "選項 4 個為原則（2–6 個），選項不可重複，只有一個正解。"
)

SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "stem": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "minItems": MIN_OPTIONS, "maxItems": MAX_OPTIONS},
            "answer_index": {"type": "integer"},
            "explanation_zh": {"type": "string"},
            "quote": {"type": "string"},
            "source_label": {"type": "string"},
            "page": {"type": "integer"},
        },
        "required": ["stem", "options", "answer_index", "explanation_zh", "quote", "source_label", "page"],
    }}},
    "required": ["questions"],
}


def system_prompt() -> str:
    return _SYSTEM_PROMPT.replace("{ctx}", persona.context(persona.current()))


def _to_question(item: dict, by_label: dict[str, list[str]]) -> Question | None:
    stem = str(item.get("stem") or "").strip()
    options = item.get("options")
    answer = item.get("answer_index")
    label, page, quote = str(item.get("source_label") or ""), item.get("page"), str(item.get("quote") or "")
    if not stem or not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return None
    options = [str(o).strip() for o in options]
    if any(not o for o in options) or len({o.lower() for o in options}) != len(options):
        return None
    if not isinstance(answer, int) or isinstance(answer, bool) or not 0 <= answer < len(options):
        return None
    hit = locate(label, page, quote, by_label)
    if hit is None:
        return None
    src_label, src_page, m = hit
    return Question("g-" + uuid.uuid4().hex[:8], stem, options, answer,
                    str(item.get("explanation_zh") or ""),
                    f"{source_name(src_label, src_page, by_label[src_label][src_page - 1], m)}：{m.text[:80]}")


def generate_questions(n: int, sources: list[Source], llm: LLM, model: str, max_chars: int,
                       focus: str = "") -> tuple[list[Question], list[str]]:
    if not MIN_N <= n <= MAX_N:
        raise ValueError(f"題數需介於 {MIN_N}–{MAX_N}")
    problem = check_sources(sources, max_chars)
    if problem:
        raise ValueError(problem)
    prompt = f"請出 {n} 題。"
    if focus:
        prompt += f"\n出題重點：{focus}"
    prompt += f"\n\n資料：\n{pages_block(sources)}"
    data = llm.generate_json(model, system_prompt(), prompt, SCHEMA)
    by_label = dict(sources)
    questions = []
    for item in data.get("questions") or []:
        if isinstance(item, dict) and (qn := _to_question(item, by_label)):
            questions.append(qn)
    questions = questions[:n]
    notes = [f"要求 {n} 題，通過驗證 {len(questions)} 題"]
    return questions, notes

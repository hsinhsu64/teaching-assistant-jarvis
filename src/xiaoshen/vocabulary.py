"""從今天的簡報抽專有名詞，叫醒時交給語音模型當詞彙提示（減少把 Bartter 聽成 Butter）。

含病人識別資訊的那一行整行不用（隱私：這些詞每次叫醒都會送到 Gemini）。
"""

from __future__ import annotations

import re
from collections import Counter

from .phi import find_phi

MAX_TITLE = 50
_PHRASE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")   # Cerebral Salt Wasting
_WORD = re.compile(r"\b[A-Z][a-z]{3,}\b")                        # Bartter, Hyponatremia
_ACRONYM = re.compile(r"\b[A-Za-z0-9]*[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*\b")  # RTA, SIADH, PaCO2
_STOP = {"The", "This", "That", "With", "From", "Case", "Step", "Example", "When", "What", "Which", "Normal"}


def _clean_lines(page: str) -> list[str]:
    return [l.strip() for l in page.splitlines() if l.strip() and not find_phi(l)]


def deck_terms(pages: list[str], max_chars: int = 1500) -> list[str]:
    """回傳詞彙清單：先放每頁標題（第一行），再放出現次數多的英文名詞與縮寫。"""
    titles: list[str] = []
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [l.strip() for l in page.splitlines() if l.strip()]
        title = " ".join(lines[0].split()) if lines else ""
        if title and len(title) <= MAX_TITLE and not find_phi(title):
            titles.append(title)
        for line in _clean_lines(page):
            for rx in (_PHRASE, _WORD, _ACRONYM):
                for m in rx.findall(line):
                    if m not in _STOP and not m.isdigit() and len(m) >= 2:
                        counts[m] += 1
    out: list[str] = []
    used = 0
    for term in titles + [t for t, _ in counts.most_common()]:
        if term in out:
            continue
        if used + len(term) + 1 > max_chars:
            break
        out.append(term)
        used += len(term) + 1
    return out


def vocabulary_prompt(terms: list[str]) -> str:
    if not terms:
        return ""
    return ("\n七、今天簡報的專有名詞如下（語音辨識常聽錯英文藥名、病名，聽成發音相近的普通英文字）："
            + "、".join(terms)
            + "。使用者提到發音相近的詞時，請依上下文改成正確名詞，再轉述問題給工具。")


def deck_context(state):
    """回傳給 VoiceSession 的 context()：依目前開啟的簡報產生詞彙提示，換簡報才重算。"""
    cache: dict[str, str] = {}

    def context() -> str:
        return vocab() + pending_prompt(state.active_voice_confirm())

    def vocab() -> str:
        deck = state.answer_deck
        if deck is None:
            return ""
        key = state.deck_version
        if key not in cache:
            cache.clear()
            cache[key] = vocabulary_prompt(deck_terms([deck.page_text(i) for i in range(deck.page_count)]))
        return cache[key]

    return context


def pending_prompt(pending: dict | None) -> str:
    """叫醒新的一輪對話時，還在等確認的語音查詢也告訴語音模型（§5.4，2026-11-06）。"""
    if not pending:
        return ""
    return (f"\n另外，上一輪已念出要確認的查詢：「{pending['question']}」。使用者說「對」「沒錯」「查吧」時，"
            f"呼叫 ask，question 照這句、confirmed=true；說出別的詞就用改好的問題呼叫 ask。")

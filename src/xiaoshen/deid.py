"""逐字稿去識別：送 Gemini 前把勾選的字串換成代號，回來再換回真名（對照表只存在筆電本機）。

代號：人名 → 病人A、病人B…；其他 → 〔病歷號1〕〔身分證1〕〔電話1〕〔生日1〕〔床號1〕〔其他1〕。
- 長字串先換（「王小明」先於「小明」），一次掃過，不會換到已經換好的代號；
- 正文原本就有同樣（或改寫後一樣）的字樣（例如有人說「病人A」）時，改用下一個沒出現過的代號，換回時不會混淆；
- 換回時容忍模型改寫：代號中間加空白、全形字母數字、大小寫、括號換成【】[]（）或拿掉括號。
"""

from __future__ import annotations

import re
import string

from .phi import find_phi

KIND_LABEL = {"chart": "病歷號", "id": "身分證", "phone": "電話", "birthday": "生日", "bed": "床號", "other": "其他"}
PERSON_PREFIX = "病人"
_OPEN, _CLOSE = "〔", "〕"
_OPEN_RE, _CLOSE_RE = r"[〔【\[（(]", r"[〕】\]）)]"
_LETTER = "A-Za-zＡ-Ｚａ-ｚ"
_DIGIT = "0-9０-９"
# 換回後還留著的、看起來像代號的字樣（代號被改寫到認不出來、或模型自己編的）。
# 人名代號最多兩個大寫字母（「病人CRP」「病人BUN」是檢驗，不算）；其他代號括號可有可無、最多三位數。
_LEFTOVER = re.compile(PERSON_PREFIX + rf"\s*[A-ZＡ-Ｚ]{{1,2}}(?![{_LETTER}])|"
                       rf"(?:{_OPEN_RE}\s*)?(?:" + "|".join(KIND_LABEL.values()) +
                       rf")\s*[{_DIGIT}]{{1,3}}(?![{_DIGIT}])(?:\s*{_CLOSE_RE})?")


def _letters(n: int) -> str:
    """0 → A、25 → Z、26 → AA…"""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


def pseudonymize(text: str, items: list[dict]) -> tuple[str, dict[str, str]]:
    """items：勾選的 {text, kind}。回（換好的文字, 代號 → 原文）。不在文字裡的項目不給代號。"""
    wanted: dict[str, str] = {}
    for it in items:
        t = (it.get("text") or "").strip()
        if t and t in text and t not in wanted:
            wanted[t] = it.get("kind") or "other"
    if not wanted:
        return text, {}
    pattern = re.compile("|".join(re.escape(t) for t in sorted(wanted, key=len, reverse=True)))
    # 依第一次出現的順序給代號
    order: list[str] = []
    for m in pattern.finditer(text):
        if m.group(0) not in order:
            order.append(m.group(0))
    codes: dict[str, str] = {}
    counters: dict[str, int] = {}
    used = set()
    for orig in order:
        kind = wanted[orig]
        if kind == "person":
            n = counters.get("person", 0)
            counters["person"] = n + 1
            code = PERSON_PREFIX + _letters(n)
        else:
            label = KIND_LABEL.get(kind, KIND_LABEL["other"])
            n = counters.get(label, 0) + 1
            counters[label] = n
            code = f"{_OPEN}{label}{n}{_CLOSE}"
        # 正文裡已經有這個代號（或改寫後一樣的字樣）：換下一個，換回時才不會分不清
        while code in used or re.search(_code_regex(code), text):
            if kind == "person":
                n = counters["person"]
                counters["person"] = n + 1
                code = PERSON_PREFIX + _letters(n)
            else:
                n = counters[label] + 1
                counters[label] = n
                code = f"{_OPEN}{label}{n}{_CLOSE}"
        used.add(code)
        codes[orig] = code
    out = pattern.sub(lambda m: codes[m.group(0)], text)
    return out, {c: o for o, c in codes.items()}


def _char_pattern(ch: str) -> str:
    if ch in string.ascii_letters:
        up, low = ch.upper(), ch.lower()
        return f"[{up}{low}{chr(ord(up) + 0xFEE0)}{chr(ord(low) + 0xFEE0)}]"
    if ch in string.digits:
        return f"[{ch}{chr(ord(ch) + 0xFEE0)}]"
    return re.escape(ch)


def _code_regex(code: str) -> str:
    if code.startswith(_OPEN):  # 〔床號1〕：括號可換成別種或拿掉；數字後面不可再接數字
        inner = code[1:-1]
        body = r"\s*".join(_char_pattern(c) for c in inner)
        return rf"(?:{_OPEN_RE}\s*)?{body}(?![{_DIGIT}])(?:\s*{_CLOSE_RE})?"
    # 病人A：不可吃掉「病人AB」的前半
    return r"\s*".join(_char_pattern(c) for c in code) + rf"(?![{_LETTER}])"


def code_like(text: str) -> list[str]:
    """文字裡看起來像代號的字樣（去識別前的原文本來就有的，換回時不要報成「換不回」）。"""
    out: list[str] = []
    for m in _LEFTOVER.finditer(text):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def reidentify(text: str, mapping: dict[str, str], ignore=()) -> tuple[str, list[str]]:
    """代號換回原文。回（換回的文字, 換不回的代號樣字串）；ignore：原文本來就有的同樣字樣。"""
    if mapping:
        codes = sorted(mapping, key=len, reverse=True)
        alts = [f"(?P<c{i}>{_code_regex(c)})" for i, c in enumerate(codes)]
        pattern = re.compile("|".join(alts))

        def sub(m: re.Match) -> str:
            return mapping[codes[int(m.lastgroup[1:])]]

        text = pattern.sub(sub, text)
    unresolved: list[str] = []
    skip = set(ignore)
    originals = set(mapping.values())
    for s in code_like(text):
        if s not in skip and not any(s in o for o in originals):
            unresolved.append(s)
    return text, unresolved


def residual_phi(text: str, mapping: dict[str, str] | None = None) -> list[str]:
    """換完代號後再掃一次病人識別資訊；代號本身（例如〔床號1〕會被「床號＋數字」規則命中）先拿掉。"""
    for code in sorted(mapping or {}, key=len, reverse=True):
        text = text.replace(code, " ")
    return find_phi(text)

"""打字內容的病人識別資訊規則檢查（送雲端前在本機執行）。

寧可誤擋也不漏：命中任何一條就不送出，由主持人改寫後再問。
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z][12]\d{8}(?!\d)"),            # 身分證字號
    re.compile(r"病[歷历][號号]?\s*[:：]?\s*\d{6,10}(?!\d)"),  # 病歷號（含語音逐字稿的簡體字）
    re.compile(r"(?<![\d.])09\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)"),       # 手機（可含空白）
    re.compile(r"(?<![\d.])0\d{1,2}[-\s]?\d{6,8}(?!\d)"),               # 市話
    re.compile(r"(?<![\d.])\d{2,4}\s*[/\-年.]\s*\d{1,2}\s*[/\-月.]\s*\d{1,2}\s*日?(?![\d.])"),  # 完整生日
    re.compile(r"(?<![\d.])\d{1,2}[A-Za-z]?\d{0,2}\s*床"),              # 床號
    re.compile(r"床[號号]\s*\d+"),
    re.compile(r"(?i)\bbed\s*(?:no\.?|#)?\s*\d+[A-Za-z]?\d*"),          # bed 12
    re.compile(r"(?i)(?<![\d.])\d{1,2}[A-Za-z]?\d{0,2}\s*bed\b"),       # 12 bed
    re.compile(r"(?i)\b(?:chart|mrn|record)\s*(?:no\.?|number|#)?\s*[:：]?\s*\d{5,10}(?!\d)"),  # chart no 654321
    re.compile(r"(?<![A-Za-z\d.])\d{7,10}(?![\d.])"),                          # 孤立的 7–10 位數字（病歷號）
    # 病房＋床號、沒有「床」字：8B191、八B 191（病房字母限大寫，避開 1g 12 小時這類劑量）
    re.compile(r"(?<![A-Za-z\d.])(?:\d{1,2}|[一二三四五六七八九十]{1,2})[A-Z]\s?\d{2,3}"
               r"(?![\d.]|\s*(?:小時|歲|岁|公斤|天|%|mg|ml|mL))"),
    # 「這一床是 X191」：語音常把病房聽成別的字（8B → 芭比）；只取「是」後面的部分（最多 4 個字＋2–4 位數字）
    re.compile(r"床[號号位]?\s*(?:是|為|为)\s*(?P<phi>[^\s\d，,。、；;：:〔〕()（）]{0,4}?\s*\d{2,4})"
               r"(?![\d.]|\s*(?:歲|岁|公斤|kg|天|個|个|月|年|週|禮拜|%|點|点|小時|分))"),
]


# 每條規則的類別（和 _PATTERNS 一一對應）：會議記錄去識別時決定代號（〔病歷號1〕、〔電話1〕…）
_KINDS = ["id", "chart", "phone", "phone", "birthday", "bed", "bed", "bed", "bed", "chart", "chart", "bed", "bed"]
assert len(_KINDS) == len(_PATTERNS)


# 醫院自己的病房（config 的 wards）與語音常聽錯的寫法（ward_misheard，例：8B → 芭比）：
# 「病房（＋病房兩字）＋1–3 位數字」當床號。由 Config 建立時設定；沒設定就不啟用。
_UNITS_AFTER = r"(?![\d.]|\s*(?:小時|歲|岁|公斤|kg|天|個|个|月|年|週|禮拜|%|點|点|分|mg|ml|mL))"
_ward_rule: re.Pattern | None = None
_ward_words: tuple[str, ...] = ()


def configure_wards(wards, misheard) -> None:
    global _ward_rule, _ward_words
    words = tuple(dict.fromkeys(w.strip() for w in [*wards, *misheard] if w and w.strip()))
    _ward_words = words
    if not words:
        _ward_rule = None
        return
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    _ward_rule = re.compile(rf"(?<![A-Za-z0-9])(?:{alt})\s*(?:病房)?\s*\d{{1,3}}" + _UNITS_AFTER)


def ward_words() -> tuple[str, ...]:
    """設定的病房名稱與錯聽寫法（去識別候選不把它們當人名）。"""
    return _ward_words


def find_phi_kinds(text: str) -> list[tuple[str, str]]:
    """回傳命中的（片段, 類別）（去重、保留順序）；類別 id／chart／phone／birthday／bed。"""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    rules = list(zip(_PATTERNS, _KINDS)) + ([(_ward_rule, "bed")] if _ward_rule is not None else [])
    for pattern, kind in rules:
        for m in pattern.finditer(text):
            s = (m.group("phi") if "phi" in pattern.groupindex else m.group(0)).strip()
            if s and s not in seen:
                seen.add(s)
                hits.append((s, kind))
    return hits


def find_phi(text: str) -> list[str]:
    """回傳命中的片段（去重、保留順序）。"""
    return [s for s, _ in find_phi_kinds(text)]

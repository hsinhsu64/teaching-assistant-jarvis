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
]


def find_phi(text: str) -> list[str]:
    """回傳命中的片段（去重、保留順序）。"""
    hits: list[str] = []
    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            s = m.group(0).strip()
            if s and s not in hits:
                hits.append(s)
    return hits

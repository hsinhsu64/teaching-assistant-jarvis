"""出處驗證：模型給的引文必須真的出現在來源文字中。不信任模型自己說的出處。

比對以「字詞」為單位：數字、單位、符號、否定詞、中文字必須逐一相同；
只容許英文長字的拼字小差異（PDF 抽字誤差，例如 cyclosporine／cyclosporin）。
因此改過的劑量、翻轉的否定、相反的方向（increase／decrease、hypo／hyper）都不會通過。
畫面上顯示的是來源裡實際的那段文字，不是模型寫的引文。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

MIN_CHARS = 8
MIN_TOKENS = 3
VARIANT_MIN_RATIO = 0.9  # hypokalemia／hyperkalemia 約 0.87，不會被當成拼字差異
VARIANT_MIN_LEN = 5

_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "‐": "-", "‑": "-"})
_TOKEN = re.compile(r"[㐀-鿿]|[A-Za-zÀ-ɏ]+|\d+(?:\.\d+)?|[^\w\s]|\w")
# 不影響意義的標點：比對時略過。+ - / < > = % ± ~ & 等保留且必須相同。
_IGNORED = set(",;:()[]{}\"'!?·•⚫✦●■◆▪*.、。…")
_TITLE_SIGNAL = re.compile(r"[\d<>=±%/:≤≥]")


@dataclass
class Match:
    text: str   # 來源中實際的那段文字（已合併空白）
    start: int
    end: int    # 在 normalize_keep(來源) 中的位置


def normalize_keep(s: str) -> str:
    """統一字形（NFKC：K⁺→K+、ﬁ→fi、全形冒號）、引號與破折號、PDF 行尾斷字與空白；保留大小寫。"""
    s = unicodedata.normalize("NFKC", s).translate(_QUOTES)
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize(s: str) -> str:
    return normalize_keep(s).lower()


_CITE_MARK = re.compile(r"\[\d+(?:\s*[,\-]\s*\d+)*\]")  # 文獻編號 [3]、[5, 6]、[1-3]：比對時略過


def _tokens(s: str, ignore_citations: bool = False) -> list[tuple[str, int, int]]:
    """ignore_citations：略過 [3]、[5, 6] 這類文獻編號（只用於 OpenEvidence；簡報裡的方括號數字是內容）。"""
    marks = [(m.start(), m.end()) for m in _CITE_MARK.finditer(s)] if ignore_citations else []
    return [(m.group(0).lower(), m.start(), m.end()) for m in _TOKEN.finditer(s)
            if m.group(0) not in _IGNORED and not any(a <= m.start() < b for a, b in marks)]


def _is_variant(a: str, b: str) -> bool:
    if not (a.isascii() and b.isascii() and a.isalpha() and b.isalpha()):
        return False
    if min(len(a), len(b)) < VARIANT_MIN_LEN:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= VARIANT_MIN_RATIO


def _is_title(quote: str, source_text: str) -> bool:
    """引文剛好是來源中獨立的一行，且不含數字或符號 → 視為標題，不算證據。"""
    q = [t for t, _, _ in _tokens(normalize(quote))]
    for line in source_text.splitlines():
        nl = normalize(line)
        if nl and not _TITLE_SIGNAL.search(nl) and [t for t, _, _ in _tokens(nl)] == q:
            return True
    return False


def match_quote(quote: str, source_text: str, ignore_citations: bool = False) -> Match | None:
    q = normalize_keep(quote)
    qt = [t for t, _, _ in _tokens(q, ignore_citations)]
    cjk = sum(1 for ch in q if "\u3400" <= ch <= "\u9fff")  # 中文字資訊量大，算兩倍長
    if len(q) + cjk < MIN_CHARS or len(qt) < MIN_TOKENS or not source_text:
        return None
    if _is_title(quote, source_text):
        return None
    src = normalize_keep(source_text)
    st = _tokens(src, ignore_citations)
    n = len(qt)
    max_variants = n // 4
    best: tuple[int, int] | None = None  # (拼字差異數, 起點)
    for i in range(len(st) - n + 1):
        variants = 0
        for j in range(n):
            a, b = qt[j], st[i + j][0]
            if a == b:
                continue
            if _is_variant(a, b):
                variants += 1
                if variants > max_variants:
                    break
                continue
            variants = -1
            break
        if 0 <= variants <= max_variants and (best is None or variants < best[0]):
            best = (variants, i)
            if variants == 0:
                break
    if best is None:
        return None
    i = best[1]
    start, end = st[i][1], st[i + n - 1][2]
    return Match(src[start:end], start, end)


def verify_quote(quote: str, source_text: str) -> bool:
    return match_quote(quote, source_text) is not None

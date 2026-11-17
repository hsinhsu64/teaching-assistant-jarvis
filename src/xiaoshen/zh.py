"""使用者說的話轉成台灣繁體（用 OpenCC）。

Gemini 的語音逐字稿常是簡體；主畫面顯示、病人識別資訊檢查都用轉好的繁體字。
套件不在時原文照用，不影響其他功能。
"""

from __future__ import annotations

import threading

_converter = None
_lock = threading.Lock()


def to_traditional(text: str) -> str:
    global _converter
    if not text:
        return text
    with _lock:
        if _converter is None:
            try:
                import opencc

                _converter = opencc.OpenCC("s2twp")  # 簡體 → 台灣繁體（含用詞）
            except Exception:
                _converter = False
        if _converter is False:
            return text
        return _converter.convert(text)

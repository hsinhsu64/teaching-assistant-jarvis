"""原文重點摘要簡報（spec §6.2b）：讀出投影上那份原文的全文。

只把 PMC 全文、直接的 PDF 連結、簡報／參考資料 PDF 當作讀得到的全文；其他（DOI、出版社網頁）改讀摘要。
原文只在記憶體處理，不存檔。
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass

import pymupdf

from .deck import PDF_LOCK

USER_AGENT = "xiaoshen/1.0"


class SourceUnreadable(Exception):
    """讀不到原文；訊息直接給主持人看或由助理念出。"""


@dataclass
class SourceDoc:
    title: str
    citation: str
    pages: list[str]
    paged: bool        # True：PDF，有頁碼
    full_text: bool    # False：只讀到摘要
    note: str = ""
    origin: str = ""   # 全文從哪裡來（fulltext.ORIGIN_LABEL 的鍵）


def pdf_pages(data: bytes) -> list[str]:
    """PDF 逐頁文字（保留頁序）。不是 PDF 或掃描檔（沒有文字）→ SourceUnreadable。"""
    if not data.startswith(b"%PDF"):
        raise SourceUnreadable("讀不到這份 PDF")
    with PDF_LOCK:  # MuPDF 不能多執行緒同時操作（審查 #4）
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            raise SourceUnreadable("讀不到這份 PDF") from e
        try:
            pages = [page.get_text() for page in doc]
        finally:
            doc.close()
    if not any(p.strip() for p in pages):
        raise SourceUnreadable("這份是掃描檔，讀不到文字")
    return pages


def fetch_bytes(url: str, timeout: float = 30, max_bytes: int = 60_000_000) -> tuple[bytes, str]:
    """下載到記憶體（不存檔）：回傳（內容, content-type）。超過 max_bytes → SourceUnreadable。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
            ctype = r.headers.get("Content-Type", "")
    except OSError as e:
        raise SourceUnreadable("連不上原文網址") from e
    if len(data) > max_bytes:
        raise SourceUnreadable("檔案太大")
    return data, ctype

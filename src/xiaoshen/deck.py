"""PDF 簡報：頁數、轉 PNG、逐頁文字、掃描檔判斷。"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pymupdf as fitz

# 整份文件的文字字元少於這個數，才當作掃描檔（只有圖片、沒有文字層）。
# 不能用「每頁平均」：投影片常只有短標題或整頁圖片。
SCANNED_MAX_CHARS = 5


# PyMuPDF 不支援多執行緒（不論是否同一份文件）：開檔、轉圖、抽字、畫字卡都共用這把鎖
PDF_LOCK = threading.RLock()


class DeckError(Exception):
    pass


# 轉圖改在子行程做（render_proc.ProcessRenderer）：PyMuPDF 轉圖時不放開 GIL，
# 在主行程轉整份簡報會卡住語音播放而破音。沒設定時（測試、子行程本身）就在本行程轉。
_RENDERER = None


def set_renderer(renderer) -> None:
    global _RENDERER
    _RENDERER = renderer


def _render_page(page: "fitz.Page", width: int, phrases: tuple[str, ...] = ()) -> bytes:
    """phrases：要用螢光框起來的字（顯示出處那一頁，§6.2a）。在複本上加標註，不動到原文件。"""
    if phrases:
        tmp = fitz.open()
        tmp.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        page = tmp[0]
        for ph in phrases:
            for rect in page.search_for(ph):
                page.add_highlight_annot(rect)
    zoom = width / page.rect.width
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=True).tobytes("png")


class Deck:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.name = self.path.stem
        try:
            # 讀進記憶體再開：不佔住檔案，PowerPoint 重新匯出或 Drive 同步才能覆蓋
            data = self.path.read_bytes()
            self.content_hash = hashlib.sha1(data).hexdigest()  # 圖片辨識快取用
            with PDF_LOCK:
                self._doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:  # PyMuPDF 對壞檔拋多種例外
            raise DeckError(f"無法開啟簡報 {self.path.name}：{e}") from e
        if not self._doc.is_pdf:
            raise DeckError(f"不是 PDF：{self.path.name}")
        self.page_count: int = self._doc.page_count
        self._png_cache: dict[tuple, bytes] = {}  # (頁, 寬[, 螢光字…]) → PNG
        self._cache_lock = threading.Lock()  # 只保護快取；等子行程轉圖時不佔 PDF_LOCK
        # PyMuPDF 不支援多執行緒同時操作同一份文件；伺服器的執行緒池會並行要圖
        self._lock = PDF_LOCK

    def _page(self, index: int) -> fitz.Page:
        if not 0 <= index < self.page_count:
            raise IndexError(f"頁碼超出範圍：{index}")
        return self._doc[index]

    def render_png(self, index: int, width: int = 1920, phrases: tuple[str, ...] = ()) -> bytes:
        if not 0 <= index < self.page_count:
            raise IndexError(f"頁碼超出範圍：{index}")
        phrases = tuple(phrases)
        key = (index, width) + phrases
        with self._cache_lock:
            hit = self._png_cache.get(key)
        if hit is not None:
            return hit
        png = (_RENDERER.render(str(self.path), self.content_hash, index, width, phrases)
               if _RENDERER else None)
        if png is None:  # 沒有子行程、子行程壞了、或檔案已被覆蓋：在本行程轉開啟時的內容
            with self._lock:
                png = _render_page(self._page(index), width, phrases)
        with self._cache_lock:
            return self._png_cache.setdefault(key, png)

    def page_text(self, index: int) -> str:
        with self._lock:
            return self._page(index).get_text()

    def image_count(self, index: int) -> int:
        with self._lock:
            return len(self._page(index).get_images())

    def is_scanned(self) -> bool:
        total = sum(len(self.page_text(i).strip()) for i in range(self.page_count))
        return total < SCANNED_MAX_CHARS


def list_decks(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)

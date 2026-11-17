"""投影片轉圖子行程。

PyMuPDF 轉圖時不放開 GIL：在主行程一次轉整份簡報（投影幕開新簡報會預載每一頁），
音效卡的回呼搶不到執行權，助理說話就破音（實測 65 頁 4.8 秒、聲音斷 25 次）。
轉圖搬到獨立行程後，主行程只是等結果，語音不受影響。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

RENDER_TIMEOUT = 60  # 秒；超過就改在主行程轉
MAX_DOCS = 4         # 子行程同時保留幾份開著的簡報

_docs: "OrderedDict[str, object]" = OrderedDict()  # 子行程內：content_hash → 開著的文件


def _render_in_child(path: str, content_hash: str, index: int, width: int,
                     phrases: tuple = ()) -> bytes | None:
    import pymupdf as fitz

    from .deck import _render_page

    doc = _docs.get(content_hash)
    if doc is None:
        data = Path(path).read_bytes()
        if hashlib.sha1(data).hexdigest() != content_hash:
            return None  # 檔案已被覆蓋：內容和主行程開啟的不同
        doc = fitz.open(stream=data, filetype="pdf")
        _docs[content_hash] = doc
        while len(_docs) > MAX_DOCS:
            _docs.popitem(last=False)[1].close()
    _docs.move_to_end(content_hash)
    return _render_page(doc[index], width, tuple(phrases))


def _init_child() -> None:
    """Windows 按 Ctrl+C 會送到整個主控台；子程式忽略，由主程式關閉（不然各印一長串錯誤）。"""
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _noop() -> None:
    import pymupdf  # noqa: F401  先載入，第一次轉圖不用等


class ProcessRenderer:
    def __init__(self, workers: int = 2):
        self.workers = workers
        self._pool: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()

    def _get_pool(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(**self._pool_kwargs())
            return self._pool

    def _pool_kwargs(self) -> dict:
        return {"max_workers": self.workers, "initializer": _init_child}

    def _reset(self, pool: ProcessPoolExecutor) -> None:
        with self._lock:
            if self._pool is pool:
                self._pool = None
        pool.shutdown(wait=False, cancel_futures=True)

    def warm(self) -> None:
        """開機時先把子行程叫起來，第一次播放簡報不用等它啟動。"""
        try:
            self._get_pool().submit(_noop)
        except Exception:
            pass

    def render(self, path: str, content_hash: str, index: int, width: int,
               phrases: tuple = ()) -> bytes | None:
        """回傳 PNG；子行程失敗、逾時或檔案已變就回 None（呼叫端改在主行程轉）。"""
        pool = self._get_pool()
        try:
            return pool.submit(_render_in_child, path, content_hash, index, width,
                               tuple(phrases)).result(timeout=RENDER_TIMEOUT)
        except BrokenProcessPool:
            self._reset(pool)
            return None
        except Exception:
            return None

    def run(self, fn, *args, timeout: float = 120):
        """在子行程執行任意（可 pickle 的）函式，例如排版摘要簡報；失敗就在本行程執行。"""
        pool = self._get_pool()
        try:
            return pool.submit(fn, *args).result(timeout=timeout)
        except BrokenProcessPool:
            self._reset(pool)
        except Exception:
            pass
        return fn(*args)

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

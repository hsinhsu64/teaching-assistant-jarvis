"""「參考資料\\」索引：逐頁文字、掃描檔判斷、模糊找檔。

pptx／ppt 比照「簡報\\」先轉成 PDF 再索引（convert 由呼叫端注入，測試用假函式）。
"""

from __future__ import annotations

import difflib
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .converter import Converter, convert_pending
from .deck import Deck, DeckError

FUZZY_MIN = 0.6


def rank_names(query: str, names: Iterable[str]) -> list[str]:
    """依名稱比對（參考資料與簡報共用）：完全相符優先；再來不分大小寫子字串／所有字詞都出現；
    最後 difflib 相似度 ≥ FUZZY_MIN。回傳依分數排序的名稱。"""
    q = query.strip().lower()
    if not q:
        return []
    q_tokens = q.split()
    scored: list[tuple[float, str]] = []
    for original in names:
        name = original.lower()
        if name == q:
            score = 3.0
        elif q in name or all(t in name for t in q_tokens):
            score = 2.0
        else:
            ratio = max(
                difflib.SequenceMatcher(None, q, name).ratio(),
                difflib.SequenceMatcher(None, " ".join(sorted(q_tokens)), " ".join(sorted(name.split()))).ratio(),
            )
            if ratio < FUZZY_MIN:
                continue
            score = ratio
        scored.append((score, original))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored]


@dataclass
class RefDoc:
    name: str
    path: Path
    pages: list[str]
    scanned: bool


class Library:
    def __init__(self, folder: Path, convert: Converter | None = None):
        self.folder = Path(folder)
        self.convert = convert
        self.docs: dict[str, RefDoc] = {}
        self._reported: set = set()
        self._cache: dict[Path, tuple[tuple[int, int], RefDoc]] = {}  # 檔案沒變就不重新抽字

    def refresh(self) -> list[str]:
        """重新掃描資料夾，回傳給主持人看的警告。"""
        warnings: list[str] = []
        if self.convert is not None:
            report = convert_pending(self.folder, self.convert, self._reported)
            warnings += [f"{name}：轉檔失敗（{err}）" for name, err in report.failed]
        docs: dict[str, RefDoc] = {}
        if self.folder.is_dir():
            for path in sorted(self.folder.glob("*.pdf")):
                if path.name.startswith("~"):
                    continue  # 轉檔暫存檔
                try:
                    stat = path.stat()
                    key = (stat.st_mtime_ns, stat.st_size)
                    cached = self._cache.get(path)
                    if cached and cached[0] == key:
                        doc = cached[1]
                    else:
                        doc = self._index(path)
                        self._cache[path] = (key, doc)
                except (DeckError, OSError):
                    warnings.append(f"{path.stem}：無法開啟")
                    continue
                if doc.scanned:
                    warnings.append(f"{path.stem}：掃描檔無法讀取")
                docs[doc.name] = doc
        self.docs = docs
        return warnings

    @staticmethod
    def _index(path: Path) -> RefDoc:
        deck = Deck(path)
        pages = [deck.page_text(i) for i in range(deck.page_count)]
        return RefDoc(path.stem, path, pages, deck.is_scanned())

    def find(self, query: str) -> list[RefDoc]:
        return [self.docs[n] for n in rank_names(query, self.docs)]

    def total_chars(self, names: list[str]) -> int:
        return sum(len(p) for n in names if n in self.docs for p in self.docs[n].pages)

    def add_file(self, src: Path) -> RefDoc:
        """複製進資料夾（同名則加「 (2)」…）並索引；pptx 會先轉 PDF。"""
        src = Path(src)
        self.folder.mkdir(parents=True, exist_ok=True)
        stem, suffix = src.stem, src.suffix.lower()
        dst = self.folder / f"{stem}{suffix}"
        n = 2
        while dst.exists() or dst.with_suffix(".pdf").exists():
            dst = self.folder / f"{stem} ({n}){suffix}"
            n += 1
        shutil.copy2(src, dst)
        if suffix != ".pdf":
            if self.convert is None:
                raise DeckError(f"這台電腦無法轉檔：{src.name}")
            convert_pending(self.folder, self.convert, self._reported)
            dst = dst.with_suffix(".pdf")
        doc = self._index(dst)
        self.docs[doc.name] = doc
        return doc

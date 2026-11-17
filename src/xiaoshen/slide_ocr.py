"""投影片圖片辨識：以圖片為主的投影片（例如檢驗報告截圖）請 Gemini 逐字抄錄，併入該頁文字。

Gemini 看圖抄錄、開簡報後在背景全部抄。只抄錄、不解讀；回答仍要通過出處驗證，
引文落在抄錄部分時出處標「（圖片辨識）」。抄錄結果依檔案內容快取在本機，不進 Drive。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .deck import Deck

OCR_MARK = "[圖片辨識]"
MAX_TEXT = 200  # 文字少於這麼多、又有圖片的頁才抄
WORKERS = 4

TRANSCRIBE_PROMPT = (
    "逐字抄錄這張投影片圖片中所有看得到的文字與數字（含表格，每列一行，欄位之間用兩個空格分隔）。"
    "不要解釋、不要翻譯、不要補字或推測；看不清楚的字寫 [?]。只輸出抄錄內容。"
)

Transcriber = Callable[[bytes], str]


def image_heavy_pages(deck: Deck) -> list[int]:
    return [i for i in range(deck.page_count)
            if deck.image_count(i) > 0 and len(deck.page_text(i).strip()) < MAX_TEXT]


class SlideReader:
    def __init__(self, state, transcriber: Transcriber, cache_dir: Path):
        self.state = state
        self.transcriber = transcriber
        self.cache_dir = Path(cache_dir)

    def _cache_path(self, deck: Deck) -> Path:
        return self.cache_dir / f"{deck.content_hash}.json"

    def run_once(self) -> None:
        """抄錄目前簡報中所有以圖片為主的頁；簡報中途換了就停止並丟棄結果。"""
        st = self.state
        deck, version = st.deck, st.deck_version
        if deck is None:
            return
        pages = image_heavy_pages(deck)
        path = self._cache_path(deck)
        try:
            cache: dict[str, str] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            cache = {}
        done = {i: cache[str(i)] for i in pages if str(i) in cache}
        failed = 0

        def publish() -> bool:
            with st.lock:
                if st.deck_version != version:
                    return False
                st.page_ocr = dict(done)
                st.ocr_progress = {"done": len(done), "total": len(pages), **({"failed": failed} if failed else {})}
            st._changed()
            return True

        if not publish():
            return
        todo = [i for i in pages if i not in done]

        def work(i: int) -> str:
            return self.transcriber(deck.render_png(i, width=1600)).strip()

        # 一頁約 12 秒；同時抄 WORKERS 頁，24 頁約 1 分多鐘
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(work, i): i for i in todo}
            for fut in as_completed(futures):
                i = futures[fut]
                if st.deck_version != version:
                    for f in futures:
                        f.cancel()
                    return
                try:
                    text = fut.result()
                except Exception:
                    failed += 1
                    if not publish():
                        return
                    continue
                self._record(i, text, done, cache, path)
                if not publish():
                    return

    def _record(self, i: int, text: str, done: dict, cache: dict, path: Path) -> None:
        done[i] = text
        cache[str(i)] = text
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 快取寫不進去不影響本場使用


def gemini_transcriber(model: str) -> Transcriber:
    """用 Gemini 看圖抄錄（金鑰讀環境變數 GEMINI_API_KEY）。"""
    import os

    def transcribe(png: bytes) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=60_000))
        resp = client.models.generate_content(
            model=model, contents=[types.Part.from_bytes(data=png, mime_type="image/png"), TRANSCRIBE_PROMPT])
        return resp.text or ""

    return transcribe

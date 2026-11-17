"""翻頁與語音工具的事件記錄（使用者同意加；用來查「一直往下翻」「冒出英文」這類實機問題）。

一行一筆 JSON，存在本機 %LOCALAPPDATA%\\xiaoshen\\logs\\events-日期.jsonl，不進 Drive。
只記事件種類、工具名稱、狀態與頁碼，不記問題內容（可能含病人資訊）。沒設定資料夾時不記。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

_dir: Path | None = None
_lock = threading.Lock()


def configure(folder) -> None:
    global _dir
    _dir = Path(folder) if folder else None


def log(event: str, **fields) -> None:
    if _dir is None:
        return
    now = datetime.now()
    row = {"ts": now.isoformat(timespec="milliseconds"), "event": event, **fields}
    try:
        with _lock:
            _dir.mkdir(parents=True, exist_ok=True)
            with (_dir / f"events-{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 記錄失敗不影響晨會

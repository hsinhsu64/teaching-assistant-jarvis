"""保留與刪除：會議記錄做好 recording_retention_days（預設 30）天後，刪掉那一場的錄音、逐字稿（含去識別候選）、
筆電本機的暫存錄音、代號對照表與送出內容的稽核記錄。會議記錄本身保留；還沒整理成會議記錄的不自動刪。

雲端硬碟裡刪掉的檔案會先進 Google 雲端硬碟的垃圾桶（依 Google 規定約 30 天後才永久刪除）。
事件紀錄只記場次與檔名，不記內容。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from . import eventlog
from . import meeting_store as ms
from .config import Config

CHECK_SECONDS = 24 * 3600


def cleanup(config: Config, now: datetime, private_dir: Path | None = None,
            recordings: Path | None = None) -> list[str]:
    minutes = ms.folder(config, ms.MINUTES_DIR)
    if not minutes.is_dir():
        return []
    limit = timedelta(days=config.recording_retention_days)
    removed: list[str] = []
    for docx in sorted(minutes.glob("*.docx")):
        mid = docx.stem
        if not ms.valid_id(mid):
            continue
        made = ms.minutes_generated_at(config, mid)
        if made is None or now - made <= limit:
            continue
        files = ms.delete_files(config, mid, private_dir=private_dir, recordings=recordings)
        if files:
            eventlog.log("retention", id=mid, files=files)
            removed += files
    return removed


async def watch(config: Config, interval: float = CHECK_SECONDS) -> None:
    """啟動時檢查一次，之後每天一次。任何錯誤（雲端硬碟暫時離線）只記事件、下一輪再試。"""
    import asyncio

    while True:
        try:
            await asyncio.to_thread(cleanup, config, datetime.now())
        except Exception as e:
            eventlog.log("retention", action="failed", error=e.__class__.__name__)
        await asyncio.sleep(interval)

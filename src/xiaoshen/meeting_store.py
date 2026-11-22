"""會議資料夾：每一場晨會錄音的檔案與狀態。

筆電與轉寫工作站只透過「紀錄」底下的三個子資料夾溝通（雲端硬碟同步）：
  紀錄\\錄音\\<id>.flac ＋ <id>.ready（音檔完整寫好才有 .ready）
  紀錄\\逐字稿\\<id>.json ＋ <id>.txt；轉寫中有 <id>.working；失敗寫 <id>.error.json
  紀錄\\會議記錄\\<id>.docx ＋ <id>.md
<id> = YYYY-MM-DD_HHMM（同一分鐘第二場起加 _2、_3…）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .config import Config
from .platform_support import app_data_dir

AUDIO_DIR, TRANSCRIPT_DIR, MINUTES_DIR = "錄音", "逐字稿", "會議記錄"
WORKING_STALE_MINUTES = 15  # 轉寫中每分鐘更新 .working；超過這麼久沒更新＝轉到一半當掉了
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}(?:_\d+)?$")


def private_dir() -> Path:
    """只在筆電本機的資料（代號對照表、送出內容的稽核記錄、填過的名字）：不進雲端硬碟。"""
    return app_data_dir() / "minutes_private"


def _questions_file(private: Path, mid: str) -> Path:
    return Path(private) / f"{mid}.questions.json"


def load_questions(private: Path, mid: str) -> list[dict]:
    """錄音期間學員手機「詢問老師」的提問：[{t: 錄音第幾秒, text}]。"""
    try:
        data = json.loads(_questions_file(private, mid).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    return [{"t": float(q["t"]), "text": str(q["text"])} for q in data if isinstance(q, dict) and "text" in q]


def add_question(private: Path, mid: str, t: float, text: str) -> None:
    """存在筆電本機（學員打的字可能有病人資料，不進雲端硬碟）；和錄音一起在保留期到時刪除。"""
    f = _questions_file(private, mid)
    f.parent.mkdir(parents=True, exist_ok=True)
    data = load_questions(private, mid) + [{"t": round(float(t), 1), "text": text}]
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)


_default_private_dir = private_dir


def recordings_dir() -> Path:
    """錄音中的本機暫存。"""
    return app_data_dir() / "recordings"


def new_id(now: datetime, exists: Callable[[str], bool] = lambda mid: False) -> str:
    base = now.strftime("%Y-%m-%d_%H%M")
    mid, n = base, 1
    while exists(mid):
        n += 1
        mid = f"{base}_{n}"
    return mid


def valid_id(mid: str) -> bool:
    return bool(ID_RE.match(mid or ""))


@dataclass(frozen=True)
class MeetingPaths:
    audio: Path
    ready: Path
    transcript_json: Path
    transcript_txt: Path
    error: Path
    working: Path
    minutes_at: Path   # 會議記錄生成的時間（30 天保留從這裡算；改 Word 檔不會重新起算）
    minutes_docx: Path
    minutes_md: Path


def folder(config: Config, name: str) -> Path:
    return config.folder("紀錄") / name


def paths(config: Config, mid: str) -> MeetingPaths:
    if not valid_id(mid):
        raise ValueError(f"不是場次代號：{mid}")
    a, t, m = folder(config, AUDIO_DIR), folder(config, TRANSCRIPT_DIR), folder(config, MINUTES_DIR)
    return MeetingPaths(audio=a / f"{mid}.flac", ready=a / f"{mid}.ready",
                        transcript_json=t / f"{mid}.json", transcript_txt=t / f"{mid}.txt",
                        error=t / f"{mid}.error.json", working=t / f"{mid}.working",
                        minutes_at=t / f"{mid}.minutes_at",
                        minutes_docx=m / f"{mid}.docx", minutes_md=m / f"{mid}.md")


def status(config: Config, mid: str, now: datetime) -> str | None:
    """None（音檔還沒完整送到）／waiting／overdue／transcribing／transcribed／minutes_done／error。"""
    p = paths(config, mid)
    if p.minutes_docx.exists():
        return "minutes_done"
    if p.transcript_json.exists():
        return "transcribed"
    if p.error.exists():
        return "error"
    if not p.ready.exists():
        return None
    try:
        if now - datetime.fromtimestamp(p.working.stat().st_mtime) < timedelta(minutes=WORKING_STALE_MINUTES):
            return "transcribing"
    except FileNotFoundError:
        pass
    sent = datetime.fromtimestamp(p.ready.stat().st_mtime)
    if now - sent > timedelta(minutes=config.transcribe_fallback_minutes):
        return "overdue"
    return "waiting"


def mark_minutes_generated(config: Config, mid: str, when: datetime) -> None:
    p = paths(config, mid)
    p.minutes_at.parent.mkdir(parents=True, exist_ok=True)
    p.minutes_at.write_text(when.isoformat(timespec="seconds"), encoding="utf-8")


def delete_minutes(config: Config, mid: str) -> list[str]:
    """只刪會議記錄（Word、.md、生成時間標記）；錄音與逐字稿留著，可以重新生成（使用者 2026-11-23）。"""
    p = paths(config, mid)
    removed = []
    for f in (p.minutes_docx, p.minutes_md, p.minutes_at):
        try:
            f.unlink()
            removed.append(f.name)
        except FileNotFoundError:
            pass
    return removed


def minutes_generated_at(config: Config, mid: str) -> datetime | None:
    """會議記錄生成的時間；沒有標記（舊資料）時用 .docx 的修改時間。"""
    p = paths(config, mid)
    try:
        return datetime.fromisoformat(p.minutes_at.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    try:
        return datetime.fromtimestamp(p.minutes_docx.stat().st_mtime)
    except OSError:
        return None


def ready_size(p: MeetingPaths) -> int | None:
    """.ready 裡記的音檔大小（舊格式或讀不到回 None）。"""
    try:
        data = json.loads(p.ready.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    size = data.get("size") if isinstance(data, dict) else None
    return size if isinstance(size, int) else None


def audio_complete(p: MeetingPaths) -> bool:
    """音檔在、而且大小和 .ready 記的一樣（雲端硬碟同步完整）。"""
    try:
        actual = p.audio.stat().st_size
    except OSError:
        return False
    expected = ready_size(p)
    return actual > 0 and (expected is None or actual == expected)


def error_info(config: Config, mid: str) -> dict | None:
    try:
        data = json.loads(paths(config, mid).error.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ids_in(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    out = set()
    for f in directory.iterdir():
        mid = f.name.split(".", 1)[0]
        if valid_id(mid):
            out.add(mid)
    return out


def list_meetings(config: Config, now: datetime) -> list[dict]:
    """所有場次（新到舊）：{id, status, created, has_minutes}。音檔還沒完整送到的不列。"""
    ids = set()
    for name in (AUDIO_DIR, TRANSCRIPT_DIR, MINUTES_DIR):
        ids |= _ids_in(folder(config, name))
    rows = []
    for mid in sorted(ids, reverse=True):
        st = status(config, mid, now)
        if st is None:
            continue
        p = paths(config, mid)
        stamp = next((f.stat().st_mtime for f in (p.ready, p.minutes_docx, p.transcript_json) if f.exists()), None)
        rows.append({"id": mid, "status": st,
                     "created": datetime.fromtimestamp(stamp).isoformat(timespec="minutes") if stamp else "",
                     "has_minutes": st == "minutes_done"})
    return rows


def delete_files(config: Config, mid: str, private_dir: Path | None = None,
                 recordings: Path | None = None) -> list[str]:
    """刪掉一場的錄音、逐字稿與本機私有資料；會議記錄本身不刪。回傳刪掉的檔名。"""
    p = paths(config, mid)
    targets = [p.audio, p.ready, p.transcript_json, p.transcript_txt, p.error, p.working, p.minutes_at,
               p.audio.with_name(p.audio.name + ".partial"),
               p.transcript_json.with_name(p.transcript_json.name + ".tmp"),
               p.transcript_txt.with_name(p.transcript_txt.name + ".tmp")]
    for d in (private_dir if private_dir is not None else _default_private_dir(),
              recordings if recordings is not None else recordings_dir()):
        if d.is_dir():
            targets += [f for f in d.iterdir() if f.name.split(".", 1)[0] == mid]
    removed = []
    for f in targets:
        try:
            f.unlink()
            removed.append(f.name)
        except FileNotFoundError:
            pass
    return removed

"""轉寫工作站的工作佇列（家裡有顯卡的電腦，以及筆電的「在這台電腦轉寫」共用）。

只處理：有 .ready、音檔在、還沒有逐字稿也沒有失敗紀錄、而且音檔大小連續兩輪一樣（雲端硬碟同步完了）的錄音。
模型由 engines 提供（transcribe／diarize／person_names），這裡只管檔案與狀態。
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path
from typing import Protocol

from . import meeting_store as ms
from .config import Config
from .transcript_build import build_transcript, transcript_text


def single_instance(lock_path: Path):
    """同一台電腦只跑一個工作站：拿得到鎖回傳開著的檔案（程式結束或當掉，作業系統自動放掉），拿不到回 None。"""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


class Engines(Protocol):
    models: dict

    def transcribe(self, audio: Path) -> list[dict]: ...           # [{start, end, text}]

    def diarize(self, audio: Path) -> tuple[list[dict], dict]: ...  # ([{start, end, speaker}], {speaker: 聲紋})

    def person_names(self, text: str) -> list[str]: ...


def pending(config: Config, sizes_seen: dict[str, int]) -> list[str]:
    """這一輪可以轉寫的場次（舊到新）。sizes_seen 由呼叫端保留，跨輪比較音檔大小。"""
    folder = ms.folder(config, ms.AUDIO_DIR)
    if not folder.is_dir():
        return []
    out = []
    for ready in sorted(folder.glob("*.ready")):
        mid = ready.stem
        if not ms.valid_id(mid):
            continue
        p = ms.paths(config, mid)
        if p.transcript_json.exists() or p.error.exists():
            sizes_seen.pop(mid, None)
            continue
        try:
            size = p.audio.stat().st_size
        except OSError:
            continue  # .ready 比音檔早同步到
        if size > 0 and sizes_seen.get(mid) == size and ms.audio_complete(p):
            out.append(mid)
        sizes_seen[mid] = size
    return out


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


HEARTBEAT_SECONDS = 60


def _heartbeat(path: Path, stop: threading.Event, interval: float) -> None:
    """轉寫中定期更新 .working 的時間：筆電據此分辨「還在轉」和「轉到一半當掉」。"""
    while not stop.wait(interval):
        try:
            os.utime(path)
        except OSError:
            pass


def process(config: Config, mid: str, engines: Engines, heartbeat: float = HEARTBEAT_SECONDS) -> Path:
    """轉一場：成功回逐字稿 json 的路徑；任何錯誤寫 <id>.error.json 並回它的路徑。"""
    p = ms.paths(config, mid)
    p.transcript_json.parent.mkdir(parents=True, exist_ok=True)
    p.working.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(p.working, stop, heartbeat), daemon=True).start()
    timing: dict[str, float] = {}
    t_all = time.monotonic()
    try:
        t = time.monotonic()
        asr = engines.transcribe(p.audio)
        timing["asr"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        diar, embeddings = engines.diarize(p.audio)
        timing["diarization"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        names = engines.person_names("\n".join(s["text"] for s in asr))
        timing["names"] = round(time.monotonic() - t, 1)
        timing["total"] = round(time.monotonic() - t_all, 1)
        doc = build_transcript(mid, asr, diar, embeddings, names, dict(engines.models), timing)
        _write_atomic(p.transcript_txt, transcript_text(doc))
        _write_atomic(p.transcript_json, json.dumps(doc, ensure_ascii=False, indent=1))  # 最後寫：有它就算完成
        return p.transcript_json
    except Exception as e:
        detail = "".join(traceback.format_exception(e)[-6:])
        _write_atomic(p.error, json.dumps({"error": str(e) or e.__class__.__name__, "detail": detail},
                                          ensure_ascii=False, indent=1))
        return p.error
    finally:
        stop.set()
        p.working.unlink(missing_ok=True)

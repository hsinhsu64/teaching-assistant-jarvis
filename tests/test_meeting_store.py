"""會議資料夾狀態：錄音 → 逐字稿 → 會議記錄（紀錄\錄音、紀錄\逐字稿、紀錄\會議記錄）。"""

import os
import time
from datetime import datetime, timedelta

from xiaoshen import meeting_store as ms
from xiaoshen.config import Config


def _touch(path, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_config_defaults():
    c = Config()
    assert c.recording_max_minutes == 180
    assert c.recording_retention_days == 30
    assert c.transcribe_fallback_minutes == 30
    assert c.minutes_model == ""
    assert c.worker_whisper_model == "large-v3"
    assert c.worker_cluster_threshold == 0.7


def test_new_id_unique_within_minute():
    now = datetime(2026, 11, 22, 7, 30, 15)
    assert ms.new_id(now) == "2026-11-22_0730"
    taken = {"2026-11-22_0730", "2026-11-22_0730_2"}
    assert ms.new_id(now, exists=lambda mid: mid in taken) == "2026-11-22_0730_3"


def test_paths_layout(config):
    p = ms.paths(config, "2026-11-22_0730")
    root = config.folder("紀錄")
    assert p.audio == root / "錄音" / "2026-11-22_0730.flac"
    assert p.ready == root / "錄音" / "2026-11-22_0730.ready"
    assert p.transcript_json == root / "逐字稿" / "2026-11-22_0730.json"
    assert p.transcript_txt == root / "逐字稿" / "2026-11-22_0730.txt"
    assert p.error == root / "逐字稿" / "2026-11-22_0730.error.json"
    assert p.minutes_docx == root / "會議記錄" / "2026-11-22_0730.docx"
    assert p.minutes_md == root / "會議記錄" / "2026-11-22_0730.md"


def test_status_progression(config):
    mid = "2026-11-22_0730"
    p = ms.paths(config, mid)
    now = datetime.now()
    assert ms.status(config, mid, now) is None  # 什麼都沒有
    _touch(p.audio)
    assert ms.status(config, mid, now) is None  # 還沒有 .ready：同步中，不算數
    _touch(p.ready)
    assert ms.status(config, mid, now) == "waiting"
    _touch(p.working)
    assert ms.status(config, mid, now) == "transcribing"
    _touch(p.transcript_json)
    assert ms.status(config, mid, now) == "transcribed"
    _touch(p.minutes_docx)
    assert ms.status(config, mid, now) == "minutes_done"


def test_overdue_after_fallback_minutes(config):
    mid = "2026-11-22_0730"
    p = ms.paths(config, mid)
    now = datetime(2026, 11, 22, 9, 0)
    _touch(p.audio)
    _touch(p.ready, (now - timedelta(minutes=29)).timestamp())
    assert ms.status(config, mid, now) == "waiting"
    _touch(p.ready, (now - timedelta(minutes=31)).timestamp())
    assert ms.status(config, mid, now) == "overdue"


def test_error_status(config):
    mid = "2026-11-22_0730"
    p = ms.paths(config, mid)
    _touch(p.audio)
    _touch(p.ready)
    p.error.parent.mkdir(parents=True, exist_ok=True)
    p.error.write_text('{"error": "壞檔", "detail": ""}', encoding="utf-8")
    assert ms.status(config, mid, datetime.now()) == "error"
    assert ms.error_info(config, mid) == {"error": "壞檔", "detail": ""}


def test_list_newest_first(config):
    for mid in ("2026-11-20_0730", "2026-11-22_0730", "2026-11-21_0730"):
        p = ms.paths(config, mid)
        _touch(p.audio)
        _touch(p.ready)
    _touch(ms.paths(config, "2026-11-21_0730").transcript_json)
    _touch(ms.paths(config, "2026-11-20_0730").transcript_json)
    _touch(ms.paths(config, "2026-11-20_0730").minutes_docx)
    rows = ms.list_meetings(config, datetime.now())
    assert [r["id"] for r in rows] == ["2026-11-22_0730", "2026-11-21_0730", "2026-11-20_0730"]
    assert [r["status"] for r in rows] == ["waiting", "transcribed", "minutes_done"]
    assert [r["has_minutes"] for r in rows] == [False, False, True]
    assert rows[0]["created"]


def test_list_includes_minutes_without_audio(config):
    """30 天後錄音與逐字稿刪了，會議記錄還在：清單照樣列出。"""
    _touch(ms.paths(config, "2026-10-01_0730").minutes_docx)
    rows = ms.list_meetings(config, datetime.now())
    assert [(r["id"], r["status"]) for r in rows] == [("2026-10-01_0730", "minutes_done")]


def test_list_without_folders(tmp_path):
    c = Config(data_root=tmp_path)
    (tmp_path / "紀錄").mkdir()
    assert ms.list_meetings(c, datetime.now()) == []


def test_delete_files_keeps_minutes(config, tmp_path):
    mid = "2026-11-22_0730"
    p = ms.paths(config, mid)
    for f in (p.audio, p.ready, p.transcript_json, p.transcript_txt, p.error, p.working, p.minutes_docx, p.minutes_md):
        _touch(f)
    private = tmp_path / "private"
    _touch(private / f"{mid}.map.json")
    _touch(private / f"{mid}.sent.txt")
    _touch(private / "2026-11-22_0731.map.json")  # 別的場次
    _touch(private / "known_names.json")
    removed = ms.delete_files(config, mid, private_dir=private)
    assert p.minutes_docx.exists() and p.minutes_md.exists()
    assert not any(f.exists() for f in (p.audio, p.ready, p.transcript_json, p.transcript_txt, p.error, p.working))
    assert not (private / f"{mid}.map.json").exists() and not (private / f"{mid}.sent.txt").exists()
    assert (private / "2026-11-22_0731.map.json").exists() and (private / "known_names.json").exists()
    assert f"{mid}.flac" in removed and f"{mid}.map.json" in removed


def test_stale_working_marker_is_ignored(config):
    """審查：家裡電腦轉到一半斷電，.working 留在雲端硬碟；太久沒更新就不算轉寫中（才會出現退路與刪除）。"""
    mid = "2026-11-22_0730"
    p = ms.paths(config, mid)
    now = datetime(2026, 11, 22, 9, 0)
    _touch(p.audio)
    _touch(p.ready, (now - timedelta(minutes=60)).timestamp())
    _touch(p.working, (now - timedelta(minutes=ms.WORKING_STALE_MINUTES - 1)).timestamp())
    assert ms.status(config, mid, now) == "transcribing"
    _touch(p.working, (now - timedelta(minutes=ms.WORKING_STALE_MINUTES + 1)).timestamp())
    assert ms.status(config, mid, now) == "overdue"


def test_phone_questions_saved_per_meeting(tmp_path):
    """使用者（2026-11-23）：錄音期間學員手機打字的提問列入那一場會議記錄；存在筆電本機。"""
    private = tmp_path / "private"
    assert ms.load_questions(private, "2026-11-22_0730") == []
    ms.add_question(private, "2026-11-22_0730", 65.2, "SRNS 第二線用什麼？")
    ms.add_question(private, "2026-11-22_0730", 130.0, "病歷號 7654321 那位呢？")
    ms.add_question(private, "2026-11-22_0731", 5.0, "別場")
    assert ms.load_questions(private, "2026-11-22_0730") == [
        {"t": 65.2, "text": "SRNS 第二線用什麼？"}, {"t": 130.0, "text": "病歷號 7654321 那位呢？"}]


def test_delete_files_removes_phone_questions(config, tmp_path):
    private = tmp_path / "private"
    ms.add_question(private, "2026-11-22_0730", 1.0, "問題")
    ms.delete_files(config, "2026-11-22_0730", private_dir=private, recordings=tmp_path / "rec")
    assert ms.load_questions(private, "2026-11-22_0730") == []

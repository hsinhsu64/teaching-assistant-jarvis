"""30 天保留：會議記錄做好 30 天後刪錄音、逐字稿與本機私有資料；會議記錄本身與未整理的場次不刪。"""

import os
from datetime import datetime, timedelta

from xiaoshen import meeting_store as ms
from xiaoshen.retention import cleanup

NOW = datetime(2026, 12, 31, 8, 0)


def make(config, mid, private, minutes_days_ago=None, audio_days_ago=40):
    p = ms.paths(config, mid)
    for f in (p.audio, p.ready, p.transcript_json, p.transcript_txt):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        t = (NOW - timedelta(days=audio_days_ago)).timestamp()
        os.utime(f, (t, t))
    private.mkdir(parents=True, exist_ok=True)
    (private / f"{mid}.map.json").write_text("{}")
    (private / f"{mid}.sent.txt").write_text("x")
    if minutes_days_ago is not None:
        for f in (p.minutes_docx, p.minutes_md):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
            t = (NOW - timedelta(days=minutes_days_ago)).timestamp()
            os.utime(f, (t, t))
    return p


def test_deletes_after_retention(config, tmp_path):
    private = tmp_path / "private"
    p = make(config, "2026-11-20_0730", private, minutes_days_ago=31)
    removed = cleanup(config, NOW, private_dir=private, recordings=tmp_path / "rec")
    assert not p.audio.exists() and not p.ready.exists()
    assert not p.transcript_json.exists() and not p.transcript_txt.exists()
    assert not (private / "2026-11-20_0730.map.json").exists() and not (private / "2026-11-20_0730.sent.txt").exists()
    assert sorted(removed) == sorted(["2026-11-20_0730.flac", "2026-11-20_0730.ready", "2026-11-20_0730.json",
                                      "2026-11-20_0730.txt", "2026-11-20_0730.map.json", "2026-11-20_0730.sent.txt"])


def test_keeps_before_retention(config, tmp_path):
    private = tmp_path / "private"
    p = make(config, "2026-12-05_0730", private, minutes_days_ago=29)
    assert cleanup(config, NOW, private_dir=private, recordings=tmp_path / "rec") == []
    assert p.audio.exists() and p.transcript_json.exists() and (private / "2026-12-05_0730.map.json").exists()


def test_keeps_unfinished_meetings(config, tmp_path):
    private = tmp_path / "private"
    p = make(config, "2026-10-01_0730", private, minutes_days_ago=None, audio_days_ago=90)
    assert cleanup(config, NOW, private_dir=private, recordings=tmp_path / "rec") == []
    assert p.audio.exists() and p.transcript_json.exists()


def test_never_deletes_minutes(config, tmp_path):
    private = tmp_path / "private"
    p = make(config, "2026-10-01_0730", private, minutes_days_ago=60)
    cleanup(config, NOW, private_dir=private, recordings=tmp_path / "rec")
    assert p.minutes_docx.exists() and p.minutes_md.exists()


def test_retention_days_from_config(config, tmp_path):
    private = tmp_path / "private"
    config.recording_retention_days = 7
    p = make(config, "2026-12-20_0730", private, minutes_days_ago=8)
    cleanup(config, NOW, private_dir=private, recordings=tmp_path / "rec")
    assert not p.audio.exists()


def test_watch_runs_at_start_and_repeats(config, tmp_path, monkeypatch):
    import asyncio

    from xiaoshen import retention

    calls = []
    monkeypatch.setattr(retention, "cleanup", lambda c, now, **k: calls.append(now) or [])

    async def run():
        task = asyncio.create_task(retention.watch(config, interval=0.02))
        await asyncio.sleep(0.07)
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 2  # 啟動時一次，之後定期


def test_watch_survives_errors(config, monkeypatch):
    import asyncio

    from xiaoshen import retention

    calls = []

    def boom(c, now, **k):
        calls.append(1)
        raise OSError("雲端硬碟離線")

    monkeypatch.setattr(retention, "cleanup", boom)

    async def run():
        task = asyncio.create_task(retention.watch(config, interval=0.02))
        await asyncio.sleep(0.07)
        assert not task.done()
        task.cancel()

    asyncio.run(run())
    assert len(calls) >= 2


def test_editing_minutes_does_not_restart_the_clock(config, tmp_path):
    """審查：主持人打開會議記錄手動改字，不能讓錄音與逐字稿多留 30 天：從「生成的時間」起算。"""
    private = tmp_path / "private"
    p = make(config, "2026-11-20_0730", private, minutes_days_ago=1)   # 昨天在 Word 裡改過
    ms.mark_minutes_generated(config, "2026-11-20_0730", NOW - timedelta(days=31))
    cleanup(config, NOW, private_dir=private, recordings=tmp_path / "rec")
    assert not p.audio.exists() and not p.transcript_json.exists()
    assert p.minutes_docx.exists()
    assert not p.minutes_at.exists()  # 標記也一起刪


def test_generate_marks_time(config, tmp_path):
    from fakes import FakeLLM
    import json as _json
    from xiaoshen.minutes import generate_minutes

    p = ms.paths(config, "2026-11-20_0730")
    p.transcript_json.parent.mkdir(parents=True, exist_ok=True)
    p.transcript_json.write_text(_json.dumps({"lines": [{"start": 0, "end": 1, "text": "好", "speaker": 0}],
                                              "speakers": [{"speaker": 0, "seconds": 1, "samples": []}]}),
                                 encoding="utf-8")
    generate_minutes(config, FakeLLM({"minutes_md": "x", "corrections": []}), "2026-11-20_0730", {}, [], "x",
                     NOW, private_dir=tmp_path / "private")
    assert ms.minutes_generated_at(config, "2026-11-20_0730") == NOW

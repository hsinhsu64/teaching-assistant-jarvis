import json

from xiaoshen import eventlog


def test_unconfigured_is_noop(tmp_path):
    eventlog.configure(None)
    eventlog.log("page", source="kws")  # 不拋例外、不寫檔


def test_writes_json_lines_per_day(tmp_path):
    eventlog.configure(tmp_path)
    try:
        eventlog.log("page", source="kws", before=1, after=2)
    finally:
        eventlog.configure(None)
    files = list(tmp_path.glob("events-*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert row["event"] == "page" and row["source"] == "kws" and row["after"] == 2 and "ts" in row

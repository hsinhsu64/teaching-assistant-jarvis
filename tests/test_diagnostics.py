import json
import re
import zipfile
from datetime import datetime

from fastapi.testclient import TestClient

from xiaoshen.diagnostics import build_bundle

NOW = datetime(2026, 11, 16, 9, 30)


def _logs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("events-2026-11-08.jsonl", "events-2026-11-09.jsonl", "events-2026-11-16.jsonl",
                 "events-bad.jsonl", "other.txt"):
        (logs / name).write_text('{"event": "x"}\n', encoding="utf-8")
    return logs


def test_bundle_redacts_and_limits(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("# 註解\ndata_root = 'D:\\a\\b\\教學助理資料'\napi_key = 'x'\nMy_Token=\"t\"\n"
                   "session_COOKIE = 'c'\ndisplay_name = \"賈維斯\"\n", encoding="utf-8")
    out = tmp_path / "out"
    z = build_bundle(out, [{"name": "麥克風", "level": "ok"}], cfg, _logs(tmp_path), NOW)
    assert z.name == "診斷包-20261116-0930.zip" and z.parent == out
    zf = zipfile.ZipFile(z)
    names = zf.namelist()
    assert "about.json" in names and "selfcheck.json" in names
    # 8 天前排除；9 天前(11-09 = 7 天前)含；今天含；不符格式的略過
    assert sorted(n for n in names if n.startswith("events-")) == ["events-2026-11-09.jsonl", "events-2026-11-16.jsonl"]
    text = zf.read("config.toml").decode()
    assert "教學助理資料" in text and "D:" not in text and "\\" not in text
    assert "api_key" not in text and "Token" not in text and "COOKIE" not in text
    assert "# 註解" in text and "display_name" in text
    about = json.loads(zf.read("about.json"))
    assert about["version"] == "0.1.0" and about["platform"] and about["python"]
    assert json.loads(zf.read("selfcheck.json"))[0]["name"] == "麥克風"


def test_bundle_without_config_or_logs(tmp_path):
    z = build_bundle(tmp_path, [], tmp_path / "nope.toml", tmp_path / "nologs", NOW)
    assert sorted(zipfile.ZipFile(z).namelist()) == ["about.json", "selfcheck.json"]


def test_bundle_never_includes_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-VALUE-123")
    cfg = tmp_path / "config.toml"
    cfg.write_text("data_root = '/x/y'\n", encoding="utf-8")
    z = build_bundle(tmp_path, [], cfg, tmp_path, NOW)
    zf = zipfile.ZipFile(z)
    assert all("SECRET-VALUE-123" not in zf.read(n).decode("utf-8") for n in zf.namelist())


def test_api_diagnostics_writes_zip(tmp_path, state):
    from xiaoshen.hub import Hub
    from xiaoshen.server import create_control_app
    state.checks = [{"name": "麥克風", "level": "ok"}]
    cfg = tmp_path / "config.toml"
    cfg.write_text("data_root = 'D:/x/資料'\n", encoding="utf-8")
    out = tmp_path / "desk"
    app = create_control_app(state, Hub(), config_path=cfg, diagnostics_dir=out, logs_dir=_logs(tmp_path))
    with TestClient(app) as c:
        r = c.post("/api/diagnostics")
    assert r.status_code == 200 and r.json()["ok"] is True
    path = r.json()["path"]
    assert re.search(r"診斷包-\d{8}-\d{4}\.zip$", path)
    assert json.loads(zipfile.ZipFile(path).read("selfcheck.json"))[0]["name"] == "麥克風"


def test_api_diagnostics_error_is_500_chinese(tmp_path, state):
    from xiaoshen.hub import Hub
    from xiaoshen.server import create_control_app
    blocker = tmp_path / "file"
    blocker.write_text("x")
    app = create_control_app(state, Hub(), config_path=tmp_path / "c.toml", diagnostics_dir=blocker / "sub",
                             logs_dir=tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/diagnostics")
    assert r.status_code == 500 and "診斷包" in r.json()["detail"]


def test_host_page_has_diagnostics_button():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "web"
    assert 'id="diagBtn"' in (web / "host.html").read_text(encoding="utf-8")
    assert "/api/diagnostics" in (web / "host.js").read_text(encoding="utf-8")


def test_selfcheck_is_scrubbed(tmp_path):
    root = "D:\\共用資料夾\\someone\\教學助理資料"
    cfg = tmp_path / "config.toml"
    cfg.write_text("data_root = '" + root + "'\n", encoding="utf-8")
    checks = [
        {"name": "資料夾", "level": "error", "detail": "找不到資料根目錄：" + root},
        {"name": "Gemini", "level": "error",
         "detail": "400 https://x.googleapis.com/v1?key=AIzaSyA1234567890abcdefghijKLMN&alt=json failed"},
        {"name": "token", "level": "warn", "detail": "bare AIzaSyB1234567890abcdefghijKLMN here",
         "extra": ["posix " + root.replace("\\", "/")]},
    ]
    z = build_bundle(tmp_path, checks, cfg, tmp_path, NOW)
    text = zipfile.ZipFile(z).read("selfcheck.json").decode("utf-8")
    assert "someone" not in text and "D:" not in text and "共用資料夾" not in text
    assert "教學助理資料" in text
    assert "AIza" not in text and "key=***" in text and "&alt=json" in text


def test_selfcheck_scrubs_data_root_param_without_config(tmp_path):
    z = build_bundle(tmp_path, [{"detail": "D:/secret/name/data"}], tmp_path / "none.toml", tmp_path, NOW,
                     data_root="D:/secret/name/data")
    text = zipfile.ZipFile(z).read("selfcheck.json").decode("utf-8")
    assert "secret" not in text and "data" in text


def test_config_key_rule_keeps_keys_prefix_lines(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("keys_next = 'n'\nkeys_wake = 'w'\nkws_x = 1\napi_key = 'a'\ngemini_key = 'g'\n"
                   "token = 't'\ncookie_file = 'c'\nclient_secret = 's'\nPassword = 'p'\napikey = 'z'\n",
                   encoding="utf-8")
    text = zipfile.ZipFile(build_bundle(tmp_path, [], cfg, tmp_path, NOW)).read("config.toml").decode()
    assert "keys_next" in text and "keys_wake" in text and "kws_x" in text
    for gone in ("api_key", "gemini_key", "token", "cookie_file", "client_secret", "Password", "apikey"):
        assert gone not in text

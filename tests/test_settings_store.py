import tomllib
from pathlib import Path

import pytest

from xiaoshen.settings_store import upsert_keys, validate

OK = {"display_name": "賈維斯", "wake_word": "", "meeting_type": "病例討論"}


def test_upsert_interrupted_write_keeps_old_config(tmp_path, monkeypatch):
    """寫到一半當掉（這裡用 os.replace 失敗模擬）：原本的 config.toml 完整保留，不留下暫存檔（M-1）。"""
    from xiaoshen import settings_store

    p = tmp_path / "config.toml"
    original = "data_root = 'D:\\x'\ndisplay_name = '甲'\n"
    p.write_text(original, encoding="utf-8")

    def boom(src, dst):
        raise OSError("power loss")

    monkeypatch.setattr(settings_store.os, "replace", boom)
    with pytest.raises(OSError):
        upsert_keys(p, {"display_name": "乙"})
    assert p.read_text(encoding="utf-8") == original
    assert [f.name for f in tmp_path.iterdir()] == ["config.toml"]


def test_upsert_writes_temp_file_then_replaces(tmp_path, monkeypatch):
    from xiaoshen import settings_store

    p = tmp_path / "config.toml"
    p.write_text("x = 'a'\n", encoding="utf-8")
    calls = []
    real = settings_store.os.replace
    monkeypatch.setattr(settings_store.os, "replace", lambda s, d: calls.append((Path(s), Path(d))) or real(s, d))
    upsert_keys(p, {"x": "b"})
    assert calls == [(tmp_path / "config.toml.tmp", p)]
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {"x": "b"}


def test_upsert_keeps_other_lines_and_tables(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("# note\naudio_device = 'USBMIC'\n[extra]\nx = 1\n", encoding="utf-8")
    upsert_keys(p, {"display_name": "小森", "audio_device": "MIC2"})
    t = p.read_text(encoding="utf-8")
    assert t.index("display_name") < t.index("[extra]") and "audio_device = 'MIC2'" in t and "# note" in t and "x = 1" in t
    assert tomllib.loads(t) == {"audio_device": "MIC2", "display_name": "小森", "extra": {"x": 1}}


def test_upsert_does_not_touch_same_key_inside_table(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[extra]\ndisplay_name = 'inner'\n", encoding="utf-8")
    upsert_keys(p, {"display_name": "外"})
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {"display_name": "外", "extra": {"display_name": "inner"}}


def test_upsert_creates_file_and_quotes(tmp_path):
    p = tmp_path / "new.toml"
    upsert_keys(p, {"specialty": "it's \"x\""})
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {"specialty": "it's \"x\""}


def test_upsert_preserves_crlf(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b"a = 1\r\nwake_word = 'x'\r\n")
    upsert_keys(p, {"wake_word": "賈維斯", "specialty": "腎臟科"})
    raw = p.read_bytes().decode("utf-8")
    assert "\n" not in raw.replace("\r\n", "") and tomllib.loads(raw) == {"a": 1, "wake_word": "賈維斯", "specialty": "腎臟科"}


def test_validate_ok():
    assert validate(OK) == {}


def test_validate_name_length():
    assert "display_name" in validate({**OK, "display_name": ""})
    assert "display_name" in validate({**OK, "display_name": "字" * 11})
    assert "display_name" not in validate({**OK, "display_name": "字" * 10, "wake_word": "賈維斯"})


def test_validate_explicit_wake_word_uses_wakeword_rules():
    assert "wake_word" in validate({**OK, "wake_word": "下一頁"})
    assert "wake_word" in validate({**OK, "wake_word": "A"})
    assert validate({**OK, "wake_word": "小智慧"}) == {}


def test_validate_blank_wake_needs_usable_name():
    errs = validate({**OK, "display_name": "Jarvis", "wake_word": ""})
    assert "wake_word" in errs and "display_name" not in errs
    assert validate({**OK, "display_name": "Jarvis", "wake_word": "賈維斯"}) == {}


def test_validate_meeting_type():
    assert "meeting_type" in validate({**OK, "meeting_type": "晨會"})
    assert validate({**OK, "meeting_type": "其它"}) == {}


def test_validate_device_names():
    assert validate({**OK, "audio_device": "", "speaker_device": "喇叭 (USBMIC1.)"}) == {}
    assert "audio_device" in validate({**OK, "audio_device": "a\nb"})
    assert "speaker_device" in validate({**OK, "speaker_device": "x" * 101})


def test_device_options_default_first_and_keeps_saved_substring():
    from xiaoshen.settings_store import device_options

    names = ["麥克風 (USBMIC1.)", "麥克風排列 (Realtek(R) Audio)"]
    # 已存的是名稱片段：對到的那一項保留原本的值，不必改寫設定
    assert device_options("usbmic", names) == [
        {"value": "", "label": "電腦預設"},
        {"value": "usbmic", "label": "麥克風 (USBMIC1.)"},
        {"value": "麥克風排列 (Realtek(R) Audio)", "label": "麥克風排列 (Realtek(R) Audio)"},
    ]
    # 已存的裝置目前沒接上：仍列出，存檔時不會被悄悄改掉
    assert device_options("USBMIC", names[1:]) == [
        {"value": "", "label": "電腦預設"},
        {"value": "USBMIC", "label": "USBMIC（目前沒有接上）"},
        {"value": "麥克風排列 (Realtek(R) Audio)", "label": "麥克風排列 (Realtek(R) Audio)"},
    ]
    assert device_options("", []) == [{"value": "", "label": "電腦預設"}]


def test_validate_wake_sensitivity():
    assert validate({**OK, "wake_sensitivity": "high"}) == {}
    assert "wake_sensitivity" in validate({**OK, "wake_sensitivity": "超高"})

from pathlib import Path

import pytest

from xiaoshen.config import REPO_ROOT, Config, load_config


def test_defaults_when_file_missing(tmp_path):
    c = load_config(tmp_path / "nope.toml")
    assert c.data_root is None
    assert c.control_port == 8765 and c.phone_port == 8766


def test_toml_overrides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('data_root = "D:/x"\nphone_port = 9000\n', encoding="utf-8")
    c = load_config(p)
    assert c.data_root == Path("D:/x") and c.phone_port == 9000


def test_folder_names(tmp_path):
    c = Config(data_root=tmp_path)
    assert c.folder("題庫") == tmp_path / "題庫"
    with pytest.raises(ValueError):
        c.folder("其他")


def test_phase2_defaults_and_override(tmp_path):
    from xiaoshen.config import Config, load_config

    c = Config()
    assert c.model_answer == "gemini-3.1-pro-preview" and c.model_quiz == "gemini-3.1-pro-preview"
    assert c.oe_script.name == "oe.py" and c.max_reference_chars == 400_000
    p = tmp_path / "config.toml"
    p.write_text("model_quiz = 'gemini-3.8-flash'\noe_script = 'C:/x/oe.py'\n", encoding="utf-8")
    c2 = load_config(p)
    assert c2.model_quiz == "gemini-3.8-flash" and c2.oe_script.name == "oe.py"


def test_ocr_model_default():
    from xiaoshen.config import Config

    assert Config().model_ocr == "gemini-3.5-flash"


def test_defaults_are_generic():
    c = Config()
    assert (c.display_name, c.wake_word, c.specialty, c.meeting_type, c.meeting_type_other,
            c.audio_device, c.data_root) == ("賈維斯", "", "", "病例討論", "", "", None)
    assert c.display_kiosk is True and c.auto_convert_pptx is True
    assert c.effective_wake_word == "賈維斯"
    assert Config(display_name="小森").effective_wake_word == "小森"
    assert Config(display_name="小森", wake_word="阿腎").effective_wake_word == "阿腎"


def test_folder_without_data_root_raises_clear_error():
    with pytest.raises(RuntimeError, match="data_root"):
        Config().folder("題庫")


def test_browser_default_is_evaluated_at_instantiation(monkeypatch, tmp_path):
    import xiaoshen.config as cfg

    monkeypatch.setattr(cfg, "default_browser", lambda: tmp_path / "b.exe")
    assert Config().browser == tmp_path / "b.exe"
    monkeypatch.setattr(cfg, "default_browser", lambda: None)
    assert Config().browser is None


def test_kws_model_dir_under_app_data():
    from xiaoshen.platform_support import app_data_dir

    d = Config().kws_model_dir
    assert d.parent == app_data_dir() / "models" and d.name.startswith("sherpa-onnx-kws")


def test_new_fields_load_from_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("display_name = '小森'\nspecialty = '兒科腎臟科'\ndisplay_kiosk = false\n", encoding="utf-8")
    c = load_config(p)
    assert (c.display_name, c.specialty, c.display_kiosk) == ("小森", "兒科腎臟科", False)


def test_source_has_no_personal_defaults():
    text = (REPO_ROOT / "src/xiaoshen/config.py").read_text(encoding="utf-8")
    # 私人資料夾名稱與雲端路徑由發行前的隱私掃描把關（開發用 repo 的測試）
    assert "USBMIC" not in text

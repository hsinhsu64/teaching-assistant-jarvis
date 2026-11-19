"""第一次啟動設定（不要求 Google Drive）。"""

import sys
import tomllib

import pytest

from xiaoshen.config import FOLDERS, load_config
from pathlib import Path

from xiaoshen import first_run, settings_store
from xiaoshen.first_run import (FirstRunAnswers, apply_first_run, ensure_data_folder, needs_data_folder,
                                save_data_root, target_folder, validate_answers)


def test_needs_folder_only_when_data_root_unset(tmp_path):
    cfg = tmp_path / "config.toml"
    assert needs_data_folder(cfg) is True
    cfg.write_text("data_root = 'X:\\nowhere'\n", encoding="utf-8")
    assert needs_data_folder(cfg) is False  # 已設定就不再問（磁碟沒接上時由自我檢查提醒）


def test_config_with_only_data_root_skips_first_run(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("data_root = 'D:\\x'\n", encoding="utf-8")
    assert needs_data_folder(p) is False and load_config(p).display_name == "賈維斯"


def test_no_config_needs_first_run(tmp_path):
    assert needs_data_folder(tmp_path / "config.toml") is True


def test_target_folder_adds_name_once(tmp_path):
    assert target_folder(tmp_path) == tmp_path / "教學助理資料"
    assert target_folder(tmp_path / "教學助理資料") == tmp_path / "教學助理資料"


def test_save_creates_config(tmp_path):
    cfg = tmp_path / "config.toml"
    root = tmp_path / "it's here" / "教學助理資料"  # 路徑有單引號也要能讀回
    save_data_root(cfg, root)
    assert load_config(cfg).data_root == root


def test_save_keeps_other_settings_and_replaces_old_value(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("# 說明\n# data_root = 'E:\\old'\ndisplay_monitor = 2\ndata_root = 'C:\\old'\n", encoding="utf-8")
    save_data_root(cfg, tmp_path / "教學助理資料")
    raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert raw == {"display_monitor": 2, "data_root": str(tmp_path / "教學助理資料")}
    assert "# 說明" in cfg.read_text(encoding="utf-8")


def _answers(folder, **kw):
    base = dict(folder=folder, display_name="賈維斯", wake_word="", specialty="", meeting_type="Journal reading",
                meeting_type_other="")
    base.update(kw)
    return FirstRunAnswers(**base)


def test_apply_first_run_writes_all_settings(tmp_path):
    cfg = tmp_path / "config.toml"
    root = apply_first_run(cfg, _answers(tmp_path))
    c = load_config(cfg)
    assert root == tmp_path / "教學助理資料" and c.data_root == root and c.meeting_type == "Journal reading"
    assert all((root / n).is_dir() for n in FOLDERS)


def test_apply_first_run_writes_name_wake_specialty_other(tmp_path):
    cfg = tmp_path / "config.toml"
    apply_first_run(cfg, _answers(tmp_path, display_name="小安", wake_word="嗨小安", specialty="兒童腎臟",
                                  meeting_type="其它", meeting_type_other="讀書會"))
    c = load_config(cfg)
    assert (c.display_name, c.wake_word, c.specialty) == ("小安", "嗨小安", "兒童腎臟")
    assert (c.meeting_type, c.meeting_type_other) == ("其它", "讀書會")


def test_validate_answers_reuses_settings_store(tmp_path):
    assert validate_answers(_answers(tmp_path)) == {}
    bad = validate_answers(_answers(tmp_path, display_name="", meeting_type="x"))
    assert set(bad) >= {"display_name", "meeting_type"}
    a = _answers(tmp_path, wake_word="下一頁")
    assert validate_answers(a) == settings_store.validate(
        {"display_name": "賈維斯", "wake_word": "下一頁", "specialty": "", "meeting_type": "Journal reading",
         "meeting_type_other": ""})


def test_validate_answers_requires_folder():
    assert "folder" in validate_answers(_answers(None))


@pytest.mark.parametrize("parts", [(), ("docs",), ("docs", "教學助理資料"), ("src", "x", "y")])
def test_validate_rejects_folder_inside_install_folder(parts):
    """安裝資料夾更新時會整個換新、移除時會被刪掉：資料夾不能放在裡面（I-1）。"""
    errors = validate_answers(_answers(first_run.REPO_ROOT.joinpath(*parts)))
    assert "安裝資料夾" in errors.get("folder", "")


def test_validate_rejects_relative_folder_inside_install_folder(monkeypatch):
    monkeypatch.chdir(first_run.REPO_ROOT)  # start.bat／start.command 從安裝資料夾啟動
    assert "安裝資料夾" in validate_answers(_answers(Path("docs"))).get("folder", "")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 路徑不分大小寫")
def test_validate_rejects_install_folder_in_other_case():
    folder = Path(str(first_run.REPO_ROOT).upper()) / "docs"
    assert "folder" in validate_answers(_answers(folder))


def test_validate_allows_folder_outside_install_folder(tmp_path):
    assert validate_answers(_answers(tmp_path)) == {}
    assert validate_answers(_answers(first_run.REPO_ROOT.parent)) == {}  # 安裝資料夾的上一層：建在旁邊，可以


def test_existing_data_root_inside_install_folder_is_left_alone(tmp_path):
    """已設定的 data_root 一律不碰，即使在安裝資料夾裡。"""
    cfg = tmp_path / "config.toml"
    text = f"data_root = '{first_run.REPO_ROOT / 'docs' / '教學助理資料'}'\n"
    cfg.write_text(text, encoding="utf-8")
    assert ensure_data_folder(cfg, ask=lambda: pytest.fail("已設定就不該再問")) is True
    assert cfg.read_text(encoding="utf-8") == text


def test_picker_starts_in_documents_and_suggests_folder(monkeypatch, tmp_path):
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(first_run, "documents_dir", lambda: docs)
    assert first_run.picker_start_dir() == docs
    assert first_run.suggested_folder() == docs / "教學助理資料"


def test_picker_falls_back_to_home_without_documents(monkeypatch, tmp_path):
    monkeypatch.setattr(first_run, "documents_dir", lambda: tmp_path / "沒有這個資料夾")
    assert first_run.picker_start_dir() == Path.home()


def test_window_uses_documents_start_and_suggestion():
    import inspect
    src = inspect.getsource(first_run.ask_first_run)
    assert "initialdir=str(picker_start_dir())" in src and "value=str(suggested_folder())" in src


def test_ensure_creates_folders_and_saves(tmp_path):
    cfg = tmp_path / "config.toml"
    asked = []
    ok = ensure_data_folder(cfg, ask=lambda: asked.append(1) or _answers(tmp_path / "Docs"))
    root = tmp_path / "Docs" / "教學助理資料"
    assert ok and asked == [1] and all((root / n).is_dir() for n in FOLDERS)
    assert load_config(cfg).data_root == root
    assert ensure_data_folder(cfg, ask=lambda: asked.append(2)) and asked == [1]  # 之後不再問


def test_cancel_returns_false(tmp_path):
    cfg = tmp_path / "config.toml"
    assert ensure_data_folder(cfg, ask=lambda: None) is False
    assert not cfg.exists()


def test_window_texts_are_generic():
    import inspect
    from xiaoshen import first_run
    src = inspect.getsource(first_run)
    assert not any(w in src for w in ("晨會", "住院醫師"))


def test_ensure_oserror_returns_false_without_crash(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    def boom(self, *a, **k):
        raise PermissionError("唯讀")
    monkeypatch.setattr(Path, "mkdir", boom)
    assert ensure_data_folder(cfg, ask=lambda: _answers(tmp_path)) is False
    assert "無法建立" in capsys.readouterr().out
    assert needs_data_folder(cfg) is True


def test_failed_settings_write_does_not_mark_done(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    def boom(path, values):
        raise OSError("disk full")
    monkeypatch.setattr(first_run, "upsert_keys", boom)
    assert ensure_data_folder(cfg, ask=lambda: _answers(tmp_path)) is False
    assert needs_data_folder(cfg) is True  # data_root 沒寫入，下次還會再問


def test_all_six_keys_written_in_one_upsert(tmp_path, monkeypatch):
    calls = []
    real = settings_store.upsert_keys
    monkeypatch.setattr(first_run, "upsert_keys", lambda p, v: calls.append(dict(v)) or real(p, v))
    apply_first_run(tmp_path / "config.toml", _answers(tmp_path))
    assert len(calls) == 1
    assert set(calls[0]) == {"data_root", "display_name", "wake_word", "specialty", "meeting_type",
                             "meeting_type_other"}


# ---- 2026-11-19：主持人畫面「更換資料夾…」用的選資料夾視窗（另開一個小程式，Mac 也能跳視窗） ----

def test_pick_folder_dialog_returns_chosen_path():
    from types import SimpleNamespace

    from xiaoshen.first_run import pick_folder_dialog

    seen = {}

    def runner(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return SimpleNamespace(returncode=0, stdout="D:/教學資料\n")

    assert pick_folder_dialog(Path("C:/start"), runner=runner) == "D:/教學資料"
    assert "askdirectory" in " ".join(seen["cmd"]) and seen["kw"]["timeout"] >= 60


def test_pick_folder_dialog_cancel_or_failure_is_empty():
    import subprocess
    from types import SimpleNamespace

    from xiaoshen.first_run import pick_folder_dialog

    assert pick_folder_dialog(Path("."), runner=lambda c, **k: SimpleNamespace(returncode=0, stdout="\n")) == ""
    assert pick_folder_dialog(Path("."), runner=lambda c, **k: SimpleNamespace(returncode=1, stdout="x")) == ""

    def timeout(c, **k):
        raise subprocess.TimeoutExpired(c, 1)

    assert pick_folder_dialog(Path("."), runner=timeout) == ""

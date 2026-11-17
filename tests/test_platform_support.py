"""平台層：Windows / macOS 各自的實作都能在 Windows 上用純函式與 monkeypatch 測。"""

import subprocess
from pathlib import Path

import pytest

from xiaoshen import platform_support as ps
from xiaoshen.platform_support import (
    mac_rects_from_frames, upsert_env_block,
)

BEGIN = "# >>> teaching-assistant >>>"
END = "# <<< teaching-assistant <<<"


def _os(monkeypatch, *, win=False, mac=False):
    monkeypatch.setattr(ps, "IS_WINDOWS", win)
    monkeypatch.setattr(ps, "IS_MAC", mac)


def test_mac_rects_convert_bottom_left_to_top_left():
    # 主螢幕 1440x900；投影機 1920x1080 放在主螢幕右邊、底部對齊
    rects = mac_rects_from_frames([(0, 0, 1440, 900), (1440, 0, 1920, 1080)], main_height=900)
    assert rects == [(0, 0, 1440, 900), (1440, -180, 3360, 900)]


def test_mac_rects_empty():
    assert mac_rects_from_frames([], main_height=0) == []


def test_env_block_added_to_empty():
    assert upsert_env_block("", "GEMINI_API_KEY", "AIzaX") == (
        "# >>> teaching-assistant >>>\nexport GEMINI_API_KEY='AIzaX'\n# <<< teaching-assistant <<<\n")


def test_env_block_replaced_not_duplicated():
    first = upsert_env_block("export PATH=/x:$PATH\n", "GEMINI_API_KEY", "old")
    second = upsert_env_block(first, "GEMINI_API_KEY", "new")
    assert second.count(">>> teaching-assistant >>>") == 1
    assert "export PATH=/x:$PATH\n" in second and "'new'" in second and "'old'" not in second


def test_env_block_keeps_other_names_in_block():
    one = upsert_env_block("", "A", "1")
    two = upsert_env_block(one, "B", "2")
    assert "export A='1'" in two and "export B='2'" in two
    three = upsert_env_block(two, "A", "9")
    assert "export A='9'" in three and "export B='2'" in three and "'1'" not in three
    assert three.count(BEGIN) == 1 and three.count(END) == 1


def test_env_block_adds_newline_before_block_when_text_lacks_one():
    out = upsert_env_block("export X=1", "K", "v")
    assert out.startswith("export X=1\n" + BEGIN)


def test_env_block_quotes_single_quote_in_value():
    out = upsert_env_block("", "K", "a'b")
    assert r"export K='a'\''b'" in out  # shell 的單引號跳脫


def test_bundled_tool_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("xiaoshen.config.REPO_ROOT", tmp_path)
    for win, mac, cloud, node in [(True, False, "cloudflared.exe", "node/node.exe"),
                                  (False, True, "cloudflared", "node/bin/node")]:
        _os(monkeypatch, win=win, mac=mac)
        assert ps.bundled_tool("cloudflared") is None
        assert ps.bundled_tool("node") is None
        for rel in (cloud, node):
            f = tmp_path / "tools" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
        assert ps.bundled_tool("cloudflared") == tmp_path / "tools" / cloud
        assert ps.bundled_tool("node") == tmp_path / "tools" / Path(node)
        for rel in (cloud, node):
            (tmp_path / "tools" / rel).unlink()


def test_bundled_tool_unknown_name(monkeypatch):
    with pytest.raises(ValueError):
        ps.bundled_tool("nope")


def test_app_data_dir_mac(monkeypatch):
    _os(monkeypatch, mac=True)
    assert ps.app_data_dir() == Path.home() / "Library" / "Application Support" / "xiaoshen"


def test_app_data_dir_windows(monkeypatch, tmp_path):
    _os(monkeypatch, win=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ps.app_data_dir() == tmp_path / "xiaoshen"


def test_app_data_dir_other(monkeypatch):
    _os(monkeypatch)
    assert ps.app_data_dir() == Path.home() / ".xiaoshen"


def test_desktop_dir_non_windows(monkeypatch):
    _os(monkeypatch, mac=True)
    assert ps.desktop_dir() == Path.home() / "Desktop"


def test_documents_dir_non_windows(monkeypatch):
    _os(monkeypatch, mac=True)
    assert ps.documents_dir() == Path.home() / "Documents"


@pytest.mark.skipif(not ps.IS_WINDOWS, reason="Windows 才問系統的「文件」資料夾")
def test_documents_dir_windows_is_a_real_folder():
    assert ps.documents_dir().is_absolute() and ps.documents_dir().is_dir()


def test_default_browser_mac_order(monkeypatch):
    _os(monkeypatch, mac=True)
    edge = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    monkeypatch.setattr(Path, "exists", lambda self: self in (edge, chrome))
    assert ps.default_browser() == edge
    monkeypatch.setattr(Path, "exists", lambda self: self == chrome)
    assert ps.default_browser() == chrome
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert ps.default_browser() is None


def test_default_browser_windows_order(monkeypatch):
    _os(monkeypatch, win=True)
    monkeypatch.setenv("ProgramFiles(x86)", "C:/PF86")
    monkeypatch.setenv("ProgramFiles", "C:/PF")
    chrome = Path("C:/PF/Google/Chrome/Application/chrome.exe")
    monkeypatch.setattr(Path, "exists", lambda self: self == chrome)
    assert ps.default_browser() == chrome
    edge = Path("C:/PF86/Microsoft/Edge/Application/msedge.exe")
    monkeypatch.setattr(Path, "exists", lambda self: self in (edge, chrome))
    assert ps.default_browser() == edge


def test_kill_profile_processes_mac_uses_pkill(monkeypatch):
    _os(monkeypatch, mac=True)
    calls = []
    monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    profile = Path("/x/edge-host")
    ps.kill_profile_processes(profile)
    assert calls == [["pkill", "-f", "--", f"--user-data-dir={profile}"]]


def test_kill_profile_processes_swallows_errors(monkeypatch):
    _os(monkeypatch, mac=True)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("pkill", 1)
    monkeypatch.setattr(ps.subprocess, "run", boom)
    ps.kill_profile_processes(Path("/x/p"))  # 不丟例外


def test_kill_profile_processes_windows_uses_powershell(monkeypatch):
    _os(monkeypatch, win=True)
    calls = []
    monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    ps.kill_profile_processes(Path("C:/x/edge-host"))
    assert calls[0][0] == "powershell" and "edge-host" in calls[0][-1]


def test_monitor_rects_other_os_empty(monkeypatch):
    _os(monkeypatch)
    assert ps.monitor_rects() == []


def test_save_user_env_mac_writes_zprofile(monkeypatch, tmp_path):
    _os(monkeypatch, mac=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ps.save_user_env("GEMINI_API_KEY", "k1")
    ps.save_user_env("GEMINI_API_KEY", "k2")
    text = (tmp_path / ".zprofile").read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1 and "export GEMINI_API_KEY='k2'" in text and "k1" not in text

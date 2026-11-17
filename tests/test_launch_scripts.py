"""啟動／設定金鑰腳本：Windows .bat 必須純 ASCII＋CRLF；Mac .command 必須 LF＋shebang。"""

from xiaoshen.config import REPO_ROOT


def test_bat_files_ascii_crlf():
    for name in ("start.bat", "set-key.bat"):
        b = (REPO_ROOT / name).read_bytes()
        assert b.isascii() and b"\r\n" in b and b"\n" not in b.replace(b"\r\n", b"")


def test_command_files_lf_and_shebang():
    for name in ("start.command", "set-key.command"):
        b = (REPO_ROOT / name).read_bytes()
        assert b.startswith(b"#!/bin/bash\n") and b"\r" not in b


def test_command_files_content():
    start = (REPO_ROOT / "start.command").read_text(encoding="utf-8")
    assert 'cd "$(dirname "$0")"' in start and "-m xiaoshen" in start and ".local/bin/uv" in start
    key = (REPO_ROOT / "set-key.command").read_text(encoding="utf-8")
    assert "xiaoshen.keysetup" in key and "read -n 1" in key


def test_command_files_chinese_prompt_and_uv_missing():
    for name in ("start.command", "set-key.command"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "Press any key" not in text
        read_lines = [ln for ln in text.splitlines() if "read -n 1" in ln]
        assert read_lines and all(not ln.isascii() for ln in read_lines)
        assert "找不到 uv" in text


def test_start_command_loads_marked_key_block_only():
    text = (REPO_ROOT / "start.command").read_text(encoding="utf-8")
    assert "# >>> teaching-assistant >>>" in text and "# <<< teaching-assistant <<<" in text
    assert "GEMINI_API_KEY" in text and "eval" in text
    assert "source" not in text and ". ~/.zprofile" not in text and '. "$HOME/.zprofile"' not in text


def test_bat_files_fall_back_to_user_local_uv():
    for name in ("start.bat", "set-key.bat"):
        text = (REPO_ROOT / name).read_text(encoding="ascii")
        assert "where uv" in text and r"%USERPROFILE%\.local\bin\uv.exe" in text and '"%UV%" run' in text

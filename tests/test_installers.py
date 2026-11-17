"""一行安裝腳本（install.ps1、install.sh）、公開 README、LICENSE 的靜態檢查。

兩種版型都能跑：開發用的 repo 把這些檔案放在 release/ 底下；公開 repo 放在根目錄。
完整試裝要用發行腳本 stage 出來的 zip。
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from xiaoshen.config import REPO_ROOT

# 公開 repo 的安裝腳本、README、LICENSE 在根目錄；開發用的 repo 放在 release/ 底下（根目錄的 README 是另一份）
PRIVATE_LAYOUT = (REPO_ROOT / "release" / "install.sh").is_file()
REL = REPO_ROOT / "release" if PRIVATE_LAYOUT else REPO_ROOT
IS_WINDOWS = sys.platform == "win32"
KEYS = ("NODE_VERSION", "CLOUDFLARED_VERSION", "OE_MCP_COMMIT")
NAME = "教學助理賈維斯"
README_ANCHOR = "openevidence-%E6%93%B4%E5%85%85%E5%8A%9F%E8%83%BD"  # GitHub 給「## OpenEvidence 擴充功能」的錨點
# polyformproject.org/licenses/noncommercial/1.0.0.txt 官方純文字的 sha256（2026-11-16 下載）
POLYFORM_NC_SHA256 = "ffcca38841adb694b6f380647e15f17c446a4d1656fed51a1e2041d064c94cc8"


def _versions_env() -> dict[str, str]:
    out = {}
    for line in (REL / "versions.env").read_text(encoding="ascii").splitlines():
        if line.strip() and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _header_values(text: str, pattern: str) -> dict[str, str]:
    return {k: m.group(1) for k in KEYS if (m := re.search(pattern.format(k=k), text, re.M))}


def test_ps1_is_ascii():
    assert (REL / "install.ps1").read_bytes().isascii()


@pytest.mark.skipif(not IS_WINDOWS, reason="PowerShell 解析只在 Windows 跑")
def test_ps1_parses():
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "$e=$null; $null=[System.Management.Automation.Language.Parser]::ParseFile("
                        "(Resolve-Path 'install.ps1').Path,[ref]$null,[ref]$e); if($e){$e; exit 1}"],
                       cwd=REL, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


_NON_GIT_BASH_DIRS = {"system32", "syswow64", "windowsapps"}


def _is_windows_launcher_bash(path: str) -> bool:
    """System32\\bash.exe 是 WSL 啟動器、WindowsApps 是 Store 版，都不是 Git Bash。"""
    return not _NON_GIT_BASH_DIRS.isdisjoint(re.split(r"[\\/]", path.lower()))


def _real_bash() -> str | None:
    if not IS_WINDOWS:
        return shutil.which("bash")
    found = shutil.which("bash")
    if found and not _is_windows_launcher_bash(found) and os.path.isfile(found):
        return found
    candidates = []
    git = shutil.which("git")
    if git:
        git_dir = Path(git).parent  # <Git>\cmd、<Git>\bin 或 <Git>\mingw64\bin
        for base in (git_dir.parent, git_dir.parent.parent, git_dir.parent.parent.parent):
            candidates += [base / "bin" / "bash.exe", base / "usr" / "bin" / "bash.exe"]
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if os.path.isfile(c) and not _is_windows_launcher_bash(str(c)):
            return str(c)
    return None


def test_real_bash_skips_system32_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.modules[__name__], "IS_WINDOWS", True)
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    git_bash = r"C:\Program Files\Git\bin\bash.exe"

    monkeypatch.setattr(shutil, "which", lambda name: r"C:\Windows\System32\bash.exe" if name == "bash" else None)
    assert _real_bash() == str(tmp_path / "Git" / "bin" / "bash.exe")

    monkeypatch.setattr(shutil, "which", lambda name: git_bash if name == "bash" else None)
    assert _real_bash() == git_bash


def test_sh_parses():
    bash = _real_bash()
    if bash is None:
        pytest.skip("找不到可用的 bash（Windows 上需 Git Bash）")
    r = subprocess.run([bash, "-n", "install.sh"], cwd=REL, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_sh_lf_and_shebang():
    b = (REL / "install.sh").read_bytes()
    assert b.startswith(b"#!/bin/bash\n") and b"\r" not in b


def test_sh_avoids_bash4_features():
    """macOS 內建 bash 3.2：不能用關聯陣列、mapfile、${x,,} 這類 bash 4 語法。"""
    text = (REL / "install.sh").read_text(encoding="utf-8")
    for bad in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "|&", "&>>", "coproc"):
        assert bad not in text, bad


def test_sh_reads_user_input_from_tty():
    text = (REL / "install.sh").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert all("/dev/tty" in ln for ln in lines if re.search(r"\bread\b", ln) and "while" not in ln)
    key_lines = [ln for ln in lines if "xiaoshen.keysetup" in ln]
    assert key_lines and all("< /dev/tty" in ln for ln in key_lines)
    assert "uname -m" in text


def test_versions_env_values_look_pinned():
    v = _versions_env()
    assert set(v) == set(KEYS)
    assert re.fullmatch(r"\d+\.\d+\.\d+", v["NODE_VERSION"])
    assert re.fullmatch(r"\d{4}\.\d+\.\d+", v["CLOUDFLARED_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{40}", v["OE_MCP_COMMIT"])


def test_versions_match_between_scripts():
    v = _versions_env()
    ps1 = (REL / "install.ps1").read_text(encoding="ascii")
    sh = (REL / "install.sh").read_text(encoding="utf-8")
    assert _header_values(ps1, r"^\$script:{k} = '([^']+)'") == v
    assert _header_values(sh, r'^{k}="([^"]+)"') == v


def test_installers_use_fixed_paths_and_source():
    ps1 = (REL / "install.ps1").read_text(encoding="ascii")
    sh = (REL / "install.sh").read_text(encoding="utf-8")
    zip_url = "https://github.com/hsinhsu64/teaching-assistant-jarvis/archive/refs/heads/main.zip"
    for text in (ps1, sh):
        assert zip_url in text
        assert "TeachingAssistantJarvis" in text and "TAJ_HOME" in text
        assert "config.toml" in text and "get_wake_model.py" in text and "xiaoshen.keysetup" in text
        assert "GEMINI_API_KEY" in text and README_ANCHOR in text
        assert "relay-daemon.js" in text and "extension" in text
    assert "-Source" in ps1 or "$Source" in ps1
    assert "SOURCE" in sh
    assert "cloudflared-windows-amd64.exe" in ps1 and "-win-x64.zip" in ps1
    assert "cloudflared-darwin-" in sh and "-darwin-" in sh


@pytest.mark.parametrize("name,call", [("install.ps1", "Step"), ("install.sh", "step")])
def test_installer_steps_in_fixed_order(name, call):
    """uv → 程式 → uv sync → cloudflared → node → openevidence-mcp → 喚醒詞 → 捷徑 → 金鑰 → README。"""
    marks = ["astral.sh/uv", "main.zip", "sync", "cloudflared/releases", "nodejs.org/dist",
             "openevidence-mcp/archive", "get_wake_model.py", "start.", "xiaoshen.keysetup", README_ANCHOR]
    text = (REL / name).read_text(encoding="utf-8")
    starts = [(int(m.group(1)), m.start()) for m in re.finditer(rf"^\s*{call} (\d+) ", text, re.M)]
    assert [n for n, _ in starts] == list(range(1, 11))
    ends = [p for _, p in starts[1:]] + [len(text)]
    for (n, start), end, mark in zip(starts, ends, marks):
        assert mark in text[start:end], (name, n, mark)


def test_ps1_shortcut_name_from_code_points():
    ps1 = (REL / "install.ps1").read_text(encoding="ascii")
    m = re.search(r"\$name = -join @\(([^)]*)\)", ps1)
    assert m, "找不到以 [char] 組成的捷徑名稱"
    chars = re.findall(r"\[char\]0x([0-9a-fA-F]{4})", m.group(1))
    assert "".join(chr(int(c, 16)) for c in chars) == NAME
    assert "start.bat" in ps1 and ".lnk" in ps1


def test_ps1_chinese_messages_decode():
    """install.ps1 的中文訊息以 \\uXXXX 寫在 ASCII 檔裡，執行時用 [regex]::Unescape 還原。"""
    ps1 = (REL / "install.ps1").read_text(encoding="ascii")
    escaped = re.findall(r"Z '([^']*)'", ps1)
    assert escaped
    decoded = [e.encode("ascii").decode("unicode_escape") for e in escaped]
    assert any(d.startswith("第 ") and "步" in d for d in decoded)
    assert any("失敗" in d and "請" in d for d in decoded)


def test_sh_desktop_command_and_failure_message():
    sh = (REL / "install.sh").read_text(encoding="utf-8")
    assert f'APP_NAME="{NAME}"' in sh and '"$desk/$APP_NAME.command"' in sh and "chmod +x" in sh and "start.command" in sh
    assert "第 " in sh and "步" in sh and "失敗，請" in sh


def test_sh_desktop_shortcut_failure_is_nonfatal():
    sh = (REL / "install.sh").read_text(encoding="utf-8")
    step8 = sh[sh.index('step 8 "'):sh.index('step 9 "')]
    assert "elif ! {" in step8 and "printf" in step8 and 'chmod +x "$shortcut"' in step8
    assert "start.command" in step8.split("elif ! {", 1)[1] and ">&2" in step8


def _run_install_sh_tail(tmp_path, body: str) -> str:
    """載入 install.sh 的函式（不執行 main），再跑 body，回傳輸出。"""
    bash = _real_bash()
    if bash is None:
        pytest.skip("找不到可用的 bash（Windows 上需 Git Bash）")
    text = (REL / "install.sh").read_text(encoding="utf-8").rstrip()
    assert text.endswith('main "$@"')
    script = tmp_path / "t.sh"
    script.write_bytes((text[: -len('main "$@"')] + body + "\n").encode("utf-8"))
    r = subprocess.run([bash, script.as_posix()], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r.stdout.decode("utf-8")


def test_sh_final_message_after_shortcut_failure_points_at_start_command(tmp_path):
    """桌面捷徑建立失敗時，最後一句不能叫人雙擊桌面上的捷徑（M-4）。"""
    out = _run_install_sh_tail(tmp_path, 'INSTALL_DIR="/Users/t/TAJ"; SHORTCUT_OK=0; finish_message')
    assert "/Users/t/TAJ/start.command" in out and "桌面上的" not in out
    out = _run_install_sh_tail(tmp_path, 'INSTALL_DIR="/Users/t/TAJ"; SHORTCUT_OK=1; finish_message')
    assert f"雙擊桌面上的「{NAME}」" in out


def test_sh_remembers_shortcut_failure():
    sh = (REL / "install.sh").read_text(encoding="utf-8")
    step8 = sh[sh.index('step 8 "'):sh.index('step 9 "')]
    failure = step8.split("elif ! {", 1)[1].split("else", 1)[0]
    assert "SHORTCUT_OK=0" in failure and "SHORTCUT_OK=1" in sh[:sh.index('step 8 "')]
    assert sh[sh.index('step 10 "'):].count("finish_message") == 1


def test_readme_key_storage_claim_is_accurate():
    """Mac 的金鑰寫在 ~/.zprofile：README 不能說「不會寫進任何設定檔」（M-2）。"""
    text = (REL / "README.md").read_text(encoding="utf-8")
    assert "不會寫進任何設定檔" not in text and "不會寫進任何檔案" not in text
    assert "不會寫進教學助理的設定檔（`config.toml`）" in text


def test_readme_content():
    text = (REL / "README.md").read_text(encoding="utf-8")
    assert "irm https://raw.githubusercontent.com/hsinhsu64/teaching-assistant-jarvis/main/install.ps1 | iex" in text
    assert "curl -fsSL https://raw.githubusercontent.com/hsinhsu64/teaching-assistant-jarvis/main/install.sh | bash" in text
    assert re.search(r"^## OpenEvidence 擴充功能$", text, re.M)
    assert "TeachingAssistantJarvis/tools/openevidence-mcp/extension/dist" in text
    assert "測試中" in text and "PolyForm Noncommercial" in text and NAME in text


def test_readme_data_folder_outside_install_folder():
    """資料夾從「文件」開始選、不能放在安裝資料夾裡（I-1），「移除」那段才成立。"""
    text = (REL / "README.md").read_text(encoding="utf-8")
    first = text[text.index("## 第一次啟動"):text.index("## 更新")]
    assert "文件" in first and "不能選在" in first and "TeachingAssistantJarvis" in first
    remove = text[text.index("## 移除"):text.index("## 授權")]
    assert "安裝資料夾以外" in remove


def test_readme_has_no_personal_info():
    # 私人路徑、帳號與舊名由發行前的隱私掃描與發行內容檢查把關（開發用 repo 的測試）
    text = (REL / "README.md").read_text(encoding="utf-8")
    for bad in ("@", "晨會", "住院醫師"):
        assert bad not in text, bad


def test_license_is_official_text_with_notice():
    text = (REL / "LICENSE").read_text(encoding="ascii")
    notice = "Required Notice: Copyright (c) 2026 hsinhsu64\n\n"
    assert text.startswith(notice)
    body = text[len(notice):]
    assert hashlib.sha256(body.encode("ascii")).hexdigest() == POLYFORM_NC_SHA256


def test_gitignore_excludes_tools():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/tools/" in lines


def test_public_gitignore_keeps_local_files_out_of_git():
    """公開 repo 的 .gitignore：老師的 agent 在 fork 裡跑過程式後，設定、虛擬環境、工具、記錄不會被 commit。"""
    raw = (REL / ".gitignore").read_bytes()
    assert b"\r" not in raw
    lines = raw.decode("utf-8").splitlines()
    for must in ("config.toml", "config.toml.tmp", ".venv/", "__pycache__/", "/tools/", "*.log"):
        assert must in lines, must


# ---- 協作：給 AI coding agent 與人看的說明、PR 範本、公開 repo 的 CI ----

def test_public_ci_workflow():
    """公開 repo 的 CI：push 到 main、Pull Request、手動觸發；Windows 與 Mac 都跑；不用 pull_request_target。"""
    text = (REL / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert b"\r" not in (REL / ".github" / "workflows" / "tests.yml").read_bytes()
    on = text[text.index("\non:"):text.index("\njobs:")]
    assert re.search(r"^  push:\n    branches: \[main\]$", on, re.M)
    assert re.search(r"^  pull_request:$", on, re.M) and re.search(r"^  workflow_dispatch:$", on, re.M)
    assert "pull_request_target" not in text and "secrets." not in text
    assert "fail-fast: false" in text and "os: [windows-latest, macos-latest]" in text
    steps = ["git config --global core.autocrlf false", "actions/checkout@", "astral-sh/setup-uv@",
             "run: uv sync --locked", "run: uv run pytest -q"]
    positions = [text.index(s) for s in steps]
    assert positions == sorted(positions)


def test_private_and_public_ci_run_the_same_steps():
    if not PRIVATE_LAYOUT:
        pytest.skip("公開 repo 只有一份 workflow")
    def steps(path):
        text = path.read_text(encoding="utf-8")
        return text[text.index("    steps:"):]
    assert steps(REPO_ROOT / ".github" / "workflows" / "tests.yml") == steps(REL / ".github" / "workflows" / "tests.yml")


def test_agents_md_for_coding_agents():
    text = (REL / "AGENTS.md").read_text(encoding="utf-8")
    for must in ("src/xiaoshen", "platform_support.py", "converter.py", "AppleScript", "install.sh",
                 "start.command", "set-key.command", "launcher.py",
                 "uv sync", "./start.command", "uv run python -m xiaoshen", "uv run pytest -q",
                 "診斷包-YYYYMMDD-HHMM.zip", "「產生診斷包」", "~/Library/Application Support/xiaoshen/logs",
                 "config.toml", "GEMINI_API_KEY", "cookie", "隱私掃描", "CRLF", "LF", "台灣",
                 "fork", "main"):
        assert must in text, must
    for item in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)"):
        assert item in text, item
    assert "一個 PR 只處理一個問題" in text
    assert "不會直接在 GitHub 上合併" in text and "不是被拒絕" in text
    # 更新時安裝資料夾保留的東西與 install.sh／install.ps1 一致
    assert "保留 `config.toml`、`tools/` 與 `.venv`" in text and "只保留 `config.toml`" not in text
    assert "已列在 `.gitignore`" in text
    # kiosk 是兩個平台的預設（config.py、config.example.toml）；Mac 視窗位置有問題才改一般視窗
    assert "display_kiosk = false" in text and "Mac 不用 kiosk" not in text
    _assert_honest_credit(text)


def _assert_honest_credit(text):
    """致謝：關閉 PR 時的留言與發行說明；Co-Authored-By 只在開發 repo 的移植 commit，公開 repo 只看到 Release commit。"""
    assert "關閉 PR 時的留言" in text and "發行說明" in text
    assert "Co-Authored-By" in text and "Release v" in text
    assert "收錄的 commit 會加一行" not in text and "維護者會在 commit 加上一行" not in text


def test_contributing_md():
    text = (REL / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "AGENTS.md" in text and "PolyForm Noncommercial License 1.0.0" in text
    assert "不會直接在 GitHub 上合併" in text and "fork" in text
    _assert_honest_credit(text)


def test_pull_request_template_checklist():
    text = (REL / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    for item in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)"):
        assert re.search(rf"^- \[ \] \*\*{re.escape(item)}", text, re.M), item
    for rule in ("config.toml", "金鑰", "cookie", "診斷包", "隱私掃描", "行尾", "一個 PR 只處理一個問題"):
        assert rule in text, rule


def test_readme_links_collaboration_docs():
    text = (REL / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## 回報問題與貢獻"):text.index("## 授權")]
    assert "(AGENTS.md)" in section and "(CONTRIBUTING.md)" in section


def test_shell_scripts_brace_variables_before_non_ascii():
    # macOS /bin/bash 3.2 (UTF-8 locale) folds the bytes of a multibyte character
    # that directly follows $NAME into the identifier, so "$APP_NAME」" becomes an
    # unbound longer name and `set -u` aborts. Always write ${NAME} before CJK text.
    pattern = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]")
    scripts = {
        p
        for root in (REPO_ROOT, REPO_ROOT / "release")
        if root.is_dir()
        for glob in ("*.sh", "*.command")
        for p in root.glob(glob)
    }
    assert scripts, "no shell scripts found to scan"
    hits = []
    for path in sorted(scripts):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}: {line.strip()}")
    assert not hits, "unbraced $VAR directly before non-ASCII:\n" + "\n".join(hits)

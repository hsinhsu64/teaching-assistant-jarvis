"""沒裝 Claude Code 也能用 OpenEvidence。

openevidence-mcp 的轉接程式（dist/relay-daemon.js，127.0.0.1:8787）本來就能單獨執行；
本程式啟動時發現它沒在跑，就用 Node.js 把它帶起來（和 Claude Code 自動啟動它的方式相同）。
oe.py 優先用 ~/.claude/skills/openevidence 的，沒有就用 openevidence-mcp 裡附的那份。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .config import REPO_ROOT, Config
from .oe_relay import RELAY, _get_json
from .platform_support import bundled_tool

RELAY_JS = Path("dist") / "relay-daemon.js"
SKILL_OE = Path("openevidence-skill") / "scripts" / "oe.py"
PLACES = [Path.home() / "openevidence-mcp", Path.home() / ".claude" / "mcp-servers" / "openevidence-mcp"]
WAIT_SECONDS = 5.0
STEP = 0.25


def find_oe_mcp(config: Config, places: list[Path] | None = None) -> Path | None:
    default_places = [REPO_ROOT / "tools" / "openevidence-mcp", *PLACES]
    candidates = ([config.oe_mcp_dir] if config.oe_mcp_dir else []) + list(default_places if places is None else places)
    for c in candidates:
        if (c / RELAY_JS).is_file():
            return c
    return None


def resolve_oe_script(config: Config, mcp: Path | None) -> Path:
    if config.oe_script.is_file() or mcp is None:
        return config.oe_script
    bundled = mcp / SKILL_OE
    return bundled if bundled.is_file() else config.oe_script


def relay_up(get: Callable[[str, float], dict] = _get_json) -> bool:
    """relay 有在跑（不管瀏覽器分頁連上沒）。"""
    try:
        return bool(get(f"{RELAY}/health", 1.5).get("ok"))
    except (OSError, ValueError):
        return False


def ensure_relay(mcp: Path | None, up: Callable[[], bool] = relay_up, popen=subprocess.Popen,
                 which: Callable[[str], str | None] = shutil.which,
                 sleep: Callable[[float], None] = time.sleep) -> str | None:
    """需要就啟動 relay。回傳要顯示在自我檢查的提醒；None＝沒問題。"""
    if up():
        return None
    if mcp is None:
        return "OpenEvidence 轉接程式沒有在執行，也找不到 openevidence-mcp 資料夾（安裝步驟見 README）"
    node = which("node") or bundled_tool("node")
    node = str(node) if node else None
    if not node:
        return "找不到 Node.js，無法啟動 OpenEvidence 轉接程式（安裝步驟見 README）"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    popen([node, str(mcp / RELAY_JS)], cwd=str(mcp), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL, creationflags=flags)
    for _ in range(int(WAIT_SECONDS / STEP)):
        if up():
            return None
        sleep(STEP)
    return "OpenEvidence 轉接程式啟動後沒有回應（可在 openevidence-mcp 資料夾執行 npm run relay 看錯誤）"

"""「設定金鑰」小程式：把 Gemini 金鑰存成使用者環境變數 GEMINI_API_KEY（Windows 登錄檔；Mac 寫進 ~/.zprofile）。

用法：Windows 雙擊 set-key.bat（或「設定金鑰.bat」）、Mac 雙擊 set-key.command，或 `uv run python -m xiaoshen.keysetup`。
金鑰輸入時不顯示；Windows 存在使用者環境變數、不寫進任何檔案，Mac 存在 ~/.zprofile；先向 Google 確認可用才存。
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Callable

from .platform_support import IS_MAC, save_user_env

NAME = "GEMINI_API_KEY"
KEY_PAGE = "https://aistudio.google.com/apikey"
MAX_TRIES = 3


class KeyError_(ValueError):
    pass


def clean_key(raw: str) -> str:
    key = raw.strip().strip("\"'").strip()
    if not key or not key.isascii() or any(ch.isspace() for ch in key):
        raise KeyError_("看起來不是金鑰：請整串複製（通常 AIza 開頭、中間沒有空白）")
    return key


def verify_key(key: str) -> str:
    """ok／invalid（Google 拒絕）／unreachable（連不上，無法確認）。"""
    try:
        from google import genai
        from google.genai import errors
    except ImportError:
        return "unreachable"
    client = genai.Client(api_key=key)  # 要留著：沒有變數指著，送出前就被回收、連線被關掉
    try:
        next(iter(client.models.list(config={"page_size": 1})), None)
    except errors.ClientError as e:
        return "invalid" if getattr(e, "code", None) in (400, 401, 403) else "unreachable"
    except Exception:
        return "unreachable"
    return "ok"


def setup_key(ask: Callable[[], str], verify: Callable[[str], str] = verify_key,
              save: Callable[[str, str], None] = save_user_env, say: Callable[[str], None] = print) -> int:
    key = None
    for _ in range(MAX_TRIES):
        try:
            key = clean_key(ask())
            break
        except KeyError_ as e:
            say(str(e))
    if key is None:
        return 1
    say("向 Google 確認金鑰中…")
    result = verify(key)
    if result == "invalid":
        say(f"這把金鑰無效（Google 拒絕），沒有存。請到 {KEY_PAGE} 重新複製後再執行一次。")
        return 1
    save(NAME, key)
    if result == "unreachable":
        say("金鑰已存好，但現在連不上 Google，沒辦法確認它是否正確；連上網路後啟動助理，看自我檢查的 AI 那一列。")
    else:
        say("金鑰已存好，而且確認可用。")
    if IS_MAC:
        say("金鑰存在 ~/.zprofile。請關掉這個 Terminal（終端機）視窗與已開著的助理，再重新啟動助理，新的金鑰才會生效。")
    else:
        say("請關掉已開著的助理（和命令視窗）後重新啟動，新的金鑰才會生效。")
    return 0


def main() -> int:
    print("設定助理的 Gemini 金鑰")
    print(f"1. 到 {KEY_PAGE} 用 Google 帳號登入，按「Create API key」，複製那一長串金鑰。")
    print("2. 回到這個視窗貼上金鑰（Mac 按 Cmd+V、Windows 按滑鼠右鍵；畫面上不會顯示任何字），再按 Enter。")
    if os.environ.get(NAME):
        print("（這台電腦已經有金鑰；輸入新的會取代舊的。）")
    return setup_key(ask=lambda: getpass.getpass("金鑰："))


if __name__ == "__main__":
    sys.exit(main())

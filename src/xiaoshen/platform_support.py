"""平台層：所有「只有某個作業系統才有」的程式集中在這裡（Windows 與 macOS）。

其他模組不直接判斷 sys.platform，都呼叫這裡。Mac 的部分用純函式拆開（mac_rects_from_frames、
upsert_env_block），可以在 Windows 上測；AppKit 只在 Mac 分支裡才 import。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)，左上原點

ENV_BEGIN = "# >>> teaching-assistant >>>"
ENV_END = "# <<< teaching-assistant <<<"


def app_data_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "xiaoshen"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "xiaoshen"
    return Path.home() / ".xiaoshen"


def _windows_known_folder(folder_id: str) -> Path | None:
    """Windows 的系統資料夾（被 OneDrive 接管時也找得到）；問不到回 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        guid = uuid.UUID(folder_id)

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        g = GUID(guid.time_low, guid.time_mid, guid.time_hi_version,
                 (ctypes.c_ubyte * 8)(*guid.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            try:
                return Path(out.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        pass
    return None


def desktop_dir() -> Path:
    """桌面資料夾。Windows 問系統（桌面被 OneDrive 接管時也找得到）。"""
    if IS_WINDOWS:
        found = _windows_known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")  # FOLDERID_Desktop
        if found:
            return found
    return Path.home() / "Desktop"


def documents_dir() -> Path:
    """「文件」資料夾（第一次啟動選資料夾位置時從這裡開始）。Windows 問系統。"""
    if IS_WINDOWS:
        found = _windows_known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7")  # FOLDERID_Documents
        if found:
            return found
    return Path.home() / "Documents"


def default_browser() -> Path | None:
    """Edge 優先、其次 Chrome；都沒有回 None。"""
    if IS_WINDOWS:
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates = [Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                      Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                      Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
                      Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe"]
    elif IS_MAC:
        candidates = [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                      Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    else:
        candidates = []
    return next((c for c in candidates if c.exists()), None)


def kill_profile_processes(profile: Path) -> None:
    """結束用這個設定檔開的所有瀏覽器（包括上次沒關乾淨、殘留的）。只動本程式專用的設定檔。"""
    profile = Path(profile)
    try:
        if IS_WINDOWS:
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -like '*{profile.name}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15)
        elif IS_MAC:
            subprocess.run(["pkill", "-f", "--", f"--user-data-dir={profile}"], capture_output=True, timeout=15)
    except Exception:
        pass


def mac_rects_from_frames(frames: list[tuple[float, float, float, float]], main_height: float) -> list[Rect]:
    """NSScreen 的 (x, y, w, h)（左下原點、y 往上）轉成 (left, top, right, bottom)（左上原點）。
    frames 第一個是主螢幕；main_height 是主螢幕高度（兩種座標系共用的基準線）。"""
    out: list[Rect] = []
    for x, y, w, h in frames:
        top = main_height - (y + h)
        out.append((round(x), round(top), round(x + w), round(top + h)))
    return out


def monitor_rects() -> list[Rect]:
    """所有螢幕的 (left, top, right, bottom)，主螢幕排第一。"""
    if IS_MAC:
        from AppKit import NSScreen  # 只在 Mac 才有

        frames = [(f.origin.x, f.origin.y, f.size.width, f.size.height)
                  for f in (s.frame() for s in NSScreen.screens())]
        return mac_rects_from_frames(frames, frames[0][3]) if frames else []
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes

    rects: list[tuple[bool, Rect]] = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmon, hdc, lprect, lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        rects.append((bool(info.dwFlags & 1), (r.left, r.top, r.right, r.bottom)))
        return 1

    ctypes.windll.user32.SetProcessDPIAware()
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    rects.sort(key=lambda pr: not pr[0])
    return [r for _, r in rects]


def bundled_tool(name: str) -> Path | None:
    """安裝腳本放在 REPO_ROOT/tools 下的執行檔；沒有就回 None。name：cloudflared 或 node。"""
    from .config import REPO_ROOT

    rel = {"cloudflared": ("cloudflared.exe",) if IS_WINDOWS else ("cloudflared",),
           "node": ("node", "node.exe") if IS_WINDOWS else ("node", "bin", "node")}.get(name)
    if rel is None:
        raise ValueError(f"不認得的工具：{name}")
    path = REPO_ROOT.joinpath("tools", *rel)
    return path if path.exists() else None


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def upsert_env_block(text: str, name: str, value: str) -> str:
    """在 shell 設定檔文字裡本程式的區塊設定 export NAME='value'；區塊不存在就加在最後，存在就只換那一行。"""
    line = f"export {name}={_shell_quote(value)}"
    lines = text.split("\n") if text else []
    if text.endswith("\n"):
        lines.pop()
    if ENV_BEGIN in lines and ENV_END in lines[lines.index(ENV_BEGIN):]:
        b = lines.index(ENV_BEGIN)
        e = lines.index(ENV_END, b)
        body = [ln for ln in lines[b + 1:e] if not ln.startswith(f"export {name}=")]
        lines[b + 1:e] = body + [line]
    else:
        lines += [ENV_BEGIN, line, ENV_END]
    return "\n".join(lines) + "\n"


def save_user_env(name: str, value: str) -> None:
    """存成使用者環境變數：Windows 寫登錄檔並通知系統；Mac 寫進 ~/.zprofile 裡本程式的區塊。"""
    if IS_MAC:
        path = Path.home() / ".zprofile"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(upsert_env_block(old, name, value), encoding="utf-8")
        return
    if not IS_WINDOWS:
        raise NotImplementedError("只支援 Windows 與 macOS")
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))

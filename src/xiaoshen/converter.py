"""「簡報\\」裡的 .pptx／.ppt 自動轉成同名 PDF。

規則：沒有同名 PDF，或 pptx 比 PDF 新，才轉檔並覆蓋；PDF 比較新就不動。
轉好的 PDF 修改時間設成與原檔相同，「最新簡報」仍依 pptx 的修改時間排序。
轉檔先寫暫存檔再換名，失敗時不會留下半份 PDF。
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .platform_support import IS_MAC

SOURCE_SUFFIXES = (".pptx", ".ppt")
PP_SAVE_AS_PDF = 32  # PowerPoint ppSaveAsPDF
MAC_POWERPOINT = Path("/Applications/Microsoft PowerPoint.app")
MAC_CONVERT_TIMEOUT = 120  # 秒

Converter = Callable[[Path, Path], None]

# 同一時間只轉一份：PowerPoint 同時被兩個執行緒操作會互相關掉，暫存檔也會互相覆蓋
_CONVERT_LOCK = threading.Lock()


@dataclass
class ConversionReport:
    attempted: list[str] = field(default_factory=list)
    converted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # 只含「第一次」失敗的版本


def find_sources(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in SOURCE_SUFFIXES and not p.name.startswith("~$") and p.is_file()
    )


def needs_conversion(src: Path) -> bool:
    pdf = src.with_suffix(".pdf")
    return not pdf.exists() or src.stat().st_mtime > pdf.stat().st_mtime + 1


def convert_pending(folder: Path, convert: Converter,
                    already_reported: set | None = None) -> ConversionReport:
    """轉換所有需要轉的檔案。already_reported 用來避免同一版壞檔每 30 秒重複警告。"""
    with _CONVERT_LOCK:
        return _convert_pending(folder, convert, already_reported)


def _convert_pending(folder: Path, convert: Converter, already_reported: set | None) -> ConversionReport:
    report = ConversionReport()
    reported = already_reported if already_reported is not None else set()
    for src in find_sources(folder):
        try:
            if not needs_conversion(src):
                continue
            mtime = src.stat().st_mtime
        except OSError:
            continue  # 檔案剛好被移走或同步中
        report.attempted.append(src.name)
        try:
            _convert_one(src, convert, mtime)
            report.converted.append(src.name)
        except Exception as e:
            key = (src.name, mtime)
            if key not in reported:
                reported.add(key)
                report.failed.append((src.name, str(e) or e.__class__.__name__))
    return report


def _convert_one(src: Path, convert: Converter, mtime: float) -> Path:
    """轉成同名 PDF：先寫暫存檔再換名，失敗時不留半份 PDF（例外照樣丟出）。"""
    pdf = src.with_suffix(".pdf")
    tmp = src.with_name(f"~xiaoshen-{src.stem}.pdf")
    try:
        convert(src, tmp)
        os.replace(tmp, pdf)
        os.utime(pdf, (mtime, mtime))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return pdf


def convert_file(src: Path, convert: Converter) -> Path:
    """只轉這一份（主持人要播還沒轉的 pptx 時）；PDF 已是最新就直接回傳。失敗丟出例外。"""
    with _CONVERT_LOCK:  # 背景轉檔正在轉同一份時，等它轉完就不必再轉
        if needs_conversion(src):
            return _convert_one(src, convert, src.stat().st_mtime)
        return src.with_suffix(".pdf")


def list_deck_entries(folder: Path) -> list[Path]:
    """簡報清單：PDF，加上還沒轉（或改過）的 pptx／ppt，新的在前。

    pptx 改過時列 pptx、不列舊的同名 PDF；選到 pptx 時才轉檔（開機不轉）。"""
    if not folder.is_dir():
        return []
    pending = [p for p in find_sources(folder) if needs_conversion(p)]
    stale = {p.stem for p in pending}
    pdfs = [p for p in folder.glob("*.pdf") if not p.name.startswith("~") and p.stem not in stale]
    return sorted(pdfs + pending, key=lambda p: p.stat().st_mtime, reverse=True)


def powerpoint_available() -> bool:
    if IS_MAC:
        return MAC_POWERPOINT.exists()
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"PowerPoint.Application\CurVer"):
            return True
    except OSError:
        return False
    except ImportError:
        return False


def _applescript_quote(text: str) -> str:
    """AppleScript 字串字面值內容的跳脫：反斜線要先處理，再處理雙引號與換行。"""
    return (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\r", "\\r").replace("\n", "\\n"))


def mac_export_script(src: Path, dst: Path) -> str:
    """Mac 用的 AppleScript：把 src 存成 PDF 到 dst。

    - 先用完整路徑找「已經開著」的同一份簡報（使用者可能正在編輯）：找到就直接匯出，
      絕不關它、也不結束 PowerPoint；沒找到才自己 open，並在 open 之後依完整路徑找回
      那份簡報（不用 active presentation）。
    - 只關掉自己開的那份；PowerPoint 原本沒開才在最後結束它。
    - 整段包在一個 try 裡：任何一步出錯都先收拾（同樣只關自己開的、只在原本沒開時結束），
      再把錯誤丟回去，osascript 會以非 0 結束並在 stderr 帶出原因。
    """
    s, d = _applescript_quote(str(src)), _applescript_quote(str(dst))
    n = _applescript_quote(Path(src).name)

    def find(cond: str, indent: int) -> list[str]:
        pad = " " * indent
        return [
            f'{pad}repeat with p in presentations',
            f'{pad}    if {cond} then',
            f'{pad}        set pres to p',
            f'{pad}        exit repeat',
            f'{pad}    end if',
            f'{pad}end repeat',
        ]

    # 完整路徑（POSIX 或 HFS 形式）或檔名相同，都當成同一份；寧可把別資料夾的同名檔當成使用者的（不關它）
    by_path = "(full name of p) is srcPath or (full name of p) is srcHfs"
    by_any = by_path + " or (name of p) is srcName"
    return "\n".join([
        f'set srcPath to "{s}"',
        f'set srcName to "{n}"',
        f'set dstPath to "{d}"',
        'set wasRunning to application "Microsoft PowerPoint" is running',
        'set openedByUs to false',
        'set pres to missing value',
        'try',
        '    set srcHfs to (POSIX file srcPath) as text',
        '    tell application "Microsoft PowerPoint"',
        *find(by_any, 8),
        '        if pres is missing value then',
        '            set countBefore to count of presentations',
        '            open POSIX file srcPath',
        '            set openedByUs to true',
        *find(by_path, 12),
        '            if pres is missing value then',
        *find("(name of p) is srcName", 16),
        '            end if',
        '            if pres is missing value then set pres to active presentation',
        '            if (count of presentations) is countBefore then set openedByUs to false',
        '        end if',
        '        if pres is missing value then error "PowerPoint 找不到剛開啟的簡報" number -1',
        '        save pres in POSIX file dstPath as save as PDF',
        '        if openedByUs then',
        '            close pres saving no',
        '            set openedByUs to false',
        '        end if',
        '        if not wasRunning then quit',
        '    end tell',
        'on error errMsg number errNum',
        '    try',
        '        tell application "Microsoft PowerPoint"',
        '            if openedByUs and pres is not missing value then close pres saving no',
        '            if not wasRunning then quit',
        '        end tell',
        '    end try',
        '    error errMsg number errNum',
        'end try',
    ])


def _mac_convert(src: Path, dst: Path, run) -> None:
    script = mac_export_script(src.resolve(), dst.resolve())
    try:
        proc = run(["osascript", "-e", script], capture_output=True, text=True,
                   timeout=MAC_CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"PowerPoint 轉檔逾時（超過 {MAC_CONVERT_TIMEOUT} 秒）") from None
    if proc.returncode != 0:
        raise RuntimeError("PowerPoint 轉檔失敗：" + ((proc.stderr or "").strip() or f"osascript 結束碼 {proc.returncode}"))
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError("PowerPoint 沒有產生 PDF")


def powerpoint_convert(src: Path, dst: Path, run=subprocess.run) -> None:
    """用本機 PowerPoint 轉 PDF。可在任何執行緒呼叫；不會關掉使用者正在用的 PowerPoint。"""
    if IS_MAC:
        _mac_convert(src, dst, run)
        return
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    started_by_us = False
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        started_by_us = app.Presentations.Count == 0
        pres = app.Presentations.Open(str(src.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
        try:
            pres.SaveAs(str(dst.resolve()), PP_SAVE_AS_PDF)
        finally:
            pres.Close()
        if not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError("PowerPoint 沒有產生 PDF")
    finally:
        # 只關掉我們自己叫起來的 PowerPoint；它關閉最後一份簡報後可能已自行結束，所以不再查詢狀態
        if app is not None and started_by_us:
            try:
                app.Quit()
            except Exception:
                pass
        app = None
        pythoncom.CoUninitialize()

"""第一次啟動：一個設定視窗，問資料夾位置、名字、喚醒詞、科別與會議類型。

還沒設定 data_root 時跳出視窗；在選的位置建立資料夾與五個子資料夾，
把五個設定寫進 config.toml，之後不再問。驗證規則全部沿用 settings_store.validate。
"""

from __future__ import annotations

import os
import sys
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import persona
from .config import FOLDERS, REPO_ROOT
from .platform_support import documents_dir
from .settings_store import upsert_keys, validate

FOLDER_NAME = "教學助理資料"
CONFIG_PATH = REPO_ROOT / "config.toml"


@dataclass
class FirstRunAnswers:
    folder: Path
    display_name: str
    wake_word: str
    specialty: str
    meeting_type: str
    meeting_type_other: str


def _configured(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    try:
        return "data_root" in tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return True  # 設定檔有錯：不動它，交給 load_config 報錯


def needs_data_folder(config_path: Path = CONFIG_PATH) -> bool:
    return not _configured(config_path)


def target_folder(chosen: Path) -> Path:
    return chosen if chosen.name == FOLDER_NAME else chosen / FOLDER_NAME


def picker_start_dir() -> Path:
    """選資料夾的視窗從「文件」開始（不從安裝資料夾開始）；沒有「文件」就從使用者資料夾開始。"""
    docs = documents_dir()
    return docs if docs.is_dir() else Path.home()


def suggested_folder() -> Path:
    return picker_start_dir() / FOLDER_NAME


def _inside_install_folder(folder: Path) -> bool:
    """資料夾是安裝資料夾本身或在它裡面（更新時會整個換新、移除時會被刪掉）。相對路徑以目前目錄（安裝資料夾）為準。"""
    target = Path(os.path.normcase(os.path.abspath(target_folder(Path(folder)).resolve())))
    install = Path(os.path.normcase(os.path.abspath(REPO_ROOT.resolve())))
    return target == install or target.is_relative_to(install)


_PICK_SCRIPT = (
    "import sys, tkinter as tk\n"
    "from tkinter import filedialog\n"
    "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
    "p = filedialog.askdirectory(title='選擇資料夾（可在視窗裡按「新增資料夾」建立）', initialdir=sys.argv[1], mustexist=False)\n"
    "sys.stdout.reconfigure(encoding='utf-8'); print(p or '')\n"
)


def pick_folder_dialog(start: Path, runner=subprocess.run) -> str:
    """跳出系統的選資料夾視窗，回傳選到的路徑；取消、失敗或逾時回空字串。
    另開一個小程式跑：Mac 的視窗只能在程式的主執行緒開，伺服器的請求不在主執行緒上。"""
    try:
        proc = runner([sys.executable, "-c", _PICK_SCRIPT, str(start)], capture_output=True, text=True,
                      encoding="utf-8", timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def inside_install_folder(folder: Path) -> bool:
    """這個資料夾本身就是安裝資料夾或在它裡面（更新、移除程式時會被刪掉）。"""
    target = Path(os.path.normcase(os.path.abspath(Path(folder).resolve())))
    install = Path(os.path.normcase(os.path.abspath(REPO_ROOT.resolve())))
    return target == install or target.is_relative_to(install)


def save_data_root(config_path: Path, root: Path) -> None:
    if not config_path.exists():
        config_path.write_text("# 設定檔（不進 git）。其他欄位見 config.example.toml。\n", encoding="utf-8")
    upsert_keys(config_path, {"data_root": str(root)})


def _setting_values(a: FirstRunAnswers) -> dict[str, str]:
    return {"display_name": a.display_name.strip(), "wake_word": a.wake_word.strip(),
            "specialty": a.specialty.strip(), "meeting_type": a.meeting_type,
            "meeting_type_other": a.meeting_type_other.strip()}


def validate_answers(a: FirstRunAnswers) -> dict[str, str]:
    """{欄位: 錯誤原因}；空字典＝通過。設定欄位沿用 settings_store.validate，另外檢查資料夾。"""
    errors = validate(_setting_values(a))
    if not a.folder or not str(a.folder).strip():
        errors["folder"] = "請選擇資料夾位置"
    elif _inside_install_folder(a.folder):
        errors["folder"] = (f"資料夾不能放在安裝資料夾（{REPO_ROOT}）裡面：更新或移除程式時會被刪除。"
                            "請選安裝資料夾以外的位置，例如「文件」。")
    return errors


def apply_first_run(config_path: Path, a: FirstRunAnswers) -> Path:
    root = target_folder(Path(a.folder))
    for name in FOLDERS:
        (root / name).mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text("# 設定檔（不進 git）。其他欄位見 config.example.toml。\n", encoding="utf-8")
    # data_root 是「已完成」的標記：和其它五個設定放在同一次寫入，不會只存一半
    upsert_keys(config_path, {"data_root": str(root), **_setting_values(a)})
    return root


def ask_first_run(apply: Callable[[FirstRunAnswers], object] | None = None) -> FirstRunAnswers | None:
    """tkinter 單一視窗。確定時驗證，錯誤顯示在視窗內不關閉；取消或關窗回傳 None。

    給 apply 時，驗證通過後在視窗內執行（建資料夾、寫設定）；OSError 顯示在視窗內，視窗不關、已填的內容保留。"""
    import tkinter as tk
    from tkinter import filedialog, ttk

    win = tk.Tk()
    win.title("教學助理：第一次使用")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    result: list[FirstRunAnswers | None] = [None]

    folder_v = tk.StringVar(value=str(suggested_folder()))  # 建議：文件／教學助理資料
    name_v = tk.StringVar(value="賈維斯")
    wake_v = tk.StringVar()
    spec_v = tk.StringVar()
    type_v = tk.StringVar(value=persona.DEFAULT_MEETING_TYPE)
    other_v = tk.StringVar()
    err_v = tk.StringVar()

    frm = ttk.Frame(win, padding=16)
    frm.grid(sticky="nsew")
    ttk.Label(frm, text="請設定資料夾位置與助理的基本資料（之後可在「設定」畫面修改名字等欄位）。",
              wraplength=460).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

    ttk.Label(frm, text="資料夾位置").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=folder_v, width=40).grid(row=1, column=1, sticky="we", pady=4)

    def choose() -> None:
        picked = filedialog.askdirectory(title=f"選擇要放「{FOLDER_NAME}」資料夾的位置", mustexist=True, parent=win,
                                         initialdir=str(picker_start_dir()))
        if picked:
            folder_v.set(picked)

    ttk.Button(frm, text="選擇…", command=choose).grid(row=1, column=2, padx=(6, 0))
    ttk.Label(frm, text=f"會在裡面建立「{FOLDER_NAME}」，放簡報、參考資料、題庫和紀錄；可選雲端同步的資料夾。",
              wraplength=460, foreground="#555").grid(row=2, column=0, columnspan=3, sticky="w")

    ttk.Label(frm, text="助理名字").grid(row=3, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=name_v, width=20).grid(row=3, column=1, sticky="w", pady=4)
    ttk.Label(frm, text="喚醒詞").grid(row=4, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=wake_v, width=20).grid(row=4, column=1, sticky="w", pady=4)
    ttk.Label(frm, text="2 到 4 個中文字；留空就用名字", foreground="#555").grid(row=4, column=2, sticky="w", padx=(6, 0))
    ttk.Label(frm, text="科別").grid(row=5, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=spec_v, width=20).grid(row=5, column=1, sticky="w", pady=4)
    ttk.Label(frm, text="會議類型").grid(row=6, column=0, sticky="w", pady=4)
    combo = ttk.Combobox(frm, textvariable=type_v, values=list(persona.MEETING_TYPES), state="readonly", width=24)
    combo.grid(row=6, column=1, sticky="w", pady=4)
    ttk.Label(frm, text="其它類型").grid(row=7, column=0, sticky="w", pady=4)
    other_entry = ttk.Entry(frm, textvariable=other_v, width=24, state="disabled")
    other_entry.grid(row=7, column=1, sticky="w", pady=4)

    def sync_other(*_args) -> None:
        other_entry.configure(state="normal" if type_v.get() == persona.OTHER else "disabled")

    combo.bind("<<ComboboxSelected>>", sync_other)

    ttk.Label(frm, textvariable=err_v, foreground="#b00020", wraplength=460, justify="left").grid(
        row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def current() -> FirstRunAnswers:
        other = other_v.get() if type_v.get() == persona.OTHER else ""
        return FirstRunAnswers(Path(folder_v.get().strip()) if folder_v.get().strip() else None,  # type: ignore[arg-type]
                               name_v.get(), wake_v.get(), spec_v.get(), type_v.get(), other)

    def ok() -> None:
        a = current()
        errors = validate_answers(a)
        if errors:
            err_v.set("\n".join(errors.values()))
            return
        if apply is not None:
            try:
                apply(a)
            except OSError as exc:
                err_v.set(f"無法建立資料夾或寫入設定：{exc}\n請換一個位置再試。")
                return
        result[0] = a
        win.destroy()

    btns = ttk.Frame(frm)
    btns.grid(row=9, column=0, columnspan=3, sticky="e", pady=(12, 0))
    ttk.Button(btns, text="確定", command=ok).pack(side="right")
    ttk.Button(btns, text="取消", command=win.destroy).pack(side="right", padx=(0, 8))
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.bind("<Return>", lambda _e: ok())
    win.bind("<Escape>", lambda _e: win.destroy())
    win.mainloop()
    return result[0]


def ensure_data_folder(config_path: Path = CONFIG_PATH,
                       ask: Callable[[], FirstRunAnswers | None] | None = None) -> bool:
    """需要時請使用者填設定並建立資料夾。回傳 False＝使用者取消或建立失敗（程式不啟動，不丟例外）。"""
    if not needs_data_folder(config_path):
        return True
    applied: list[Path] = []
    if ask is None:  # 預設視窗：在視窗內建立，失敗時留在視窗讓使用者改
        answers = ask_first_run(lambda a: applied.append(apply_first_run(config_path, a)))
    else:
        answers = ask()
    if answers is None:
        return False
    if not applied:
        try:
            applied.append(apply_first_run(config_path, answers))
        except OSError as exc:
            print(f"無法建立資料夾或寫入設定：{exc}", flush=True)
            return False
    print(f"已建立資料夾：{applied[-1]}（記在 config.toml 的 data_root）", flush=True)
    return True

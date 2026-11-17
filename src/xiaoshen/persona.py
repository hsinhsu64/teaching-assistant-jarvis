"""助理名稱、專科與會議類型：決定 AI 提示詞、標籤與頁尾的用字。

名稱預設「賈維斯」，使用者可改；啟動時 launcher 呼叫 set_persona，其他模組用 current() 讀取。
聽眾一律稱「與會人員」。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .config import Config

MEETING_TYPES: tuple[str, ...] = ("病例討論", "Journal reading", "病歷教學", "Mortality & Morbidity", "VS teaching", "其它")
AUDIENCE = "與會人員"
OTHER = "其它"
DEFAULT_MEETING_TYPE = "病例討論"


@dataclass
class Persona:
    display_name: str = "賈維斯"
    specialty: str = ""
    meeting_type: str = DEFAULT_MEETING_TYPE
    meeting_type_other: str = ""

    @classmethod
    def from_config(cls, c: Config) -> "Persona":
        mt = c.meeting_type if c.meeting_type in MEETING_TYPES else DEFAULT_MEETING_TYPE
        return cls(c.display_name, c.specialty, mt, c.meeting_type_other)


def meeting_label(p: Persona) -> str:
    """前五項回傳本身；「其它」回傳自訂名稱（可能是空字串）。"""
    if p.meeting_type == OTHER:
        return p.meeting_type_other.strip()
    return p.meeting_type


def context(p: Persona) -> str:
    """提示詞裡的場合描述，例如「兒科腎臟科病例討論」。專科與會議都沒填時是「醫學教學」。"""
    spec = p.specialty.strip()
    label = meeting_label(p)
    if not label:
        return f"{spec}醫學教學" if spec else "醫學教學"
    if spec and label[0].isascii():  # 中文接英文開頭的會議名稱時中間留一個空白
        return f"{spec} {label}"
    return f"{spec}{label}"


_lock = threading.Lock()
_current = Persona()
_wake = "賈維斯"  # 執行中的喚醒詞（和名字分開：名字超過 4 字或喚醒詞退回預設時兩者不同）


def current() -> Persona:
    with _lock:
        return _current


def set_persona(p: Persona) -> None:
    global _current
    with _lock:
        _current = p


def wake_word() -> str:
    with _lock:
        return _wake


def set_wake_word(word: str) -> None:
    """語音建好偵測器時設成實際生效的詞（退回預設時是預設詞）。"""
    global _wake
    with _lock:
        _wake = word


def start_from_config(c: Config) -> None:
    """啟動時：名稱、專科、會議類型，與設定的有效喚醒詞（語音建好偵測器後改成實際用的詞）。"""
    set_persona(Persona.from_config(c))
    set_wake_word(c.effective_wake_word)


def app_info() -> dict:
    """畫面用語：助理名稱、喚醒詞（都是執行中的）與聽眾稱呼，放進 snapshot 的 app 欄位。"""
    return {"name": current().display_name, "wake": wake_word(), "audience": AUDIENCE}

"""config.toml 的頂層鍵讀寫與「設定」畫面欄位的驗證。

寫入只動指定的鍵：檔案其他內容（註解、其他鍵、表格）原樣保留。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .persona import MEETING_TYPES
from .wakeword import validate_wake_word

NAME_MAX = 10
_TABLE = re.compile(r"^[ \t]*\[", re.M)


def toml_string(value: str) -> str:
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"  # 字面字串：Windows 路徑的反斜線不用跳脫
    return json.dumps(value, ensure_ascii=False)  # JSON 的跳脫寫法也是合法的 TOML 字串


def upsert_keys(path: Path, values: dict[str, str]) -> None:
    """逐一取代或新增頂層鍵（字串值）。新增的放在第一個 [表格] 之前，才不會變成表格裡的欄位。"""
    text = path.read_bytes().decode("utf-8") if path.exists() else ""
    eol = "\r\n" if "\r\n" in text else "\n"
    for key, value in values.items():
        line = f"{key} = {toml_string(value)}"
        table = _TABLE.search(text)
        at = table.start() if table else len(text)
        head, tail = text[:at], text[at:]
        pattern = re.compile(rf"^{re.escape(key)}[ \t]*=[^\r\n]*", re.M)
        if pattern.search(head):
            head = pattern.sub(lambda m: line, head, count=1)
        else:
            head += ("" if not head or head.endswith("\n") else eol) + line + eol
        text = head + tail
    _atomic_write(path, text.encode("utf-8"))


def _atomic_write(path: Path, data: bytes) -> None:
    """先寫暫存檔再一次換上：寫到一半當掉或斷電時，原本的 config.toml 仍完整。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def validate(values: dict) -> dict[str, str]:
    """回傳 {欄位: 錯誤原因}；空字典＝通過。"""
    errors: dict[str, str] = {}
    name = str(values.get("display_name", "")).strip()
    wake = str(values.get("wake_word", "")).strip()
    if not 1 <= len(name) <= NAME_MAX:
        errors["display_name"] = f"名字要 1 到 {NAME_MAX} 個字"
    if wake:
        reason = validate_wake_word(wake)
        if reason:
            errors["wake_word"] = reason
    elif "display_name" not in errors:
        reason = validate_wake_word(name)
        if reason:  # 空白＝同名字，但這個名字不能當喚醒詞
            errors["wake_word"] = f"名字不能當喚醒詞（{reason}），請另外填喚醒詞"
    if values.get("meeting_type") not in MEETING_TYPES:
        errors["meeting_type"] = "請選擇會議類型"
    for key in DEVICE_KEYS:
        device = values.get(key)
        if device is not None and (len(device) > DEVICE_MAX or "\n" in device or "\r" in device):
            errors[key] = "裝置名稱不正確，請從選單重新選擇"
    sensitivity = values.get("wake_sensitivity")
    if sensitivity is not None and sensitivity not in SENSITIVITY_LABELS:
        errors["wake_sensitivity"] = "請從選單選擇喚醒靈敏度"
    return errors


SENSITIVITY_LABELS = {"low": "低（較少誤叫醒）", "medium": "中（預設）", "high": "高（較少漏聽）"}


def sensitivity_options() -> list[dict]:
    return [{"value": k, "label": v} for k, v in SENSITIVITY_LABELS.items()]


DEVICE_KEYS = ("audio_device", "speaker_device")
DEVICE_MAX = 100


def device_options(saved: str, names: list[str]) -> list[dict]:
    """麥克風／喇叭選單：「電腦預設」在最前面。

    已存的值是名稱片段（例如 USBMIC）時，對到的那一項沿用原值，存檔不會改寫設定；
    已存的裝置目前沒接上也照樣列出，不會被悄悄換掉。"""
    options = [{"value": "", "label": "電腦預設"}]
    match = next((n for n in names if saved and saved.lower() in n.lower()), None)
    if saved and match is None:
        options.append({"value": saved, "label": f"{saved}（目前沒有接上）"})
    options += [{"value": saved if n == match else n, "label": n} for n in names]
    return options

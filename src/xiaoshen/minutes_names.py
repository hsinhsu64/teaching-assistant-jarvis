"""主持人填過的講者名字（只存在筆電本機），下次當下拉選項，也用來預設取消勾選去識別。"""

from __future__ import annotations

import json
import re
from pathlib import Path

FILE = "known_names.json"
MAX_NAMES = 60
_DEFAULT = re.compile(r"^講者\s*\d+$")


def load(folder: Path) -> list[str]:
    try:
        data = json.loads((Path(folder) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(n) for n in data if isinstance(n, str) and n.strip()] if isinstance(data, list) else []


def remember(folder: Path, names: list[str]) -> list[str]:
    """新填的排前面、去重；「講者N」這種預設名字不記。"""
    new = []
    for n in names:
        n = (n or "").strip()
        if n and not _DEFAULT.match(n) and n not in new:
            new.append(n)
    if not new:
        return load(folder)
    merged = (new + [n for n in load(folder) if n not in new])[:MAX_NAMES]
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILE).write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return merged

"""診斷包：遠端測試者一鍵產生、再寄回來的 zip。

只放：程式版本與系統資訊、自我檢查結果、去識別的 config.toml、最近 7 天的事件記錄。
絕不放：Gemini 金鑰、環境變數、簡報與參考文獻。
"""

from __future__ import annotations

import json
import platform
import re
import sys
import tomllib
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from .config import REPO_ROOT

LOG_DAYS = 7
_LOG_NAME = re.compile(r"^events-(\d{4}-\d{2}-\d{2})\.jsonl$")
_KEY_LINE = re.compile(r"^\s*([^=#\s][^=]*?)\s*=\s*(.*)$")
_SECRET_WORDS = {"key", "apikey", "token", "cookie", "cookies", "secret", "password"}
_AIZA = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_KEY_PARAM = re.compile(r"(?i)\bkey=[^&\s\"'<>]+")


def _is_secret_key(key: str) -> bool:
    return any(part in _SECRET_WORDS for part in key.strip().strip("\"'").lower().split("_"))


def _last_component(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1]


def _data_roots(config_path: Path, extra: list) -> list[str]:
    roots = [str(r) for r in extra if r]
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8")).get("data_root")
        if isinstance(raw, str) and raw:
            roots.append(raw)
    except (OSError, tomllib.TOMLDecodeError):
        pass
    variants = set()
    for r in roots:
        variants.update({r, r.replace("\\", "/"), r.replace("/", "\\")})
    return sorted((v for v in variants if _last_component(v)), key=len, reverse=True)


def scrub(value, roots: list[str]):
    """遞迴清掉字串裡的個資與金鑰：完整 data_root 只留最後一層；key=… 與 AIza… 遮成 ***。"""
    if isinstance(value, str):
        for r in roots:
            value = value.replace(r, _last_component(r))
        value = _AIZA.sub("***", value)
        return _KEY_PARAM.sub("key=***", value)
    if isinstance(value, dict):
        return {k: scrub(v, roots) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, roots) for v in value]
    return value


def program_version() -> str:
    try:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


def redact_config(text: str) -> str:
    """逐行處理：data_root 只留最後一層；鍵名含 key／token／cookie 的行整行刪除。"""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = _KEY_LINE.match(line)
        if m:
            key = m.group(1).strip().strip("\"'").lower()
            if _is_secret_key(key):
                continue
            if key == "data_root":
                value = m.group(2).strip()
                quote = value[0] if value[:1] in ("'", '"') else ""
                inner = value.strip("'\"")
                last = re.split(r"[\\/]", inner.rstrip("\\/"))[-1]
                eol = line[len(line.rstrip("\r\n")):]
                line = f"data_root = {quote}{last}{quote}{eol}"
        out.append(line)
    return "".join(out)


def _recent_logs(logs_dir: Path, now: datetime) -> list[Path]:
    if not logs_dir.is_dir():
        return []
    cutoff = (now - timedelta(days=LOG_DAYS)).date()
    found = []
    for p in sorted(logs_dir.iterdir()):
        m = _LOG_NAME.match(p.name)
        if not m or not p.is_file():
            continue
        try:
            day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff <= day <= now.date():
            found.append(p)
    return found


def build_bundle(dest_dir: Path, checks: list[dict], config_path: Path, logs_dir: Path, now: datetime,
                 data_root=None) -> Path:
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"診斷包-{now:%Y%m%d-%H%M}.zip"
    about = {"version": program_version(), "platform": platform.platform(), "python": sys.version,
             "generated_at": now.isoformat(timespec="seconds")}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about.json", json.dumps(about, ensure_ascii=False, indent=2))
        safe = scrub(json.loads(json.dumps(checks, ensure_ascii=False, default=str)),
                     _data_roots(config_path, [data_root]))
        z.writestr("selfcheck.json", json.dumps(safe, ensure_ascii=False, indent=2))
        if Path(config_path).is_file():
            z.writestr("config.toml", redact_config(Path(config_path).read_text(encoding="utf-8")))
        for log in _recent_logs(Path(logs_dir), now):
            z.write(log, log.name)
    return target

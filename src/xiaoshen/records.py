"""收題時把題目與統計寫進 紀錄\\（匿名，不含個別作答者）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .quiz import QuizRun


def save_record(folder: Path, deck_name: str, run: QuizRun) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    closed = run.closed_at or datetime.now()
    path = folder / f"{closed:%Y-%m-%d_%H%M%S}_{deck_name}_{run.kind}.json"
    data = {
        "deck": deck_name,
        "kind": run.kind,
        "opened_at": run.opened_at.isoformat(timespec="seconds"),
        "closed_at": closed.isoformat(timespec="seconds"),
        "questions": [qn.to_dict() for qn in run.questions],
        "stats": run.stats(),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

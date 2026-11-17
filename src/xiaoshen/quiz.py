"""作答引擎：單題（single）與考卷（exam）的作答、統計、收題與三種視圖。

視圖分三種受眾，差別在能看到什麼：
- display（投影幕）：開放中絕不含正解、解析、答對率
- host（主持人畫面）：永遠含正解、解析、出處
- phone（手機）：開放中只看自己的作答；收題後看自己的對錯與解析
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

MIN_OPTIONS, MAX_OPTIONS = 2, 6


@dataclass
class Question:
    id: str
    stem: str
    options: list[str]
    answer: int
    explanation: str = ""
    source: str = ""

    def __post_init__(self):
        if not MIN_OPTIONS <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"選項數需介於 {MIN_OPTIONS}–{MAX_OPTIONS}：{len(self.options)}")
        if not 0 <= self.answer < len(self.options):
            raise ValueError(f"正解超出選項範圍：{self.answer}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stem": self.stem,
            "options": list(self.options),
            "answer": self.answer,
            "explanation": self.explanation,
            "source": self.source,
        }


class QuizClosed(Exception):
    pass


class InvalidAnswer(Exception):
    pass


@dataclass
class _Submission:
    answers: dict[str, int] = field(default_factory=dict)
    final: bool = False


class QuizRun:
    def __init__(self, kind: Literal["single", "exam"], questions: list[Question]):
        if kind not in ("single", "exam"):
            raise ValueError(f"未知的作答類型：{kind}")
        if kind == "single" and len(questions) != 1:
            raise ValueError("單題作答必須剛好一題")
        if not questions:
            raise ValueError("至少需要一題")
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.questions = list(questions)
        self.status: Literal["open", "closed"] = "open"
        self.opened_at = datetime.now()
        self.closed_at: datetime | None = None
        self._by_id = {qn.id: qn for qn in self.questions}
        self._subs: dict[str, _Submission] = {}

    # ---- 作答 ----

    def submit(self, client_id: str, answers: dict[str, int], final: bool) -> None:
        if self.status != "open":
            raise QuizClosed("作答已截止")
        for qid, choice in answers.items():
            qn = self._by_id.get(qid)
            if qn is None:
                raise InvalidAnswer(f"沒有這一題：{qid}")
            if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(qn.options):
                raise InvalidAnswer(f"選項超出範圍：{qid}={choice}")
        sub = self._subs.setdefault(client_id, _Submission())
        sub.answers.update(answers)
        if final or self.kind == "single":
            sub.final = True

    def close(self) -> None:
        if self.status == "open":
            self.status = "closed"
            self.closed_at = datetime.now()

    # ---- 統計 ----

    def stats(self) -> dict:
        per_question = []
        for qn in self.questions:
            counts = [0] * len(qn.options)
            for sub in self._subs.values():
                if qn.id in sub.answers:
                    counts[sub.answers[qn.id]] += 1
            answered = sum(counts)
            per_question.append({
                "qid": qn.id,
                "counts": counts,
                "answered": answered,
                "correct_rate": counts[qn.answer] / answered if answered else None,
            })
        return {
            "participants": len(self._subs),
            "submitted": sum(1 for s in self._subs.values() if s.final),
            "per_question": per_question,
        }

    # ---- 視圖 ----

    def _base(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status}

    def view_for_display(self) -> dict:
        stats = self.stats()
        per_q = {p["qid"]: p for p in stats["per_question"]}
        closed = self.status == "closed"
        items = []
        for qn in self.questions:
            p = per_q[qn.id]
            item = {
                "id": qn.id,
                "stem": qn.stem,
                "options": list(qn.options),
                "counts": p["counts"],
                "answered": p["answered"],
            }
            if closed:
                item.update(
                    answer=qn.answer,
                    explanation=qn.explanation,
                    source=qn.source,
                    correct_rate=p["correct_rate"],
                )
            items.append(item)
        if closed and self.kind == "exam":
            # 答對率低的排前面；沒人作答的排最後
            items.sort(key=lambda it: (it["correct_rate"] is None, it["correct_rate"] or 0))
        return {
            **self._base(),
            "participants": stats["participants"],
            "submitted": stats["submitted"],
            "questions": items,
        }

    def view_for_host(self) -> dict:
        stats = self.stats()
        per_q = {p["qid"]: p for p in stats["per_question"]}
        return {
            **self._base(),
            "participants": stats["participants"],
            "submitted": stats["submitted"],
            "questions": [{**qn.to_dict(), **per_q[qn.id]} for qn in self.questions],
        }

    def view_for_phone(self, client_id: str) -> dict:
        sub = self._subs.get(client_id, _Submission())
        closed = self.status == "closed"
        items = []
        for qn in self.questions:
            mine = sub.answers.get(qn.id)
            item = {"id": qn.id, "stem": qn.stem, "options": list(qn.options), "mine": mine}
            if closed:
                item.update(
                    answer=qn.answer,
                    explanation=qn.explanation,
                    correct=mine == qn.answer if mine is not None else None,
                )
            items.append(item)
        return {**self._base(), "submitted": sub.final, "questions": items}

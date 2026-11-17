import json

from xiaoshen.quiz import Question, QuizRun
from xiaoshen.records import save_record


def test_save_record_roundtrip(tmp_path):
    r = QuizRun("single", [Question("q1", "s", ["A", "B"], 1, "e", "src")])
    r.submit("a", {"q1": 1}, final=True)
    r.close()
    path = save_record(tmp_path / "紀錄", "Case_SRNS", r)
    assert path.name.endswith("_Case_SRNS_single.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["questions"][0]["source"] == "src" and data["stats"]["participants"] == 1
    assert data["deck"] == "Case_SRNS" and data["kind"] == "single"

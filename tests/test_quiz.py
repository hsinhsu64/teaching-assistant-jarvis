import json

import pytest

from xiaoshen.quiz import InvalidAnswer, Question, QuizClosed, QuizRun


def q(n: int) -> Question:
    return Question(f"q{n}", f"題目 {n}", ["A", "B", "C", "D"], 1, f"解析 {n}", f"出處 {n}")


def test_question_validation():
    with pytest.raises(ValueError):
        Question("x", "s", ["A"], 0)
    with pytest.raises(ValueError):
        Question("x", "s", ["A", "B"], 2)
    with pytest.raises(ValueError):
        Question("x", "s", list("ABCDEFG"), 0)


def test_single_requires_exactly_one_question():
    with pytest.raises(ValueError):
        QuizRun("single", [q(1), q(2)])


def test_single_counts_and_rate():
    r = QuizRun("single", [q(1)])
    r.submit("a", {"q1": 1}, final=False)
    r.submit("b", {"q1": 0}, final=False)
    s = r.stats()
    assert s["participants"] == 2 and s["submitted"] == 2
    assert s["per_question"][0]["counts"] == [1, 1, 0, 0]
    assert s["per_question"][0]["correct_rate"] == 0.5


def test_correct_rate_none_when_no_answers():
    assert QuizRun("single", [q(1)]).stats()["per_question"][0]["correct_rate"] is None


def test_resubmit_overwrites():
    r = QuizRun("single", [q(1)])
    r.submit("a", {"q1": 0}, final=True)
    r.submit("a", {"q1": 1}, final=True)
    assert r.stats()["participants"] == 1
    assert r.stats()["per_question"][0]["counts"] == [0, 1, 0, 0]


def test_exam_partial_save_merges_answers():
    r = QuizRun("exam", [q(1), q(2)])
    r.submit("a", {"q1": 1}, final=False)
    r.submit("a", {"q2": 0}, final=False)
    assert r.view_for_phone("a")["questions"][0]["mine"] == 1
    assert r.view_for_phone("a")["questions"][1]["mine"] == 0


def test_submit_after_close_rejected():
    r = QuizRun("single", [q(1)])
    r.close()
    with pytest.raises(QuizClosed):
        r.submit("a", {"q1": 1}, final=True)


def test_invalid_answer():
    r = QuizRun("exam", [q(1), q(2)])
    with pytest.raises(InvalidAnswer):
        r.submit("a", {"q9": 0}, final=False)
    with pytest.raises(InvalidAnswer):
        r.submit("a", {"q1": 7}, final=False)


def test_exam_unsubmitted_counted_on_close():
    r = QuizRun("exam", [q(1), q(2)])
    r.submit("a", {"q1": 1}, final=False)
    r.submit("b", {"q1": 1, "q2": 1}, final=True)
    assert r.stats()["submitted"] == 1
    r.close()
    assert r.stats()["per_question"][0]["answered"] == 2


def test_display_view_hides_answer_while_open():
    r = QuizRun("exam", [q(1)])
    r.submit("a", {"q1": 1}, final=True)
    blob = json.dumps(r.view_for_display(), ensure_ascii=False)
    assert '"answer"' not in blob and '"explanation"' not in blob and "correct_rate" not in blob


def test_display_view_after_close_sorted_by_rate():
    r = QuizRun("exam", [q(1), q(2)])
    r.submit("a", {"q1": 1, "q2": 0}, final=True)
    r.close()
    v = r.view_for_display()
    assert [x["id"] for x in v["questions"]] == ["q2", "q1"]
    assert v["questions"][0]["answer"] == 1 and v["questions"][0]["explanation"] == "解析 2"


def test_host_view_always_has_answers():
    v = QuizRun("single", [q(1)]).view_for_host()
    assert v["questions"][0]["answer"] == 1 and v["questions"][0]["source"] == "出處 1"


def test_phone_view_open_hides_answer():
    r = QuizRun("single", [q(1)])
    blob = json.dumps(r.view_for_phone("a"), ensure_ascii=False)
    assert '"answer"' not in blob and '"explanation"' not in blob


def test_phone_view_after_close_shows_own_correctness():
    r = QuizRun("single", [q(1)])
    r.submit("a", {"q1": 0}, final=True)
    r.close()
    v = r.view_for_phone("a")
    assert v["questions"][0]["mine"] == 0 and v["questions"][0]["correct"] is False
    assert v["questions"][0]["answer"] == 1


def test_bool_or_str_choice_rejected():
    r = QuizRun("single", [q(1)])
    with pytest.raises(InvalidAnswer):
        r.submit("a", {"q1": True}, final=True)
    with pytest.raises(InvalidAnswer):
        r.submit("a", {"q1": "1"}, final=True)

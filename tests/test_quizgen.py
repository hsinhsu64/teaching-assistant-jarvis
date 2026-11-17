import pytest

from xiaoshen.quizgen import generate_questions

from fakes import FakeLLM

PAGES = ["Intro page.", "We recommend calcineurin inhibitors as first-line therapy for SRNS."]
SOURCES = [("KDIGO 2021 GN", PAGES)]
QUOTE = "calcineurin inhibitors as first-line therapy for SRNS"


def q(stem="SRNS 一線？", options=("CYC", "CNI", "RTX", "MMF"), answer=1, quote=QUOTE, label="KDIGO 2021 GN", page=2):
    return {"stem": stem, "options": list(options), "answer_index": answer, "explanation_zh": "解析",
            "quote": quote, "source_label": label, "page": page}


def gen(items, n=3, **kw):
    llm = FakeLLM({"questions": items})
    return generate_questions(n, SOURCES, llm, "m", 100_000, **kw), llm


def test_invalid_questions_dropped_with_note():
    (qs, notes), _ = gen([q(), q(quote="fabricated quote not in source"), q(page=5), q(answer=7)], n=4)
    assert len(qs) == 1
    assert any("要求 4 題，通過驗證 1 題" in n for n in notes)


def test_duplicate_options_dropped():
    (qs, _), _ = gen([q(options=("CNI", "cni ", "RTX")), q()], n=2)
    assert len(qs) == 1


def test_n_out_of_range():
    with pytest.raises(ValueError):
        generate_questions(0, SOURCES, FakeLLM({}), "m", 100_000)
    with pytest.raises(ValueError):
        generate_questions(21, SOURCES, FakeLLM({}), "m", 100_000)


def test_too_large_raises_without_calling_model():
    llm = FakeLLM({})
    with pytest.raises(ValueError, match="資料太大"):
        generate_questions(3, SOURCES, llm, "m", 10)
    assert llm.calls == []


def test_focus_in_prompt():
    _, llm = gen([q()], n=1, focus="第 2 頁：CNI")
    p = llm.calls[0]["prompt"]
    assert "第 2 頁：CNI" in p and "1 題" in p and "[KDIGO 2021 GN 第 2 頁]" in p


def test_source_string_format():
    (qs, _), _ = gen([q()], n=1)
    x = qs[0]
    assert x.source == f"KDIGO 2021 GN 第 2 頁：{QUOTE}"[: len("KDIGO 2021 GN 第 2 頁：") + 80]
    assert x.id.startswith("g-") and len(x.id) == 10
    assert x.answer == 1 and x.explanation == "解析" and x.options[1] == "CNI"


def test_extra_questions_truncated_to_n():
    (qs, _), _ = gen([q(), q(), q()], n=2)
    assert len(qs) == 2

"""原文重點摘要簡報（spec §6.2b）：Flash 整理整份原文，每一點由程式逐字核對原文。"""

import pytest

from fakes import FakeLLM
from xiaoshen.llm import LLMError
from xiaoshen.source_text import SourceDoc
from xiaoshen.summary import SummaryError, summarize

P1 = "Glucocorticoids are the first-line treatment for children with nephrotic syndrome in most cases."
P2 = "Relapses should be treated with daily prednisone until complete remission for at least three days."
P3 = "Calcineurin inhibitors are recommended as the initial second-line therapy for steroid-resistant disease."


def _page(sentence):
    return f"Recommendation details. {sentence} Further practice points follow in the text."


PAGES = (_page(P1), _page(P2), _page(P3))


def doc(paged=True, pages=PAGES):
    return SourceDoc("KDIGO 2025", "[4] KDIGO", list(pages), paged=paged, full_text=True)


def run(llm, d=None, max_chars=600_000, chunk_chars=150_000):
    return summarize(d or doc(), llm, "gemini-3.5-flash", timeout=60, max_chars=max_chars, chunk_chars=chunk_chars)


def slide(title, *points):
    return {"title": title, "points": [{"zh": zh, "quotes": [q], "page": pg} for zh, q, pg in points]}


def test_points_verified_and_page_corrected():
    llm = FakeLLM({"slides": [slide("SRNS", ("CNI 為第二線首選", P3, 1))]})
    s = run(llm)
    pt = s.slides[0].points[0]
    assert pt.verified is True and pt.page == 3


def test_unverified_point_kept():
    llm = FakeLLM({"slides": [slide("初發", ("類固醇為第一線", P1, 1),
                                    ("捏造的點", "This sentence does not appear anywhere in the guideline text.", 2))]})
    s = run(llm)
    pts = s.slides[0].points
    assert len(pts) == 2
    assert pts[1].verified is False and pts[1].page is None
    assert s.unverified == 1


def test_unpaged_doc_has_no_page_numbers():
    llm = FakeLLM({"slides": [slide("重點", ("a", P1, 1), ("b", P3, 3))]})
    s = run(llm, doc(paged=False, pages=(" ".join(PAGES),)))
    assert all(p.verified for p in s.slides[0].points)
    assert all(p.page is None for p in s.slides[0].points)


def test_long_input_truncated_at_page_boundary():
    llm = FakeLLM({"slides": [slide("重點", ("a", P1, 1))]})
    s = run(llm, max_chars=len(PAGES[0]) + len(PAGES[1]) + 10)
    assert s.truncated_at == 2
    assert P3 not in llm.calls[0]["prompt"] and P2 in llm.calls[0]["prompt"]


class FirstCallBroken:
    """第一次（整份）輸出被截斷；之後每段正常。"""

    def __init__(self, per_chunk):
        self.per_chunk = list(per_chunk)
        self.calls = []

    def generate_json(self, model, system, prompt, schema, timeout=90, thinking=""):
        self.calls.append(prompt)
        if len(self.calls) == 1:
            raise LLMError("文字模型回傳的不是 JSON")
        return self.per_chunk.pop(0)


def test_chunked_retry_after_truncated_output():
    llm = FirstCallBroken([{"slides": [slide("第一段", ("a", P1, 1))]},
                           {"slides": [slide("第二段", ("b", P2, 2))]},
                           {"slides": [slide("第三段", ("c", P3, 3))]}])
    s = run(llm, chunk_chars=len(PAGES[0]) + 20)
    assert [sl.title for sl in s.slides] == ["第一段", "第二段", "第三段"]
    assert len(llm.calls) == 4 and P3 in llm.calls[3] and P1 not in llm.calls[3]


def test_timeout_is_not_retried_in_chunks():
    llm = FakeLLM(exc=LLMError("文字模型逾時（60 秒）"))
    with pytest.raises(SummaryError):
        run(llm)
    assert len(llm.calls) == 1


def test_empty_summary_is_error():
    with pytest.raises(SummaryError, match="沒有整理出重點"):
        run(FakeLLM({"slides": []}))
    with pytest.raises(SummaryError, match="沒有整理出重點"):
        run(FakeLLM({"slides": [{"title": "空", "points": []}]}))


def test_uses_summary_model():
    llm = FakeLLM({"slides": [slide("重點", ("a", P1, 1))]})
    s = run(llm)
    assert llm.calls[0]["model"] == "gemini-3.5-flash" and s.model == "gemini-3.5-flash"
    assert "[第 1 頁]" in llm.calls[0]["prompt"]


def test_config_has_summary_defaults():
    from xiaoshen.config import Config

    c = Config()
    assert c.model_summary == "gemini-3.1-pro-preview" and c.summary_timeout_seconds == 170  # 2026-11-18：改用 Pro
    assert c.summary_max_chars == 600_000 and c.summary_chunk_chars == 150_000


# ---- 2026-11-18 摘要品質：依類型整理、每點 1～3 句引文 ----

def multi(title, *points, doc_type="guideline"):
    return {"doc_type": doc_type,
            "slides": [{"title": title, "points": [{"zh": zh, "quotes": qs, "page": pg} for zh, qs, pg in points]}]}


def test_point_with_all_quotes_matching_is_verified():
    s = run(FakeLLM(multi("建議", ("初發用類固醇，復發用 prednisone", [P1, P2], 1))))
    pt = s.slides[0].points[0]
    assert pt.verified and pt.quotes == [P1, P2] and pt.page == 1


def test_point_with_one_bad_quote_is_unverified():
    s = run(FakeLLM(multi("建議", ("混了捏造", [P1, "This sentence is not in the guideline at all, really."], 1))))
    pt = s.slides[0].points[0]
    assert not pt.verified and pt.page is None and s.unverified == 1


def test_empty_quotes_unverified():
    s = run(FakeLLM(multi("建議", ("沒有引文", [], 1))))
    assert not s.slides[0].points[0].verified


def test_more_than_three_quotes_truncated_to_three():
    s = run(FakeLLM(multi("建議", ("四句", [P1, P2, P3, P1], 1))))
    assert s.slides[0].points[0].quotes == [P1, P2, P3]


def test_old_single_quote_field_still_accepted():
    s = run(FakeLLM({"slides": [{"title": "x", "points": [{"zh": "a", "quote": P3, "page": 3}]}]}))
    assert s.slides[0].points[0].verified and s.slides[0].points[0].quotes == [P3]


def test_prompt_lists_required_sections():
    from xiaoshen.summary import system_prompt
    text = system_prompt()
    for word in ("設計", "對象", "主要結果", "每一條建議", "證據等級", "quotes"):
        assert word in text


def test_doc_type_kept():
    assert run(FakeLLM(multi("建議", ("a", [P1], 1), doc_type="study"))).doc_type == "study"


# ---- 2026-11-19：摘要用 Pro、思考量低（實測 41 秒，Pro 預設 65 秒、Flash 46 秒） ----

def test_summarize_passes_thinking_level():
    llm = FakeLLM(multi("建議", ("a", [P1], 1)))
    summarize(doc(), llm, "pro", timeout=60, max_chars=600_000, chunk_chars=150_000, thinking="low")
    assert llm.calls[0]["thinking"] == "low"


def test_summarize_default_has_no_thinking_override():
    llm = FakeLLM(multi("建議", ("a", [P1], 1)))
    run(llm)
    assert llm.calls[0]["thinking"] == ""


def test_config_summary_thinking_low():
    from xiaoshen.config import Config
    assert Config().summary_thinking == "low"


def test_gemini_passes_thinking_only_when_given(monkeypatch):
    from xiaoshen.llm import GeminiLLM
    seen = []
    llm = GeminiLLM([])
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(llm, "_call", lambda m, s, p, sc, t, **k: seen.append(k.get("thinking", "")) or '{"ok": 1}')
    llm.generate_json("m", "s", "p", {}, timeout=30, thinking="low")
    llm.generate_json("m", "s", "p", {}, timeout=30)
    assert seen == ["low", ""]

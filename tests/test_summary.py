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
    return {"title": title, "points": [{"zh": zh, "quote": q, "page": pg} for zh, q, pg in points]}


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

    def generate_json(self, model, system, prompt, schema, timeout=90):
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
    assert c.model_summary == "gemini-3.5-flash" and c.summary_timeout_seconds == 170
    assert c.summary_max_chars == 600_000 and c.summary_chunk_chars == 150_000

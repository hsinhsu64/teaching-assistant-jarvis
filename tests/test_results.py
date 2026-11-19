from xiaoshen.answering import Answer, citation_label
from xiaoshen.config import Config
from xiaoshen.llm import LLMError
from xiaoshen.results import (MAX_READ, answer_from_abstracts, answer_from_list, is_results_base,
                              result_refs)

from fakes import FakeLLM

ABS1 = "Tacrolimus induced remission in 80% of children with steroid-resistant nephrotic syndrome."
ABS2 = "This randomized controlled trial enrolled 240 children and compared rituximab with tacrolimus."


def ref(i, year, journal, author="Smith", **kw):
    return {"index": i, "title": f"Paper {i}", "first_author": author, "journal": journal, "year": year, **kw}


def base_answer(n=2, route="openevidence"):
    refs = {1: ref(1, "2019", "Kidney Int", "Lee"), 2: ref(2, "2025", "N Engl J Med", "Wang")}
    for i in range(3, n + 1):
        refs[i] = ref(i, "2020", "Pediatr Nephrol")
    return Answer("b1", "SRNS 治療？", route, "answered", references=refs)


def test_is_results_base_needs_answered_literature_with_refs():
    assert is_results_base(base_answer())
    assert is_results_base(base_answer(route="pubmed"))
    assert not is_results_base(None)
    assert not is_results_base(Answer("x", "q", "deck", "answered", references={1: ref(1, "2020", "J")}))
    assert not is_results_base(Answer("x", "q", "openevidence", "not_found", references={1: ref(1, "2020", "J")}))
    assert not is_results_base(Answer("x", "q", "openevidence", "answered"))


def test_result_refs_sorted_by_index():
    b = base_answer(3)
    assert [r["index"] for r in result_refs(b)] == [1, 2, 3]


def test_list_answer_builds_card_from_listed_refs():
    b = base_answer()
    llm = FakeLLM({"kind": "answer", "answer_zh": "最新的是第 2 篇（2025）", "refs": [2]})
    ans = answer_from_list("哪一篇最新？", b, llm, "flash")
    assert ans.status == "answered"
    c = ans.cards[0]
    assert c.point_zh == "最新的是第 2 篇（2025）"
    assert c.source == citation_label(b.references[2]) and c.refs == [2]
    assert ans.route == b.route and ans.references == {2: b.references[2]}
    prompt = llm.calls[0]["prompt"]
    assert "2019" in prompt and "2025" in prompt and "Kidney Int" in prompt and "N Engl J Med" in prompt
    assert llm.calls[0]["model"] == "flash"


def test_list_answer_rejects_unknown_ref():
    ans = answer_from_list("哪一篇最新？", base_answer(), FakeLLM({"kind": "answer", "answer_zh": "第 7 篇", "refs": [7]}),
                           "flash")
    assert ans.status == "error" and ans.cards == []


def test_list_answer_rejects_empty_answer():
    ans = answer_from_list("哪一篇最新？", base_answer(), FakeLLM({"kind": "answer", "answer_zh": " ", "refs": [2]}),
                           "flash")
    assert ans.status == "error"


def test_list_answer_needs_reading():
    llm = FakeLLM({"kind": "needs_reading", "answer_zh": "", "refs": []})
    assert answer_from_list("哪幾篇是 RCT？", base_answer(), llm, "flash") == "needs_reading"


def test_list_answer_llm_error_is_error_not_search():
    llm = FakeLLM(exc=LLMError("timeout"))
    ans = answer_from_list("哪一篇最新？", base_answer(), llm, "flash")
    assert ans.status == "error" and len(llm.calls) == 1


def _reading_llm(label, quote):
    return FakeLLM({"status": "answered", "summary_zh": "", "clarify_zh": "",
                    "cards": [{"point_zh": "第 2 篇是隨機對照試驗", "quote": quote, "source_label": label, "page": 1}]})


def test_abstract_answer_cards_point_to_literature():
    b = base_answer()
    texts = {1: ABS1, 2: ABS2}
    llm = _reading_llm(citation_label(b.references[2]), "This randomized controlled trial enrolled 240 children")
    ans = answer_from_abstracts("哪幾篇是 RCT？", b, lambda r: (texts[r["index"]], r["title"]), llm, "pro", 50000)
    assert ans.status == "answered"
    c = ans.cards[0]
    assert c.refs == [2] and c.doc == "" and c.source == citation_label(b.references[2])
    assert ans.route == b.route and set(ans.references) == {2}
    assert ans.to_dict()["cards"][0]["origin"] == "literature"


def test_abstract_answer_rejects_unverified_quote():
    b = base_answer()
    llm = _reading_llm(citation_label(b.references[2]), "Cyclophosphamide was superior in all children")
    ans = answer_from_abstracts("哪幾篇是 RCT？", b, lambda r: (ABS2, r["title"]), llm, "pro", 50000)
    assert ans.status == "not_found" and ans.cards == []


def test_abstract_answer_reads_at_most_10():
    b = base_answer(12)
    seen = []

    def fetch(r):
        seen.append(r["index"])
        return ABS1, r["title"]

    answer_from_abstracts("q", b, fetch, FakeLLM({"status": "not_found", "summary_zh": "", "cards": []}), "pro", 50000)
    assert MAX_READ == 10 and seen == list(range(1, 11))


def test_abstract_answer_notes_missing():
    b = base_answer()
    llm = _reading_llm(citation_label(b.references[2]), "This randomized controlled trial enrolled 240 children")
    ans = answer_from_abstracts("q", b, lambda r: ("" if r["index"] == 1 else ABS2, r["title"]), llm, "pro", 50000)
    assert ans.status == "answered" and "第 1 篇抓不到摘要" in ans.fallback_note


def test_abstract_answer_all_missing():
    llm = FakeLLM({"status": "answered", "summary_zh": "", "cards": []})
    ans = answer_from_abstracts("q", base_answer(), lambda r: ("", r["title"]), llm, "pro", 50000)
    assert ans.status == "error" and ans.error == "抓不到這些文獻的摘要" and llm.calls == []


def test_config_has_model_results():
    assert Config().model_results == "gemini-3.5-flash"


def test_list_answer_rejects_unlisted_index_in_text():
    ans = answer_from_list("哪一篇最新？", base_answer(),
                           FakeLLM({"kind": "answer", "answer_zh": "最新的是第 9 篇", "refs": [2]}), "flash")
    assert ans.status == "error" and ans.cards == []


def test_list_answer_adds_listed_indices_from_text():
    b = base_answer()
    ans = answer_from_list("哪一篇最新？", b,
                           FakeLLM({"kind": "answer", "answer_zh": "第 1 篇是 2019 年，第 2 篇是 [2] 2025 年", "refs": [2]}),
                           "flash")
    assert ans.status == "answered" and ans.cards[0].refs == [2, 1]
    assert set(ans.references) == {1, 2}

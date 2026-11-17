import json
import subprocess

import pytest

from xiaoshen.llm import GeminiLLM, LLMError
from xiaoshen.oe_client import OEBlocked, OEError, ask_openevidence, oe_auth_ok


class FakeRun:
    def __init__(self, stdout="", stderr="", returncode=0, exc=None):
        self.stdout, self.stderr, self.returncode, self.exc = stdout, stderr, returncode, exc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def _cit(title, journal="", date="", authors="", doi=""):
    return {"metadata": {"citation_detail": {"title": title, "journal_name": journal, "dt_published": date,
                                             "authors_string": authors, "doi": doi}}}


def _article(raw_text, spans):
    return {"output": {"structured_article": {"raw_text": raw_text, "articlesection_set": [
        {"articleparagraph_set": [{"articlespan_set": [{"text": t, "citations": c} for t, c in spans]}]}]}}}


def _run_with_article(tmp_path, article, answer):
    art = tmp_path / "article.json"
    art.write_text(json.dumps(article), encoding="utf-8")
    out = {"article_id": "abc", "status": "success", "extracted_answer_raw": answer,
           "artifacts": {"articlePath": str(art)}}
    run = FakeRun(stdout=json.dumps(out))
    return ask_openevidence("SRNS first line?", tmp_path / "oe.py", runner=run), run


def test_parses_answer_and_citations_by_marker(tmp_path):
    """實測 OpenEvidence：文中 [n] 是它自己的編號；每段的 citations 依該段排序後的編號一一對應。"""
    raw = ("**CNIs** are first-line [3][5].\n\n- A 2012 RCT favoured CNI over cyclophosphamide [3][4].\n"
           "<visual>Figure 1[3]</visual>\n![fig](https://x/y.png)")
    lancet = _cit("Childhood Nephrotic Syndrome", "Lancet", "2023-09-02T00:00:00+00:00", "Vivarelli M, Gibson K", "10.1016/x")
    kdigo = _cit("KDIGO 2025 Nephrotic Syndrome in Children", "Kidney International", "2025-01-01", "Floege J")
    cochrane = _cit("Interventions for idiopathic SRNS", "Cochrane Database Syst Rev", "2025", "Isaac M")
    article = _article(raw, [("<strong>CNIs</strong>", []), (" are first-line", [lancet, kdigo]),
                             ("- A 2012 RCT favoured CNI over cyclophosphamide", [lancet, cochrane])])
    r, run = _run_with_article(tmp_path, article, raw)
    assert r.article_id == "abc"
    assert "CNIs" in r.answer and "<visual" not in r.answer and "![" not in r.answer
    by = {c["index"]: c for c in r.citations}
    assert sorted(by) == [3, 4, 5]
    assert by[3] == {"index": 3, "title": "Childhood Nephrotic Syndrome", "journal": "Lancet", "year": "2023",
                     "doi": "10.1016/x", "first_author": "Vivarelli", "authors": "Vivarelli M, Gibson K",
                     "pmid": "", "href": ""}
    assert by[5]["title"].startswith("KDIGO 2025") and by[4]["title"].startswith("Interventions")
    cmd, kw = run.calls[0]
    assert cmd[2:4] == ["ask", "SRNS first line?"] and "--no-bibtex" in cmd
    assert "OE_ARTIFACT_DIR" in kw["env"]


def test_inconsistent_marker_mapping_dropped(tmp_path):
    """同一編號在兩段對到不同文獻、或段落編號數與文獻數不符 → 那些編號不採用（寧可沒有出處也不標錯）。"""
    raw = "Claim one [1][2].\n\nClaim two [1].\n\nClaim three [7]."
    a, b, c = _cit("Paper A"), _cit("Paper B"), _cit("Paper C")
    article = _article(raw, [("Claim one", [a, b]), ("Claim two", [c]), ("Claim three", [a, b])])
    r, _ = _run_with_article(tmp_path, article, raw)
    assert {c["index"] for c in r.citations} == {2}


def test_captcha_403_raises_blocked(tmp_path):
    err = "POST /api/article failed: 403 b'{\"url\":\"https://geo.captcha-delivery.com/captcha/?x\"}'"
    with pytest.raises(OEBlocked, match="重新驗證"):
        ask_openevidence("q", tmp_path / "oe.py", runner=FakeRun(stderr=err, returncode=1))


def test_other_failure_raises_oeerror(tmp_path):
    with pytest.raises(OEError):
        ask_openevidence("q", tmp_path / "oe.py", runner=FakeRun(stderr="POST failed: 500 boom", returncode=1))


def test_timeout_raises_oeerror(tmp_path):
    run = FakeRun(exc=subprocess.TimeoutExpired("oe", 180))
    with pytest.raises(OEError, match="逾時"):
        ask_openevidence("q", tmp_path / "oe.py", runner=run)


def test_empty_answer_raises_oeerror(tmp_path):
    out = {"article_id": "abc", "status": "processing", "extracted_answer_raw": None, "artifacts": None}
    with pytest.raises(OEError):
        ask_openevidence("q", tmp_path / "oe.py", runner=FakeRun(stdout=json.dumps(out)))


def test_auth_ok_false_on_401(tmp_path):
    out = {"authenticated": False, "statusCode": 401}
    assert oe_auth_ok(tmp_path / "oe.py", runner=FakeRun(stdout=json.dumps(out))) is False
    ok = {"authenticated": True, "statusCode": 200}
    assert oe_auth_ok(tmp_path / "oe.py", runner=FakeRun(stdout=json.dumps(ok))) is True
    assert oe_auth_ok(tmp_path / "oe.py", runner=FakeRun(exc=FileNotFoundError())) is False


def test_gemini_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM().generate_json("m", "sys", "prompt", {"type": "object"})


class _Err(Exception):
    def __init__(self, code):
        super().__init__(f"{code} ERR")
        self.code = code


def test_gemini_falls_back_on_quota_or_overload(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    tried = []

    def fake_call(self, model, system, prompt, schema, timeout):
        tried.append(model)
        if model != "c":
            raise _Err(429 if model == "a" else 503)
        return '{"ok": true}'

    monkeypatch.setattr(GeminiLLM, "_call", fake_call)
    llm = GeminiLLM(fallbacks=["b", "c"])
    assert llm.generate_json("a", "s", "p", {}) == {"ok": True}
    assert tried == ["a", "b", "c"] and llm.last_model == "c"


def test_gemini_no_fallback_on_other_errors(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    tried = []

    def fake_call(self, model, *a):
        tried.append(model)
        raise _Err(400)

    monkeypatch.setattr(GeminiLLM, "_call", fake_call)
    with pytest.raises(LLMError):
        GeminiLLM(fallbacks=["b"]).generate_json("a", "s", "p", {})
    assert tried == ["a"]


def test_gemini_total_timeout_shared_across_fallbacks(monkeypatch):
    import xiaoshen.llm as llm_mod

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    clock = {"t": 0.0}
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: clock["t"])
    budgets = []

    def fake_call(self, model, system, prompt, schema, timeout):
        budgets.append(timeout)
        clock["t"] += 50  # 每個模型耗 50 秒後失敗
        raise _Err(503)

    monkeypatch.setattr(GeminiLLM, "_call", fake_call)
    with pytest.raises(LLMError, match="逾時|503"):
        GeminiLLM(fallbacks=["b", "c"]).generate_json("a", "s", "p", {}, timeout=90)
    assert budgets == [90, 40]  # 第三個模型已沒有時間，不再呼叫


def test_markers_follow_appearance_order_with_repeats(tmp_path):
    """實測中文答案：[3][4][7][9][9][7] 對 6 筆文獻——依出現順序（含重複）對應，不是排序去重。"""
    raw = "CNI 為首選[3][4][7][9]。KDIGO 指引建議[9][7]。"
    a, b, c, d = _cit("Paper A"), _cit("Paper B"), _cit("Paper C"), _cit("Paper D")
    article = _article(raw, [("CNI 為首選", [a, b, c, d, d, c])])
    r, _ = _run_with_article(tmp_path, article, raw)
    by = {x["index"]: x["title"] for x in r.citations}
    assert by == {3: "Paper A", 4: "Paper B", 7: "Paper C", 9: "Paper D"}


def test_figures_linked_to_citation_markers(tmp_path):
    """實測：<visual>Figure 1.1[12]</visual> 的 [12] 是出處；figures.json 有說明與已下載檔。找不到檔或出處的圖不採用。"""
    figdir = tmp_path / "figures"
    figdir.mkdir()
    (figdir / "Figure_1.1.jpg").write_bytes(b"\xff\xd8jpg")
    figs = tmp_path / "figures.json"
    figs.write_text(json.dumps([
        {"name": "Figure 1.1", "caption": "CSA vs control: complete remission", "localPath": str(figdir / "Figure_1.1.jpg")},
        {"name": "Figure 2", "caption": "沒下載成功", "url": "https://x"},
        {"name": "Table 3", "caption": "文中沒標出處", "localPath": str(figdir / "Figure_1.1.jpg")},
    ]), encoding="utf-8")
    raw = "CNI first-line [12].\n<visual>Figure 1.1[12]</visual>\n<visual>Figure 2[13]</visual>"
    article = _article(raw, [("CNI first-line", [_cit("Interventions for SRNS", "Cochrane", "2025", "Isaac M")])])
    art = tmp_path / "article.json"
    art.write_text(json.dumps(article), encoding="utf-8")
    out = {"article_id": "a", "status": "success", "extracted_answer_raw": raw,
           "artifacts": {"articlePath": str(art), "figuresJsonPath": str(figs)}}
    r = ask_openevidence("q", tmp_path / "oe.py", runner=FakeRun(stdout=json.dumps(out)))
    assert r.figures == [{"name": "Figure 1.1", "caption": "CSA vs control: complete remission",
                          "path": str(figdir / "Figure_1.1.jpg"), "index": 12}]


def test_tables_parsed_from_mdtable():
    """實測格式：<mdtable> 包住的 markdown 表格，最後一欄常是文獻編號。"""
    from xiaoshen.oe_client import tables_from_answer

    text = ("Intro [1].\n\n<mdtable>\n| Feature | Bartter syndrome | Gitelman syndrome | References |\n|---|---|---|---|\n"
            "| Urine Ca/Cr | Normal or elevated | Low | [1][8] |\n| Serum magnesium | Usually normal | Often low | [1][3] |\n"
            "</mdtable>\n\nMore text.\n<mdtable>\nnot a table\n</mdtable>")
    assert tables_from_answer(text) == [{
        "header": ["Feature", "Bartter syndrome", "Gitelman syndrome", "References"],
        "rows": [["Urine Ca/Cr", "Normal or elevated", "Low", "[1][8]"],
                 ["Serum magnesium", "Usually normal", "Often low", "[1][3]"]],
    }]


def test_table_rows_with_extra_reference_column_kept():
    """實測：表頭 4 欄、每列 5 欄（多一欄文獻編號沒表頭）→ 補表頭，不丟列。"""
    from xiaoshen.oe_client import tables_from_answer

    text = ("<mdtable>\n| 疾病 | 基因 | 蛋白產物 | 部位 |\n|------|------|----------|------|\n"
            "| Bartter II型 | KCNJ1 | ROMK鉀通道 | 亨利氏環 | [3] |\n| Gitelman | SLC12A3 | NCC | 遠端腎小管 |\n</mdtable>")
    t = tables_from_answer(text)[0]
    assert t["header"] == ["疾病", "基因", "蛋白產物", "部位", "出處"]
    assert t["rows"] == [["Bartter II型", "KCNJ1", "ROMK鉀通道", "亨利氏環", "[3]"],
                         ["Gitelman", "SLC12A3", "NCC", "遠端腎小管", ""]]


def test_short_or_linked_spans_do_not_poison_mapping(tmp_path):
    """實測中文答案：「。」這種短片段會對到錯的行而把正確編號判為衝突；含 markdown 連結的片段找不到行。"""
    from xiaoshen.oe_client import citations_by_marker

    raw = ("**主要鑑別點**[1][3]。其他說明[4]。\n"
           "- [Bartter syndrome](/rare-disease/bartter)：缺損在亨氏環升支粗段[2]\n"
           "- 尿鈣排泄是最重要的鑑別指標[1][3]")
    a, b, c, d = _cit("Paper A"), _cit("Paper B"), _cit("Paper C"), _cit("Paper D")
    article = _article(raw, [("<strong>主要鑑別點</strong>", []), ("。", [a, b, c]),
                             ("- [Bartter syndrome](/rare-disease/bartter)：缺損在亨氏環升支粗段", [d]),
                             ("- 尿鈣排泄是最重要的鑑別指標", [a, b])])
    by = {x["index"]: x["title"] for x in citations_by_marker(article)}
    assert by == {1: "Paper A", 2: "Paper D", 3: "Paper B"}


def test_gemini_uses_configured_timeout(monkeypatch):
    """AI 回答時限 150 秒（解讀型問題曾超過 90 秒）。"""
    from xiaoshen.config import Config

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    budgets = []

    def fake_call(self, model, system, prompt, schema, timeout):
        budgets.append(timeout)
        return "{}"

    monkeypatch.setattr(GeminiLLM, "_call", fake_call)
    assert Config().ai_timeout_seconds == 150
    GeminiLLM(timeout=150).generate_json("m", "s", "p", {})
    GeminiLLM(timeout=150).generate_json("m", "s", "p", {}, timeout=20)  # 呼叫端指定的較短時限仍有效
    assert budgets[0] == pytest.approx(150, abs=1) and budgets[1] == pytest.approx(20, abs=1)


def test_citation_keeps_pmid_and_web_link():
    """2026-11-05 實機：KDIGO 指引 PDF 沒有 DOI，只有 OpenEvidence 給的官網連結——要留下來才開得了原文。"""
    from xiaoshen.oe_client import _citation

    kdigo = _citation(4, {"title": "KDIGO 2025", "pmid": None,
                          "href": "https://kdigo.org/wp-content/uploads/2025/04/KDIGO-2025-Guideline.pdf"})
    assert kdigo["href"].startswith("https://kdigo.org/") and kdigo["pmid"] == ""
    jama = _citation(1, {"title": "Tacrolimus or MMF", "doi": "10.1001/x", "pmid": 40354041, "href": "https://j/x"})
    assert jama["pmid"] == "40354041"
    assert _citation(2, {"title": "t", "href": "javascript:alert(1)"})["href"] == ""  # 只收 http(s)


# ---- OE 簡報（§6.2c）：保留原文、圖的位置、對應不上編號的文獻 ----

def test_raw_keeps_images_and_tables_drops_followups(tmp_path):
    raw = ("# Title\n\nCNI first-line [1].\n\n![Visual Abstract: Trial](https://img/x.jpg)\n\n<mdtable>\n| a | b |\n|---|---|\n| 1 | 2 |\n</mdtable>\n\n"
           '<follow-up-question submit="q2">More?</follow-up-question>\n<follow-up-topic>Topic A</follow-up-topic>')
    article = _article(raw, [("CNI first-line", [_cit("Paper A")])])
    r, _ = _run_with_article(tmp_path, article, raw)
    assert "![Visual Abstract: Trial](https://img/x.jpg)" in r.raw and "<mdtable>" in r.raw
    assert "follow-up" not in r.raw and "More?" not in r.raw and "Topic A" not in r.raw


def test_other_citations_collects_unmapped(tmp_path):
    raw = "Claim one [1][2].\n\nClaim two [1].\n\nClaim three [7]."
    a, b, c = _cit("Paper A"), _cit("Paper B"), _cit("Paper C")
    article = _article(raw, [("Claim one", [a, b]), ("Claim two", [c]), ("Claim three", [a])])
    r, _ = _run_with_article(tmp_path, article, raw)
    mapped = {x["title"] for x in r.citations}
    others = [x["title"] for x in r.other_citations]
    assert not (mapped & set(others))
    assert set(others) | mapped == {"Paper A", "Paper B", "Paper C"}
    assert all(x["index"] is None for x in r.other_citations)
    assert len(others) == len(set(others))


def test_deck_figures_have_marker_and_optional_index(tmp_path):
    figdir = tmp_path / "figures"
    figdir.mkdir()
    (figdir / "va.jpg").write_bytes(b"\xff\xd8jpg")
    (figdir / "f2.jpg").write_bytes(b"\xff\xd8jpg")
    figs = tmp_path / "figures.json"
    figs.write_text(json.dumps([
        {"name": "Visual Abstract", "url": "https://img/va.jpg", "caption": "Trial", "localPath": str(figdir / "va.jpg")},
        {"name": "Figure 2", "url": "https://img/f2.jpg", "caption": "No ref", "localPath": str(figdir / "f2.jpg")},
        {"name": "Figure 3", "url": "https://img/f3.jpg", "caption": "not downloaded"},
    ]), encoding="utf-8")
    answer = "Text [18].\n\n![Visual Abstract: Trial](https://img/va.jpg)\n\nMore.\n\n![Figure 2: No ref](https://img/f2.jpg)"
    structured = "Text [18].\n<visual>Visual Abstract[18]</visual>\nMore.\n<visual>Figure 2</visual>"
    article = _article(structured, [("Text", [_cit("Paper A")])])
    art = tmp_path / "article.json"
    art.write_text(json.dumps(article), encoding="utf-8")
    out = {"article_id": "a", "status": "success", "extracted_answer_raw": answer,
           "artifacts": {"articlePath": str(art), "figuresJsonPath": str(figs)}}
    r = ask_openevidence("q", tmp_path / "oe.py", runner=FakeRun(stdout=json.dumps(out)))
    by = {f["name"]: f for f in r.deck_figures}
    assert by["Visual Abstract"]["marker"] == "![Visual Abstract: Trial](https://img/va.jpg)"
    assert by["Visual Abstract"]["index"] == 18 and by["Figure 2"]["index"] is None
    assert "Figure 3" not in by and r.missing_figures == 1


# ---- 經瀏覽器分頁的 relay ----

def test_ask_openevidence_runs_through_the_adapter(tmp_path):
    import subprocess

    from xiaoshen import oe_relay

    class Run:
        def __call__(self, cmd, **kw):
            self.cmd, self.env = cmd, kw["env"]
            return subprocess.CompletedProcess(cmd, 1, "", "boom")

    run = Run()
    try:
        ask_openevidence("q", tmp_path / "oe.py", runner=run)
    except Exception as e:  # 失敗是預期的（假的 oe.py 回 1）；只看送出的指令
        assert "OpenEvidence" in str(e), repr(e)
    assert run.cmd[1] == oe_relay.__file__ and run.cmd[2:4] == ["ask", "q"]
    assert run.env["XIAOSHEN_OE_SCRIPT"] == str(tmp_path / "oe.py")

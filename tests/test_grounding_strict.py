"""最終審查找到的出處驗證漏洞：近似比對不得放過改過的數字、否定、方向、單位。"""

import pytest

from xiaoshen.grounding import match_quote, verify_quote


@pytest.mark.parametrize("page, quote", [
    ("Insulin 0.1 U/kg with dextrose shifts potassium into cells.", "Insulin 1 U/kg with dextrose shifts potassium into cells."),
    ("Give prednisolone 60 mg/m2/day for 4 weeks as initial therapy.", "Give prednisolone 40 mg/m2/day for 6 weeks as initial therapy."),
    ("Calcium stabilizes the myocardium but does NOT lower plasma K+ levels.", "Calcium stabilizes the myocardium and does lower plasma K+ levels."),
    ("Thiazides decrease urinary calcium excretion in these patients.", "Thiazides increase urinary calcium excretion in these patients."),
    ("Avoid metformin when eGFR > 30 is not maintained in follow up.", "Avoid metformin when eGFR < 30 is not maintained in follow up."),
    ("Albumin 1 g/kg was infused over four hours in the trial.", "Albumin 1 mg/kg was infused over four hours in the trial."),
    ("In SRNS rituximab is recommended after failure of calcineurin inhibitors.",
     "In SRNS rituximab is not recommended after failure of calcineurin inhibitors."),
    ("Hypokalemia with metabolic alkalosis suggests diuretic use or vomiting.",
     "Hyperkalemia with metabolic alkalosis suggests diuretic use or vomiting."),
])
def test_altered_meaning_rejected(page, quote):
    assert not verify_quote(quote, page)


def test_spelling_variant_still_passes():
    assert verify_quote("tacrolimus or cyclosporine is recommended", "tacrolimus or cyclosporin is recommended")


@pytest.mark.parametrize("quote, page", [
    ("尿 AG 正值：RTA", "RTA Diagnostic Algorithm\n尿 AG 正值：RTA\n尿 AG 負值：GI loss"),
    ("低血鉀 代謝性鹼中毒", "Gitelman Syndrome ⚫AR ⚫低血鉀 代謝性鹼中毒合併鹽分流失"),
    ("K 3.5-5.0 mEq/L", "Normal values: Na 135-145 mEq/L, K 3.5-5.0 mEq/L"),
    ("Na < 135 mEq/L", "Hyponatremia is defined as Na < 135 mEq/L in plasma."),
])
def test_short_verbatim_clinical_content_passes(quote, page):
    assert verify_quote(quote, page)


def test_translation_is_not_a_quote():
    assert not verify_quote("hypokalemia, metabolic alkalosis", "Gitelman ⚫低血鉀 代謝性鹼中毒合併鹽分流失")


@pytest.mark.parametrize("quote", ["Pathophysiology", "Gitelman Syndrome", "indomethacin", "Bartter & Gitelman Syndrome"])
def test_titles_and_single_words_rejected(quote):
    page = "Bartter & Gitelman Syndrome\nPathophysiology\nGitelman Syndrome\nindomethacin plus potassium supplements"
    assert not verify_quote(quote, page)


def test_nfkc_variants_pass():
    assert verify_quote("does NOT lower plasma K⁺ levels", "Calcium does NOT lower plasma K+ levels at all.")
    assert verify_quote("the ﬁrst-line agent is tacrolimus", "In children the first-line agent is tacrolimus.")


def test_match_returns_source_text_not_model_text():
    m = match_quote("tacrolimus or cyclosporine is recommended", "We say Tacrolimus or cyclosporin is recommended here.")
    assert m is not None and m.text == "Tacrolimus or cyclosporin is recommended"
    m2 = match_quote("calcineurin inhibitors are first-line", "... Calcineurin  inhibitors are first-line therapy ...")
    assert m2.text == "Calcineurin inhibitors are first-line"


def test_citation_markers_in_source_are_ignored():
    """OpenEvidence 原文句中夾著 [3][4]；模型引用時常省略編號，不應因此判定不符。"""
    src = "Calcineurin inhibitors are first-line therapy [3][4] and remission rates reach 52.4% [5, 6]."
    # 第二次審查：只有 OpenEvidence 路線略過 [n]（簡報裡的方括號數字是內容）
    m = match_quote("Calcineurin inhibitors are first-line therapy and remission rates reach 52.4%", src, ignore_citations=True)
    assert m is not None and "[3][4]" in m.text
    assert match_quote("first-line therapy [3][4] and remission rates", src, ignore_citations=True)
    assert not match_quote("Calcineurin inhibitors are first-line therapy and remission rates reach 42.4%", src,
                           ignore_citations=True)


def test_short_chinese_quote_passes():
    """實測：「低尿鈣和低血鎂」7 個中文字是有效證據，不應因少於 8 字被丟。"""
    page = "Gitelman Syndrome ⚫AR ⚫低血鉀的代謝性鹼中毒合併鹽分流失 ⚫低尿鈣和低血鎂"
    assert verify_quote("低尿鈣和低血鎂", page)
    assert not verify_quote("Pathophysiology", "Pathophysiology\nBartter: normal Mg")

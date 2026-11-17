from xiaoshen.answering import Answer, Card
from xiaoshen.card import card_html, render_card

REFS = {
    1: {"index": 1, "title": "Estrogen and Progesterone Receptor Testing", "journal": "J Clin Oncol", "year": "2020",
        "doi": "", "first_author": "Allison", "authors": "Allison KH, Hammond MEH, Dowsett M, Lester SC"},
    4: {"index": 4, "title": "Endocrine Therapy for High-Risk Lesions", "journal": "J Clin Oncol", "year": "2023",
        "doi": "", "first_author": "Laws", "authors": "Laws A, Punglia RS"},
}


def oe_answer():
    return Answer("a1", "DCIS endocrine therapy?", "openevidence", "answered",
                  cards=[Card("內分泌治療", "Endocrine therapy reduces ipsilateral events", "[1] Allison 2020, J Clin Oncol", refs=[1]),
                         Card("無存活益處", "has not shown an overall survival benefit", "[4] Laws 2023, J Clin Oncol", refs=[4])],
                  references=REFS)


def test_card_uses_original_quotes_and_markers():
    h = card_html(oe_answer())
    assert "Endocrine therapy reduces ipsilateral events" in h and "[1]" in h and "[4]" in h
    assert "內分泌治療" not in h  # 重點用英文原文（已驗證的引文）
    assert "DCIS endocrine therapy?" in h and "OpenEvidence" in h


def test_card_references_full_details():
    h = card_html(oe_answer())
    assert "Estrogen and Progesterone Receptor Testing" in h and "J Clin Oncol. 2020." in h
    assert "Allison KH, Hammond MEH, Dowsett M, et al." in h  # 超過 3 位加 et al.
    assert "Laws A, Punglia RS" in h and "et al" not in h.split("Laws A, Punglia RS")[1][:10]


def test_card_for_slide_answer_numbers_sources():
    a = Answer("a2", "Bartter 跟 Gitelman？", "deck", "answered", interpreted="Bartter 和 Gitelman 怎麼分？",
               cards=[Card("p", "Bartter: 鎂正常; 尿鈣多", "今天的簡報 第 37 頁"),
                      Card("p", "出生後會有多尿", "今天的簡報 第 38 頁"),
                      Card("p", "Gitelman:低血鎂; 尿鈣少", "今天的簡報 第 37 頁")])
    h = card_html(a)
    assert "Bartter 和 Gitelman 怎麼分？" in h  # 標題用校正後的問題
    assert h.count("今天的簡報 第 37 頁") == 1 and "[1]" in h and "[2]" in h and "[3]" not in h


def test_card_escapes_html():
    a = Answer("a3", "<script>x</script>", "deck", "answered", cards=[Card("p", "a < b & c", "今天的簡報 第 1 頁")])
    h = card_html(a)
    assert "<script>" not in h and "a &lt; b &amp; c" in h


def test_render_card_png(tmp_path):
    out = render_card(oe_answer(), tmp_path / "card.png")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and out.stat().st_size > 5000


def test_display_quote_cleans_markdown():
    """實測：引文可能是 OpenEvidence 的表格列或帶粗體符號；顯示時整理，驗證用的原文不動。"""
    from xiaoshen.answering import display_quote

    assert display_quote("| 受累腎小管部位 | Henle氏環上升支粗段(TAL) | 遠曲小管(DCT) |") == \
        "受累腎小管部位：Henle氏環上升支粗段(TAL) ／ 遠曲小管(DCT)"
    assert display_quote("生化指標**:Gitelman syndrome") == "生化指標:Gitelman syndrome"
    assert display_quote("plain **bold** text") == "plain bold text"
    a = Answer("a", "q", "deck", "answered", cards=[Card("p", "| 部位 | TAL | DCT |", "今天的簡報 第 1 頁")])
    assert "部位：TAL ／ DCT" in card_html(a) and "| 部位" not in card_html(a)
    assert a.to_dict(with_quotes=True)["cards"][0]["quote"] == "部位：TAL ／ DCT"
    assert a.cards[0].quote == "| 部位 | TAL | DCT |"


def test_card_includes_derivations():
    from xiaoshen.persona import Persona, set_persona
    set_persona(Persona("小森"))
    from xiaoshen.answering import Derivation

    a = Answer("a4", "AG?", "deck", "answered",
               cards=[Card("p", "Na (B) 鈉 140", "今天的簡報 第 28 頁（圖片辨識）"),
                      Card("p", "AG = Na − (Cl + HCO3)", "今天的簡報 第 61 頁")],
               derivations=[Derivation("AG = 140 − (106 + 20.4) = 13.6", [0, 1],
                                       ["今天的簡報 第 28 頁（圖片辨識）", "今天的簡報 第 61 頁"])])
    h = card_html(a)
    assert "小森推導" in h and "AG = 140 − (106 + 20.4) = 13.6" in h and "[1][2]" in h


def test_card_marks_derivations_unconfirmed():
    from xiaoshen.answering import Derivation

    a = Answer("a5", "AG?", "deck", "answered", cards=[Card("p", "Na (B) 鈉 140", "今天的簡報 第 28 頁")],
               derivations=[Derivation("AG = 13.6", [0], ["今天的簡報 第 28 頁"])],
               derivation_notes=["此為靜脈血，未推導代償"])
    h = card_html(a)
    assert "未經主治確認" in h and "此為靜脈血，未推導代償" in h

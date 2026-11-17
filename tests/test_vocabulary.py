from xiaoshen.vocabulary import deck_terms, vocabulary_prompt

PAGES = [
    "酸鹼平衡異常 (Acid - Base Disorders)\n示範醫院",
    "Bartter & Gitelman Syndrome\n",
    "Pathophysiology\n⚫Bartter: 鎂正常; 尿 鈣多 ⚫Gitelman:低血鎂; 尿鈣少",
    "Renal Tubular Acidosis\n⚫Renal tubular acidosis (RTA) is a disease state with normal anion gap",
    "Case: A123456789 王小明 12床\nABG pH 7.21 PaCO2 28",
    "",
    "SIADH vs Cerebral Salt Wasting\nHyponatremia work-up",
]


def test_deck_terms_collects_titles_and_medical_terms():
    terms = deck_terms(PAGES)
    for t in ("Bartter & Gitelman Syndrome", "Gitelman", "Bartter", "RTA", "ABG", "SIADH",
              "Cerebral Salt Wasting", "Renal Tubular Acidosis", "Hyponatremia"):
        assert t in terms, t


def test_deck_terms_drop_identifiers():
    terms = deck_terms(PAGES)
    joined = " ".join(terms)
    assert "A123456789" not in joined and "12床" not in joined
    assert not any("Case:" in t for t in terms)  # 含識別資訊的標題整個不用


def test_deck_terms_capped_and_deduped():
    many = [f"Term{i} Syndrome\nWord{i}abc" for i in range(500)]
    terms = deck_terms(many, max_chars=600)
    assert len(" ".join(terms)) <= 600 and len(terms) == len(set(terms))


def test_vocabulary_prompt():
    assert vocabulary_prompt([]) == ""
    p = vocabulary_prompt(["Bartter", "Gitelman"])
    assert "Bartter、Gitelman" in p and "聽錯" in p


def test_deck_context_follows_current_deck(state_factory, make_pdf):
    from xiaoshen.vocabulary import deck_context

    st = state_factory()
    ctx = deck_context(st)
    assert ctx() == ""  # 沒開簡報
    st.open_deck(make_pdf(["Bartter Syndrome", "Gitelman Syndrome"], folder=st.config.folder("簡報"), name="A.pdf"))
    assert "Bartter Syndrome" in ctx()
    st.open_deck(make_pdf(["Nephrotic Syndrome"], folder=st.config.folder("簡報"), name="B.pdf"))
    p = ctx()
    assert "Nephrotic Syndrome" in p and "Bartter" not in p

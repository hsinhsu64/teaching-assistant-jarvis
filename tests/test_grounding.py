from xiaoshen.grounding import normalize, verify_quote


def test_normalize():
    assert normalize("  Neph-\nrotic   “Syndrome” — x ") == 'nephrotic "syndrome" - x'


def test_exact_quote_passes():
    assert verify_quote("calcineurin inhibitors are first-line", "... Calcineurin  inhibitors are first-line therapy ...")


def test_hyphen_linebreak_passes():
    assert verify_quote("steroid-resistant nephrotic", "steroid-resistant neph-\nrotic syndrome")


def test_quote_not_on_page_rejected():
    assert not verify_quote("rituximab is first-line therapy", "CNI are first-line therapy")


def test_too_short_rejected():
    assert not verify_quote("CNI", "CNI CNI CNI")


def test_near_match_passes():
    assert verify_quote("tacrolimus or cyclosporine is recommended", "tacrolimus or cyclosporin is recommended")


def test_near_match_inside_long_source():
    filler = "Unrelated sentence about blood pressure targets. " * 80
    src = filler + "We suggest tacrolimus or cyclosporin as initial therapy for SRNS. " + filler
    assert verify_quote("tacrolimus or cyclosporine as initial therapy for SRNS", src)


def test_empty_source_rejected():
    assert not verify_quote("calcineurin inhibitors are first-line", "")


def test_title_or_single_word_quote_rejected():
    page = "Pathophysiology\nBartter: normal Mg; high urine Ca\nGitelman Syndrome\nindomethacin plus potassium"
    for q in ("Pathophysiology", "Gitelman Syndrome", "indomethacin"):
        assert not verify_quote(q, page), q


def test_short_cjk_evidence_passes():
    assert verify_quote("Gitelman:低血鎂; 尿鈣少", "Pathophysiology ⚫Bartter: 鎂正常; 尿 鈣多 ⚫Gitelman:低血鎂; 尿鈣少")
    assert verify_quote("does NOT lower plasma K⁺.", "Calcium stabilizes the membrane but does NOT lower plasma K⁺.")

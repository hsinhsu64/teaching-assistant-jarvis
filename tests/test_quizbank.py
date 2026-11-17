from xiaoshen.quizbank import load_bank


def test_load_good_rows(make_bank):
    p = make_bank([["Case_SRNS", 5, "SRNS 一線？", "CYC", "CNI", "", "", "", "", "B", "解析", "KDIGO 2021"]])
    bank, warns = load_bank(p)
    qn = bank[("Case_SRNS", 5)]
    assert qn.options == ["CYC", "CNI"] and qn.answer == 1 and warns == []
    assert qn.id == "Case_SRNS-p5" and qn.source == "KDIGO 2021" and qn.explanation == "解析"


def test_bad_rows_skipped_with_row_numbers(make_bank):
    p = make_bank([
        ["D", 1, "ok", "A", "B", "", "", "", "", "A", "", ""],
        ["D", "x", "頁碼壞", "A", "B", "", "", "", "", "A", "", ""],
        ["D", 3, "正解超界", "A", "B", "", "", "", "", "E", "", ""],
        ["D", 4, "只有一個選項", "A", "", "", "", "", "", "A", "", ""],
    ])
    bank, warns = load_bank(p)
    assert list(bank) == [("D", 1)]
    assert [w.split("：")[0] for w in warns] == ["第 3 列", "第 4 列", "第 5 列"]


def test_duplicate_key_keeps_first(make_bank):
    p = make_bank([
        ["D", 1, "first", "A", "B", "", "", "", "", "A", "", ""],
        ["D", 1, "second", "A", "B", "", "", "", "", "B", "", ""],
    ])
    bank, warns = load_bank(p)
    assert bank[("D", 1)].stem == "first" and warns[0].startswith("第 3 列")


def test_blank_rows_ignored(make_bank):
    p = make_bank([["D", 1, "ok", "A", "B", "", "", "", "", "a", "", ""], ["", "", "", "", "", "", "", "", "", "", "", ""]])
    bank, warns = load_bank(p)
    assert bank[("D", 1)].answer == 0 and warns == []


def test_missing_bank_file(tmp_path):
    assert load_bank(tmp_path / "題庫.xlsx") == ({}, [])


def test_unreadable_bank_file(tmp_path):
    p = tmp_path / "題庫.xlsx"
    p.write_bytes(b"garbage")
    bank, warns = load_bank(p)
    assert bank == {} and warns[0].startswith("題庫無法開啟")


def test_deck_name_with_pdf_suffix_or_number(make_bank):
    p = make_bank([
        ["Case_SRNS.pdf", 1, "q", "A", "B", "", "", "", "", "A", "", ""],
        [123, 2, "q", "A", "B", "", "", "", "", "A", "", ""],
    ])
    bank, warns = load_bank(p)
    assert ("Case_SRNS", 1) in bank and ("123", 2) in bank and warns == []

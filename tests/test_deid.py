"""去識別換代號與換回（送 Gemini 前在本機做；對照表只在本機）。"""

from xiaoshen.deid import code_like, pseudonymize, reidentify, residual_phi


def items(*pairs):
    return [{"text": t, "kind": k} for t, k in pairs]


def test_longest_first():
    text = "王小明昨天發燒，小明的媽媽說王小明有吃藥"
    out, mapping = pseudonymize(text, items(("小明", "person"), ("王小明", "person")))
    assert "王小明" not in out and "小明" not in out
    back, unresolved = reidentify(out, mapping)
    assert back == text and unresolved == []
    assert out.count(next(c for c, o in mapping.items() if o == "王小明")) == 2


def test_codes_by_kind_and_order():
    text = "李大華 12床，病歷號 1234567；王小明 電話 0912345678，身分證 A123456789，生日 2015/3/4，其他資料 XYZ"
    out, mapping = pseudonymize(text, items(("王小明", "person"), ("李大華", "person"), ("12床", "bed"),
                                            ("病歷號 1234567", "chart"), ("0912345678", "phone"),
                                            ("A123456789", "id"), ("2015/3/4", "birthday"), ("XYZ", "other")))
    assert mapping == {"病人A": "李大華", "病人B": "王小明", "〔床號1〕": "12床", "〔病歷號1〕": "病歷號 1234567",
                       "〔電話1〕": "0912345678", "〔身分證1〕": "A123456789", "〔生日1〕": "2015/3/4",
                       "〔其他1〕": "XYZ"}
    assert out.startswith("病人A 〔床號1〕，〔病歷號1〕；病人B")


def test_item_not_in_text_gets_no_code():
    out, mapping = pseudonymize("沒有名字", items(("王小明", "person")))
    assert out == "沒有名字" and mapping == {}


def test_existing_code_in_text_not_clobbered():
    text = "上次那位病人A很好；這次王小明住院"
    out, mapping = pseudonymize(text, items(("王小明", "person")))
    code = next(iter(mapping))
    assert code == "病人B"  # 原文已有「病人A」：換下一個字母（審查後改；原本加 ′，Gemini 會丟掉 ′）
    assert out == f"上次那位病人A很好；這次{code}住院"
    back, unresolved = reidentify(out, mapping, ignore=code_like(text))
    assert back == text  # 原本的「病人A」不被換成王小明
    assert unresolved == []


def test_reidentify_tolerates_space_and_fullwidth():
    mapping = {"病人A": "王小明", "病人B": "李大華", "〔病歷號1〕": "1234567"}
    text = "病人 A 與病人Ａ、病人b，〔病歷號 1〕，【病歷號１】"
    back, unresolved = reidentify(text, mapping)
    assert back == "王小明 與王小明、李大華，1234567，1234567"
    assert unresolved == []


def test_reidentify_reports_unknown_codes():
    back, unresolved = reidentify("病人A 和病人C，〔電話2〕", {"病人A": "王小明"})
    assert back == "王小明 和病人C，〔電話2〕"
    assert unresolved == ["病人C", "〔電話2〕"]


def test_residual_scan_hits():
    out, mapping = pseudonymize("病歷號 1234567，12床", items(("病歷號 1234567", "chart"), ("12床", "bed")))
    assert residual_phi(out, mapping) == []  # 代號本身（〔床號1〕）不算命中
    assert residual_phi(out + "，另一位 7654321", mapping) == ["7654321"]


def test_reidentify_bracketless_codes():
    """審查：Gemini 把〔床號1〕寫成「床號1」也要換回，不能留下像真床號的字。"""
    mapping = {"病人A": "王小明", "〔床號1〕": "12床", "〔病歷號1〕": "病歷號 1234567"}
    back, unresolved = reidentify("病人A 住在床號1，病歷號1", mapping)
    assert back == "王小明 住在12床，病歷號 1234567" and unresolved == []


def test_reidentify_reports_bracketless_unknown():
    back, unresolved = reidentify("床號2 的病人", {"〔床號1〕": "12床"})
    assert unresolved == ["床號2"]


def test_existing_code_skips_to_next_letter():
    """審查：原文本來就有「病人A」時不用加 ′（Gemini 會丟掉 ′），改用沒出現過的代號。"""
    text = "上次那位病人A很好；這次王小明住院，〔床號1〕"
    out, mapping = pseudonymize(text, items(("王小明", "person")))
    assert mapping == {"病人B": "王小明"}
    back, unresolved = reidentify("這次病人A住院、病人B也是", mapping, ignore=code_like(text))
    assert back == "這次病人A住院、王小明也是" and unresolved == []


def test_lab_words_not_reported_as_codes():
    back, unresolved = reidentify("病人CRP 上升，病人BUN 也高", {"病人A": "王小明"})
    assert unresolved == []

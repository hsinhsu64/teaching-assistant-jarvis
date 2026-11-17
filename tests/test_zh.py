from xiaoshen.zh import to_traditional


def test_simplified_to_taiwan_traditional():
    assert to_traditional("这张图代表什么意义") == "這張圖代表什麼意義"


def test_traditional_and_english_unchanged():
    assert to_traditional("病歷號 1234567，anion gap 與 HCO3⁻") == "病歷號 1234567，anion gap 與 HCO3⁻"


def test_empty():
    assert to_traditional("") == ""

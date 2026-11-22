import pytest

from xiaoshen.phi import find_phi


@pytest.mark.parametrize("text, hit", [
    ("病人 A123456789 這床", "A123456789"),
    ("id b223456789", "b223456789"),
    ("病歷號：12345678 的孩子", "12345678"),
    ("病歷 1234567", "1234567"),
    ("編號 87654321 那位", "87654321"),
    ("家長電話 0912-345-678", "0912-345-678"),
    ("打 05-2765041 找", "05-2765041"),
    ("生日 2015/03/12", "2015/03/12"),
    ("104年3月12日出生", "104年3月12日"),
    ("12床 的病人", "12床"),
    ("5A03床", "5A03床"),
    ("床號 7", "床號 7"),
    ("8B191 的病人", "8B191"),           # 病房＋床號（沒有「床」字）
    ("住八B 191 那位", "八B 191"),
    ("今天要報這一床是芭比191周小旭", "芭比191"),  # 語音把「8B」聽成「芭比」
    ("這床是 23 號", "23"),
])
def test_phi_patterns(text, hit):
    hits = find_phi(text)
    assert any(hit in h or h in hit for h in hits), hits


@pytest.mark.parametrize("text", [
    "8 歲 SRNS，prednisolone 60 mg/m2 四週無效",
    "eGFR 45",
    "UPCR 2.5 g/g",
    "tacrolimus 0.05 mg/kg/day bid",
    "KDIGO 2021 建議什麼",
    "這一床是 10 歲男生",
    "這床是三歲男生，體重 13 公斤",
    "床頭抬高 30 度",
    "臥床 14 天",
    "CYP2C19 和 CYP3A4 基因型",
    "H1N1 流感",
    "維他命 B12 200 mcg",
    "ceftriaxone 1g 12 小時一次",
    "這床是 2019 年入院",
])
def test_phi_negatives(text):
    assert find_phi(text) == []


@pytest.fixture
def wards():
    """使用者（2026-11-22）：病房 8A、8B、NBC、PI；語音常把 8B 聽成「芭比」。"""
    from xiaoshen import phi

    phi.configure_wards(["8A", "8B", "NBC", "PI"], ["芭比"])
    yield
    phi.configure_wards([], [])


@pytest.mark.parametrize("text, hit", [
    ("今天要報是芭比152", "芭比152"),
    ("NBC 3 的寶寶", "NBC 3"),
    ("PI12 那位", "PI12"),
    ("8B病房152", "8B病房152"),
])
def test_ward_bed_numbers(wards, text, hit):
    from xiaoshen.phi import find_phi_kinds

    assert (hit, "bed") in find_phi_kinds(text)


@pytest.mark.parametrize("text", ["我們的芭比兒科病房", "PI 1.2", "8B 病房很忙", "NBC 3 天大"])
def test_ward_words_alone_are_not_phi(wards, text):
    assert find_phi(text) == []


def test_ward_rules_off_without_config():
    assert find_phi("今天要報是芭比152") == []

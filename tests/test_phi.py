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
])
def test_phi_negatives(text):
    assert find_phi(text) == []

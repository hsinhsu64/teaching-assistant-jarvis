from pathlib import Path

import pymupdf as fitz
import pytest


@pytest.fixture
def make_pdf(tmp_path):
    """造 PDF：pages 每個字串一頁；空字串代表無文字頁。"""
    counter = {"n": 0}

    def _make(pages: list[str], folder: Path | None = None, name: str | None = None) -> Path:
        counter["n"] += 1
        folder = folder or tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name or f"deck{counter['n']}.pdf")
        doc = fitz.open()
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        doc.save(path)
        doc.close()
        return path

    return _make


BANK_HEADER = ["簡報檔名", "頁碼", "題目", "選項A", "選項B", "選項C", "選項D", "選項E", "選項F", "正解", "解析", "出處"]


@pytest.fixture
def make_bank(tmp_path):
    """造題庫 Excel：rows 為資料列（不含標題列）。"""

    def _make(rows: list[list], folder: Path | None = None) -> Path:
        from openpyxl import Workbook

        folder = folder or tmp_path
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "題庫.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(BANK_HEADER)
        for row in rows:
            ws.append([None if v == "" else v for v in row])
        wb.save(path)
        return path

    return _make


@pytest.fixture
def config(tmp_path):
    from xiaoshen.config import FOLDERS, Config

    root = tmp_path / "data"
    for name in FOLDERS:
        (root / name).mkdir(parents=True)
    return Config(data_root=root)


@pytest.fixture
def bank_question():
    from xiaoshen.quiz import Question

    return Question("D-p2", "SRNS 一線？", ["CYC", "CNI", "RTX", "MMF"], 1, "解析", "KDIGO")


@pytest.fixture
def state(config, make_pdf, bank_question):
    """3 頁簡報 D.pdf；題庫第 2 頁有一題。"""
    from xiaoshen.state import AppState

    deck = make_pdf(["Case 1 " * 5, "Case 2 " * 5, "Case 3 " * 5], folder=config.folder("簡報"), name="D.pdf")
    st = AppState(config, {("D", 2): bank_question})
    st.play(deck)  # 既有測試假設投影中（§6.0 起開機預設是主畫面）
    return st


@pytest.fixture
def state_factory(tmp_path):
    """以 tmp 資料根目錄建 AppState（題庫為空）；可覆蓋 Config 欄位。"""
    from xiaoshen.config import FOLDERS, Config
    from xiaoshen.state import AppState

    def _make(**overrides) -> AppState:
        if "data_root" not in overrides:
            root = tmp_path / "data2"
            for name in FOLDERS:
                (root / name).mkdir(parents=True, exist_ok=True)
            overrides["data_root"] = root
        return AppState(Config(**overrides), {})

    return _make


@pytest.fixture
def state_with_scanned_deck(state_factory, make_pdf):
    st = state_factory()
    st.open_deck(make_pdf(["", ""], folder=st.config.folder("簡報"), name="Scan.pdf"))
    return st


@pytest.fixture(autouse=True)
def no_real_openevidence(request, monkeypatch):
    """測試絕不呼叫真的 OpenEvidence（自動改查 OE 後，任何沒注入假 ask 的測試都會走到這裡）。"""
    if request.module.__name__.endswith("test_oe_client"):
        return
    import xiaoshen.oe_client as oe

    def refuse(*a, **k):
        raise oe.OEError("測試環境不連 OpenEvidence")

    monkeypatch.setattr(oe, "_run", refuse)


@pytest.fixture(autouse=True)
def _reset_persona():
    """每個測試前把助理名稱、專科、會議類型重設成預設值，避免互相影響。"""
    from xiaoshen.persona import Persona, set_persona, set_wake_word

    set_persona(Persona())
    set_wake_word("賈維斯")
    yield

import threading

import pymupdf
import pytest

from xiaoshen.slide_ocr import OCR_MARK, SlideReader, image_heavy_pages


@pytest.fixture
def image_deck(tmp_path):
    """3 頁：第 1 頁純文字、第 2 頁一張圖＋短標題（以圖片為主）、第 3 頁有圖但文字很多。"""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), 0)
    pix.set_rect(pix.irect, (200, 30, 30))
    png = pix.tobytes("png")
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Plain text slide " * 20)
    p = doc.new_page()
    p.insert_text((72, 72), "An Example")
    p.insert_image(pymupdf.Rect(72, 100, 400, 400), stream=png)
    p = doc.new_page()
    for k in range(12):
        p.insert_text((72, 72 + k * 16), f"Line {k}: lots of words on this slide")
    p.insert_image(pymupdf.Rect(72, 300, 200, 400), stream=png)
    path = tmp_path / "簡報" / "Img.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def test_image_heavy_pages(image_deck):
    from xiaoshen.deck import Deck

    assert image_heavy_pages(Deck(image_deck)) == [1]  # 0-based：第 2 頁


class FakeTranscriber:
    def __init__(self, text="pH 7.299  PCO2 42.5  HCO3 20.4", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    def __call__(self, png: bytes) -> str:
        self.calls.append(len(png))
        if self.fail:
            raise RuntimeError("503")
        return self.text


def test_reader_transcribes_and_caches(state_factory, image_deck, tmp_path):
    st = state_factory()
    st.open_deck(image_deck)
    tr = FakeTranscriber()
    reader = SlideReader(st, tr, tmp_path / "ocr")
    reader.run_once()
    assert st.page_ocr == {1: "pH 7.299  PCO2 42.5  HCO3 20.4"} and len(tr.calls) == 1
    assert st.ocr_progress == {"done": 1, "total": 1}
    # 重開同一份簡報：用快取，不再呼叫
    st.open_deck(image_deck)
    tr2 = FakeTranscriber("different")
    SlideReader(st, tr2, tmp_path / "ocr").run_once()
    assert tr2.calls == [] and st.page_ocr[1].startswith("pH 7.299")


def test_reader_failure_skips_page(state_factory, image_deck, tmp_path):
    st = state_factory()
    st.open_deck(image_deck)
    SlideReader(st, FakeTranscriber(fail=True), tmp_path / "ocr").run_once()
    assert st.page_ocr == {} and st.ocr_progress == {"done": 0, "total": 1, "failed": 1}


def test_reader_discards_result_if_deck_changed(state_factory, image_deck, make_pdf, tmp_path):
    st = state_factory()
    st.open_deck(image_deck)
    other = make_pdf(["Other deck"], folder=st.config.folder("簡報"), name="Other.pdf")
    gate = threading.Event()

    def slow(png):
        st.open_deck(other)  # 抄錄途中換了簡報
        return "stale"

    SlideReader(st, slow, tmp_path / "ocr").run_once()
    assert st.page_ocr == {}


def test_deck_sources_include_ocr_text(state_factory, image_deck, tmp_path):
    from fakes import FakeLLM
    from xiaoshen.jobs import JobRunner
    from xiaoshen.library import Library

    st = state_factory()
    st.open_deck(image_deck)
    st.page_ocr = {1: "pH 7.299 PCO2 42.5 HCO3 20.4 BE -5.8"}
    jr = JobRunner(st, FakeLLM({}), st.config, Library(st.config.folder("參考資料")))
    pages = jr.sources("deck", [])[0][1]
    assert OCR_MARK in pages[1] and "HCO3 20.4" in pages[1] and OCR_MARK not in pages[0]
    st.page = 1
    assert "HCO3 20.4" in jr.sources("page", [])[0][1][1]

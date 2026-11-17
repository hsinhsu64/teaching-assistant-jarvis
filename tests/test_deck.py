import os
import time

import pytest

from xiaoshen.deck import Deck, DeckError, list_decks


def test_page_count_and_text(make_pdf):
    d = Deck(make_pdf(["Case 1 nephrotic syndrome", "Case 2 SRNS"]))
    assert d.page_count == 2 and "SRNS" in d.page_text(1)


def test_name_is_stem(make_pdf):
    assert Deck(make_pdf(["x"], name="Case_SRNS.pdf")).name == "Case_SRNS"


def test_render_png_is_png_and_cached(make_pdf):
    d = Deck(make_pdf(["a" * 50]))
    png = d.render_png(0, width=800)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and d.render_png(0, width=800) is png


def test_render_out_of_range(make_pdf):
    d = Deck(make_pdf(["a"]))
    with pytest.raises(IndexError):
        d.render_png(5)


def test_scanned_detection(make_pdf):
    assert Deck(make_pdf(["", ""])).is_scanned()
    assert not Deck(make_pdf(["x" * 100])).is_scanned()


def test_corrupt_pdf_raises(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not a pdf")
    with pytest.raises(DeckError):
        Deck(p)


def test_list_decks_newest_first(tmp_path, make_pdf):
    old = make_pdf(["a"], name="old.pdf")
    new = make_pdf(["b"], name="new.pdf")
    t = time.time()
    os.utime(old, (t - 100, t - 100))
    os.utime(new, (t, t))
    (tmp_path / "notes.txt").write_text("x")
    assert list_decks(tmp_path) == [new, old]


def test_list_decks_missing_folder(tmp_path):
    assert list_decks(tmp_path / "none") == []


def test_pdf_file_not_locked_while_open(make_pdf, tmp_path):
    # Windows：開著簡報時，PowerPoint 重新匯出或 Drive 同步要能覆蓋檔案
    path = make_pdf(["old " * 10], name="Live.pdf")
    d = Deck(path)
    newer = make_pdf(["new " * 10], folder=tmp_path / "other", name="Live.pdf")
    os.replace(newer, path)
    assert "old" in d.page_text(0)


def test_deck_access_is_serialized(make_pdf, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    d = Deck(make_pdf(["p"] * 6))
    active, peak, lock = [0], [0], threading.Lock()
    original = Deck._page

    def probe(self, index):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return original(self, index)

    monkeypatch.setattr(Deck, "_page", probe)
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda i: d.render_png(i, width=400), range(6)))
    assert peak[0] == 1


def test_text_poor_slides_are_not_scanned(make_pdf):
    # 投影片常只有短標題或整頁圖片；只要有文字層就不是掃描檔
    assert not Deck(make_pdf(["Case A", "", "Echo"])).is_scanned()

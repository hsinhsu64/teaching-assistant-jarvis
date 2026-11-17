"""投影片轉圖放到子行程：轉圖不再卡住同一行程裡的語音播放。"""

import os
import runpy
from concurrent.futures.process import BrokenProcessPool

import pytest

import xiaoshen.deck as deck_mod
from xiaoshen.deck import Deck
from xiaoshen.render_proc import ProcessRenderer


@pytest.fixture(scope="module")
def renderer():
    r = ProcessRenderer(workers=1)
    yield r
    r.close()


@pytest.fixture
def use_renderer(renderer):
    deck_mod.set_renderer(renderer)
    yield renderer
    deck_mod.set_renderer(None)


def test_process_render_matches_local(make_pdf, renderer):
    d = Deck(make_pdf(["Case 1 nephrotic syndrome"]))
    local = Deck(d.path).render_png(0, width=400)
    assert renderer.render(str(d.path), d.content_hash, 0, 400) == local


def test_deck_renders_through_renderer(make_pdf, use_renderer, monkeypatch):
    d = Deck(make_pdf(["a" * 50, "b" * 50]))

    def no_local(*a, **k):
        raise AssertionError("不應該在主行程轉圖")

    monkeypatch.setattr(deck_mod, "_render_page", no_local)
    png = d.render_png(1, width=400)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and d.render_png(1, width=400) is png


def test_changed_file_falls_back_to_local(make_pdf, tmp_path, renderer):
    # 檔案在開啟後被覆蓋（PowerPoint 重新匯出）：子行程讀到的內容對不上，改在主行程轉開啟時的內容
    path = make_pdf(["old " * 10], name="Live.pdf")
    d = Deck(path)
    expected = Deck(path).render_png(0, width=400)  # 還沒設定子行程：在本行程轉
    deck_mod.set_renderer(renderer)
    use_renderer = renderer
    newer = make_pdf(["new " * 10], folder=tmp_path / "other", name="Live.pdf")
    os.replace(newer, path)
    assert use_renderer.render(str(path), d.content_hash, 0, 400) is None
    try:
        assert d.render_png(0, width=400) == expected
    finally:
        deck_mod.set_renderer(None)


def test_broken_pool_is_replaced(make_pdf, monkeypatch):
    r = ProcessRenderer(workers=1)
    try:
        d = Deck(make_pdf(["x" * 20]))
        pool = r._get_pool()

        def broken(*a, **k):
            raise BrokenProcessPool("worker died")

        monkeypatch.setattr(pool, "submit", broken)
        assert r.render(str(d.path), d.content_hash, 0, 300) is None
        assert r.render(str(d.path), d.content_hash, 0, 300)[:4] == b"\x89PNG"
    finally:
        r.close()


def test_entry_module_does_not_start_app_in_child(monkeypatch):
    # Windows 用 spawn 開子行程，會以 __mp_main__ 重新載入主模組；不能在子行程裡再啟動一次助理
    import xiaoshen.launcher as launcher

    called = []
    monkeypatch.setattr(launcher, "main", lambda: called.append(1))
    runpy.run_module("xiaoshen.__main__", run_name="__mp_main__")
    assert called == []


def test_child_ignores_ctrl_c():
    # Windows 按 Ctrl+C 會送到整個主控台：子程式各自印一長串 KeyboardInterrupt。由主程式負責關閉子程式
    import signal

    from xiaoshen import render_proc

    before = signal.getsignal(signal.SIGINT)
    try:
        render_proc._init_child()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, before)
    assert ProcessRenderer()._pool_kwargs()["initializer"] is render_proc._init_child



def test_run_summary_render_in_child_process(tmp_path):
    # 審查 #4：摘要簡報排版放子行程（不卡語音）；Summary 要能 pickle 過去
    from xiaoshen.source_text import SourceDoc
    from xiaoshen.summary import Summary, SummaryPoint, SummarySlide
    from xiaoshen.summary_deck import render_summary_pdf

    s = Summary(SourceDoc("T", "c", ["x"], True, True),
                [SummarySlide("章", [SummaryPoint("重點", "A quoted sentence here.", 1, True)])], None, "m")
    r = ProcessRenderer(workers=1)
    try:
        assert r.run(render_summary_pdf, s, tmp_path / "s.pdf") == 2
    finally:
        r.close()
    assert (tmp_path / "s.pdf").stat().st_size > 0

"""最終審查修正：執行緒鎖、轉檔互斥、參考資料索引快取、識別資訊規則的漏網寫法。"""

import os
import threading
import time

import pytest

from xiaoshen.converter import convert_pending
from xiaoshen.jobs import JobRunner
from xiaoshen.library import Library
from xiaoshen.phi import find_phi

from fakes import FakeLLM

QUOTE = "We recommend calcineurin inhibitors as first-line therapy"


@pytest.fixture
def deck_state(state_factory, make_pdf):
    st = state_factory()
    st.open_deck(make_pdf(["Intro " * 5, QUOTE + " for SRNS."], folder=st.config.folder("簡報"), name="D.pdf"))
    return st


def answered():
    return {"status": "answered", "summary_zh": "s", "clarify_zh": "",
            "cards": [{"point_zh": "p", "quote": QUOTE, "source_label": "今天的簡報", "page": 2}]}


# ---- I3：工作槽的檢查與寫回必須在鎖內 ----

def test_answer_write_back_waits_for_state_lock(deck_state):
    jr = JobRunner(deck_state, FakeLLM(answered()), deck_state.config, Library(deck_state.config.folder("參考資料")))
    with deck_state.lock:
        jr.ask("q", "deck", [])
        time.sleep(0.3)  # 工作已完成，但寫回必須等鎖
        assert deck_state.answer is None
    jr.wait_idle()
    assert deck_state.answer is not None


def test_start_run_waits_for_state_lock(deck_state, bank_question):
    done = threading.Event()

    def other():
        deck_state.start_run("single", [bank_question])
        done.set()

    with deck_state.lock:
        t = threading.Thread(target=other)
        t.start()
        assert not done.wait(0.3)
    t.join(2)
    assert done.is_set()


# ---- I4：同一時間只能有一個 PowerPoint 轉檔 ----

def test_conversions_never_overlap(tmp_path, make_pdf):
    a, b = tmp_path / "a", tmp_path / "b"
    for folder in (a, b):
        folder.mkdir()
        (folder / "T.pptx").write_bytes(b"x")
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow_convert(src, dst):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        make_pdf(["converted " * 5], folder=dst.parent, name=dst.name)
        with lock:
            active[0] -= 1

    threads = [threading.Thread(target=convert_pending, args=(f, slow_convert)) for f in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert peak[0] == 1


# ---- I5：參考資料只在檔案變動時重新抽字 ----

def test_library_refresh_reuses_unchanged_files(tmp_path, make_pdf, monkeypatch):
    path = make_pdf(["text " * 5], folder=tmp_path, name="A.pdf")
    lib = Library(tmp_path)
    calls = []
    real = Library._index
    monkeypatch.setattr(Library, "_index", staticmethod(lambda p: calls.append(p.name) or real(p)))
    lib.refresh()
    lib.refresh()
    assert calls == ["A.pdf"]
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    lib.refresh()
    assert calls == ["A.pdf", "A.pdf"]


# ---- I6：識別資訊常見的其他打法 ----

@pytest.mark.parametrize("text", [
    "家長電話 0912 345 678",
    "生日 2019 年 5 月 3 日",
    "生日 2019 / 05 / 03",
    "bed 12 的孩子",
    "the kid in 12 bed",
    "chart no 654321",
    "MRN: 7654321",
])
def test_phi_other_typing_styles(text):
    assert find_phi(text), text


@pytest.mark.parametrize("text", [
    "K 3.5-5.0 mEq/L 正常嗎",
    "prednisolone 60 mg/m2/day 四週",
    "Na 135 到 145",
])
def test_phi_lab_values_not_flagged(text):
    assert find_phi(text) == []

import os
import time

import pytest

from pathlib import Path, PurePosixPath

from xiaoshen import converter
from xiaoshen.converter import convert_pending, find_sources, mac_export_script, needs_conversion
from xiaoshen.platform_support import IS_WINDOWS

BS = chr(92)


def touch(path, mtime):
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def fake_convert(src, dst):
    dst.write_bytes(b"%PDF-1.4 fake " + src.name.encode())


def test_find_sources_includes_ppt_and_pptx_skips_lock_files(tmp_path):
    t = time.time()
    a = touch(tmp_path / "A.pptx", t)
    b = touch(tmp_path / "B.ppt", t)
    touch(tmp_path / "~$A.pptx", t)
    touch(tmp_path / "C.pdf", t)
    assert sorted(find_sources(tmp_path)) == sorted([a, b])
    assert find_sources(tmp_path / "missing") == []


def test_needs_conversion_rules(tmp_path):
    t = time.time()
    src = touch(tmp_path / "A.pptx", t)
    assert needs_conversion(src)                      # 沒有 PDF
    touch(tmp_path / "A.pdf", t + 10)
    assert not needs_conversion(src)                  # PDF 較新
    os.utime(src, (t + 20, t + 20))
    assert needs_conversion(src)                      # pptx 改過了


def test_convert_pending_converts_and_stamps_source_time(tmp_path):
    t = time.time() - 1000
    src = touch(tmp_path / "Case1.pptx", t)
    report = convert_pending(tmp_path, fake_convert)
    pdf = tmp_path / "Case1.pdf"
    assert report.converted == ["Case1.pptx"] and report.failed == []
    assert pdf.read_bytes().startswith(b"%PDF")
    # PDF 帶原檔時間：「最新簡報」仍依 pptx 的修改時間排序
    assert abs(pdf.stat().st_mtime - t) < 2
    assert convert_pending(tmp_path, fake_convert).converted == []   # 不重複轉


def test_convert_pending_keeps_newer_pdf(tmp_path):
    t = time.time()
    touch(tmp_path / "Case1.pptx", t - 100)
    (tmp_path / "Case1.pdf").write_bytes(b"%PDF hand-made")
    os.utime(tmp_path / "Case1.pdf", (t, t))
    assert convert_pending(tmp_path, fake_convert).converted == []
    assert (tmp_path / "Case1.pdf").read_bytes() == b"%PDF hand-made"


def test_failed_conversion_leaves_no_partial_pdf_and_reports_once(tmp_path):
    touch(tmp_path / "Bad.pptx", time.time())

    def broken(src, dst):
        dst.write_bytes(b"partial")
        raise RuntimeError("PowerPoint 無法開啟")

    seen: set = set()
    r1 = convert_pending(tmp_path, broken, already_reported=seen)
    r2 = convert_pending(tmp_path, broken, already_reported=seen)
    assert not (tmp_path / "Bad.pdf").exists()
    assert [name for name, _ in r1.failed] == ["Bad.pptx"] and "PowerPoint" in r1.failed[0][1]
    assert r2.failed == []          # 同一版檔案的失敗不重複回報
    assert r2.attempted == ["Bad.pptx"]   # 但仍會重試（例如 Drive 還在同步）


def test_one_failure_does_not_stop_others(tmp_path):
    t = time.time()
    touch(tmp_path / "A.pptx", t)
    touch(tmp_path / "B.pptx", t)

    def half(src, dst):
        if src.stem == "A":
            raise RuntimeError("壞檔")
        fake_convert(src, dst)

    r = convert_pending(tmp_path, half)
    assert r.converted == ["B.pptx"] and [n for n, _ in r.failed] == ["A.pptx"]


@pytest.mark.skipif(not IS_WINDOWS or not converter.powerpoint_available(),
                    reason="需要 Windows 與本機 PowerPoint")
def test_real_powerpoint_conversion(tmp_path):
    from pptx import Presentation

    from xiaoshen.converter import powerpoint_convert
    from xiaoshen.deck import Deck

    prs = Presentation()
    for title in ["Case A", "Case B"]:
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = title
    src = tmp_path / "真實轉檔.pptx"
    prs.save(src)
    report = convert_pending(tmp_path, powerpoint_convert)
    assert report.converted == ["真實轉檔.pptx"], report.failed
    deck = Deck(tmp_path / "真實轉檔.pdf")
    assert deck.page_count == 2 and "Case B" in deck.page_text(1)


# ---- Mac：AppleScript ----

def test_mac_script_quotes_paths():
    s = mac_export_script(PurePosixPath('/U/a "b".pptx'), PurePosixPath("/U/a.pdf"))
    assert 'a \\"b\\".pptx' in s and "save" in s and "PDF" in s


def test_mac_script_escapes_backslash_before_quote():
    s = mac_export_script(PurePosixPath('/U/x@"y.pptx'.replace("@", BS)), PurePosixPath("/U/o.pdf"))
    assert 'set srcPath to "/U/x' + BS * 3 + '"y.pptx"' in s   # 反斜線先加倍（2）、引號再加一個（1）


def _script():
    return mac_export_script(PurePosixPath("/U/a.pptx"), PurePosixPath("/U/a.pdf"))


def test_mac_script_paths_and_save_as_pdf():
    s = _script()
    assert 'set srcPath to "/U/a.pptx"' in s and 'set dstPath to "/U/a.pdf"' in s
    assert "save pres in POSIX file dstPath as save as PDF" in s


def test_mac_script_matches_by_path_hfs_or_name_before_open():
    s = _script()
    assert 'set srcName to "a.pptx"' in s and "set srcHfs to (POSIX file srcPath) as text" in s
    pre = s[:s.index("open POSIX file srcPath")]
    assert ("(full name of p) is srcPath or (full name of p) is srcHfs "
            "or (name of p) is srcName") in pre       # 已開著：路徑（POSIX／HFS）或檔名相符就算使用者的


def test_mac_script_post_open_lookup_falls_back_name_then_active():
    s = _script()
    post = s[s.index("open POSIX file srcPath"):]
    by_path = post.index("(full name of p) is srcPath or (full name of p) is srcHfs")
    by_name = post.index("if (name of p) is srcName then")
    active = post.index("set pres to active presentation")
    assert by_path < by_name < active                # 完整路徑 -> 檔名 -> 最後才用 active presentation
    assert "active presentation" not in s[:s.index("open POSIX file srcPath")]


def test_mac_script_open_of_already_open_file_is_not_ours():
    s = _script()
    assert "set countBefore to count of presentations" in s
    assert "if (count of presentations) is countBefore then set openedByUs to false" in s


def test_mac_script_close_and_quit_are_gated():
    s = _script()
    lines = [ln.strip() for ln in s.splitlines()]
    for n, ln in enumerate(lines):
        if ln.startswith("close pres"):
            # 要嘛同一行帶 openedByUs 條件，要嘛位於 "if openedByUs then" 區塊內
            assert "openedByUs" in ln or any(l == "if openedByUs then" for l in lines[max(0, n - 2):n])
    assert "close every" not in s and "close all" not in s
    quits = [ln for ln in lines if ln.endswith(" quit") or ln == "quit"]
    assert quits and all("not wasRunning" in ln for ln in quits)


def test_mac_script_cleans_up_inside_on_error_and_reraises():
    s = _script()
    head, handler = s.split("on error errMsg number errNum")
    assert "try" in head and "open POSIX file srcPath" in head and "save pres" in head
    assert "close pres saving no" in handler and "openedByUs" in handler
    assert "if not wasRunning then quit" in handler
    assert handler.rstrip().endswith("end try") and "error errMsg number errNum" in handler


def test_mac_powerpoint_available_checks_app_path(monkeypatch, tmp_path):
    monkeypatch.setattr(converter, "IS_MAC", True)
    monkeypatch.setattr(converter, "MAC_POWERPOINT", tmp_path / "nope.app")
    assert converter.powerpoint_available() is False
    (tmp_path / "PP.app").mkdir()
    monkeypatch.setattr(converter, "MAC_POWERPOINT", tmp_path / "PP.app")
    assert converter.powerpoint_available() is True


class _Proc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr, self.stdout = returncode, stderr, ""


def test_mac_convert_runs_osascript(monkeypatch, tmp_path):
    monkeypatch.setattr(converter, "IS_MAC", True)
    src, dst = tmp_path / "a.pptx", tmp_path / "out.pdf"
    src.write_bytes(b"x")
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        dst.write_bytes(b"%PDF-1.4")
        return _Proc()

    converter.powerpoint_convert(src, dst, run=run)
    cmd, kw = calls[0]
    assert cmd[:2] == ["osascript", "-e"] and cmd[2] == mac_export_script(src.resolve(), dst.resolve())
    assert kw["timeout"] == 120 and kw["capture_output"] is True and kw["text"] is True


def test_mac_convert_nonzero_exit_raises_with_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(converter, "IS_MAC", True)
    with pytest.raises(RuntimeError) as e:
        converter.powerpoint_convert(tmp_path / "a.pptx", tmp_path / "o.pdf",
                                     run=lambda cmd, **kw: _Proc(1, "execution error: boom"))
    assert "boom" in str(e.value) and str(e.value).startswith("PowerPoint")


def test_mac_convert_timeout_raises(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(converter, "IS_MAC", True)

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)

    with pytest.raises(RuntimeError, match="逾時"):
        converter.powerpoint_convert(tmp_path / "a.pptx", tmp_path / "o.pdf", run=run)


@pytest.mark.parametrize("content", [None, b""])
def test_mac_convert_missing_or_empty_pdf_raises(monkeypatch, tmp_path, content):
    monkeypatch.setattr(converter, "IS_MAC", True)
    dst = tmp_path / "o.pdf"

    def run(cmd, **kw):
        if content is not None:
            dst.write_bytes(content)
        return _Proc()

    with pytest.raises(RuntimeError, match="沒有產生 PDF"):
        converter.powerpoint_convert(tmp_path / "a.pptx", dst, run=run)

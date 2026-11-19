from xiaoshen.library import Library


def test_indexes_pdfs_page_by_page(tmp_path, make_pdf):
    make_pdf(["KDIGO page one text " * 3, "page two CNI " * 3], folder=tmp_path, name="KDIGO 2021 GN.pdf")
    lib = Library(tmp_path)
    assert lib.refresh() == []
    d = lib.docs["KDIGO 2021 GN"]
    assert len(d.pages) == 2 and "CNI" in d.pages[1]
    assert not d.scanned


def test_find_exact_substring_fuzzy(tmp_path, make_pdf):
    make_pdf(["text " * 5], folder=tmp_path, name="KDIGO 2021 GN.pdf")
    make_pdf(["text " * 5], folder=tmp_path, name="IPNA 2020 SRNS.pdf")
    lib = Library(tmp_path)
    lib.refresh()
    assert [d.name for d in lib.find("KDIGO 2021 GN")] == ["KDIGO 2021 GN"]
    assert lib.find("KDIGO")[0].name == "KDIGO 2021 GN"
    assert lib.find("kdigo gn")[0].name == "KDIGO 2021 GN"
    assert lib.find("IPNA SRNS 2020")[0].name == "IPNA 2020 SRNS"
    assert lib.find("hyperkalemia") == []


def test_scanned_reference_flagged(tmp_path, make_pdf):
    make_pdf(["", ""], folder=tmp_path, name="Scan.pdf")
    lib = Library(tmp_path)
    warns = lib.refresh()
    assert any("Scan" in w and "掃描檔" in w for w in warns)
    assert lib.docs["Scan"].scanned


def test_broken_file_warned(tmp_path):
    (tmp_path / "Bad.pdf").write_bytes(b"not a pdf")
    lib = Library(tmp_path)
    warns = lib.refresh()
    assert any("Bad" in w and "無法開啟" in w for w in warns)
    assert "Bad" not in lib.docs


def test_add_file_avoids_overwrite(tmp_path, make_pdf):
    folder = tmp_path / "refs"
    make_pdf(["original " * 5], folder=folder, name="Guide.pdf")
    src = make_pdf(["uploaded " * 5], folder=tmp_path / "up", name="Guide.pdf")
    lib = Library(folder)
    lib.refresh()
    doc = lib.add_file(src)
    assert doc.name == "Guide (2)"
    assert "original" in lib.docs["Guide"].pages[0]
    assert "uploaded" in lib.docs["Guide (2)"].pages[0]


def test_total_chars(tmp_path, make_pdf):
    make_pdf(["abcde", "fghij"], folder=tmp_path, name="A.pdf")
    lib = Library(tmp_path)
    lib.refresh()
    assert lib.total_chars(["A"]) == sum(len(p) for p in lib.docs["A"].pages)


def test_refresh_converts_pptx_first(tmp_path, make_pdf):
    (tmp_path / "Talk.pptx").write_bytes(b"fake")
    calls = []

    def fake_convert(src, dst):
        calls.append(src.name)
        make_pdf(["converted " * 5], folder=dst.parent, name=dst.name)

    lib = Library(tmp_path, convert=fake_convert)
    lib.refresh()
    assert calls == ["Talk.pptx"]
    assert "Talk" in lib.docs


from xiaoshen.library import rank_names


def test_rank_names_exact_first():
    assert rank_names("酸鹼電解質平衡", ["酸鹼電解質平衡 v2", "酸鹼電解質平衡"])[0] == "酸鹼電解質平衡"


def test_rank_names_substring_and_case():
    assert rank_names("srns", ["SRNS 治療指引", "VCUG 判讀"]) == ["SRNS 治療指引"]


def test_rank_names_no_match_and_empty():
    assert rank_names("腎移植", ["VCUG 判讀"]) == [] and rank_names("  ", ["A"]) == []


def test_refresh_can_skip_pptx_conversion_at_startup(tmp_path, make_pdf):
    """開機時只整理已有的 PDF；pptx 之後在背景轉。"""
    make_pdf(["Ready"], folder=tmp_path, name="Ready.pdf")
    (tmp_path / "Slow.pptx").write_bytes(b"pptx")
    calls = []
    lib = Library(tmp_path, convert=lambda s, d: calls.append(s.name))
    lib.refresh(convert=False)
    assert calls == [] and set(lib.docs) == {"Ready"}

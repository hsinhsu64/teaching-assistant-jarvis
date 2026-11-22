"""逐字稿純邏輯：講者對應、小群歸併、講者摘要、去識別候選、組 json 與 txt。"""

import json

import pytest

from xiaoshen import transcript_build as tb
from xiaoshen.phi import find_phi, find_phi_kinds


def test_assign_by_max_overlap():
    asr = [{"start": 0.0, "end": 4.0, "text": "大家早"},
           {"start": 4.0, "end": 10.0, "text": "今天的病例"},
           {"start": 50.0, "end": 52.0, "text": "沒人講話的地方"}]
    diar = [{"start": 0.0, "end": 5.0, "speaker": 1},
            {"start": 5.0, "end": 12.0, "speaker": 0}]
    out = tb.assign_speakers(asr, diar)
    assert [l["speaker"] for l in out] == [1, 0, None]
    assert out[0]["text"] == "大家早" and out[0]["start"] == 0.0


def _diar(spans):
    return [{"start": s, "end": e, "speaker": k} for s, e, k in spans]


def test_merge_small_into_most_similar():
    diar = _diar([(0, 100, 0), (100, 200, 1), (200, 210, 2), (210, 215, 3)])
    emb = {0: [1.0, 0.0], 1: [0.0, 1.0], 2: [0.1, 0.9], 3: [0.95, 0.05]}
    m = tb.merge_small_clusters(diar, emb, min_seconds=30, max_groups=8)
    assert m[2] == m[1] and m[3] == m[0] and m[0] != m[1]
    assert sorted(set(m.values())) == [0, 1]


def test_merge_caps_groups():
    # 10 個都超過 30 秒的群，最多留 3 群；新編號依發言長度排（最長的是 0）
    diar = _diar([(i * 100, i * 100 + 40 + i, i) for i in range(10)])
    emb = {i: [1.0, i / 10] for i in range(10)}
    m = tb.merge_small_clusters(diar, emb, min_seconds=30, max_groups=3)
    assert len(set(m.values())) == 3
    assert set(m) == set(range(10))
    totals = {}
    for d in diar:
        totals[m[d["speaker"]]] = totals.get(m[d["speaker"]], 0) + d["end"] - d["start"]
    assert totals[0] == max(totals.values())


def test_merge_all_small_keeps_largest():
    diar = _diar([(0, 10, 0), (10, 15, 1)])
    m = tb.merge_small_clusters(diar, {0: [1, 0], 1: [0, 1]}, min_seconds=30)
    assert m == {0: 0, 1: 0}


def test_summaries_sorted_and_samples():
    lines = [{"start": 0, "end": 2, "text": "短", "speaker": 0},
             {"start": 2, "end": 20, "text": "第一位講很長的一句話", "speaker": 1},
             {"start": 20, "end": 22, "text": "嗯", "speaker": 1},
             {"start": 22, "end": 30, "text": "中等長度的話", "speaker": 1},
             {"start": 30, "end": 31, "text": "對", "speaker": 1},
             {"start": 31, "end": 33, "text": "沒有講者", "speaker": None}]
    s = tb.speaker_summaries(lines)
    assert [x["speaker"] for x in s] == [1, 0]
    assert s[0]["seconds"] == 29.0
    assert s[0]["samples"] == ["第一位講很長的一句話", "中等長度的話", "嗯"]


def test_find_phi_kinds_matches_find_phi():
    text = "病歷號 1234567，A123456789，0912-345-678，2015/3/4 生日，12床"
    kinds = find_phi_kinds(text)
    assert [t for t, _ in kinds] == find_phi(text)
    d = dict(kinds)
    assert d["A123456789"] == "id" and d["0912-345-678"] == "phone"
    assert d["12床"] == "bed" and d["2015/3/4"] == "birthday"
    assert d["病歷號 1234567"] == "chart"


def test_candidates_rules_and_names():
    lines = [{"start": 0, "end": 1, "text": "王小明 病歷號 1234567 住 12床", "speaker": 0},
             {"start": 1, "end": 2, "text": "王小明今天燒退了", "speaker": 1}]
    c = tb.phi_candidates(lines, ["王小明"])
    by = {x["text"]: x for x in c}
    assert by["王小明"]["kind"] == "person" and by["王小明"]["count"] == 2
    assert by["王小明"]["lines"] == [0, 1]
    assert by["病歷號 1234567"]["kind"] == "chart"
    assert by["12床"]["kind"] == "bed"


def test_candidates_dedup_longest_first():
    lines = [{"start": 0, "end": 1, "text": "王小明說小明很好，小明", "speaker": 0}]
    c = tb.phi_candidates(lines, ["小明", "王小明", "小明", ""])
    assert [x["text"] for x in c] == ["王小明", "小明"]
    assert [x["count"] for x in c] == [1, 3]  # 次數照原文出現次數（含長字串裡的）


def test_build_transcript_roundtrip():
    asr = [{"start": 0.0, "end": 40.0, "text": "我們來看王小明"},
           {"start": 40.0, "end": 75.0, "text": "好的"},
           {"start": 3700.0, "end": 3704.0, "text": "最後"}]
    diar = [{"start": 0.0, "end": 40.0, "speaker": 5}, {"start": 40.0, "end": 75.0, "speaker": 2},
            {"start": 3700.0, "end": 3704.0, "speaker": 2}]
    doc = tb.build_transcript("2026-11-22_0730", asr, diar, {5: [1, 0], 2: [0, 1]}, ["王小明"],
                              {"asr": "large-v3"}, {"asr": 12.5})
    doc = json.loads(json.dumps(doc, ensure_ascii=False))
    assert doc["id"] == "2026-11-22_0730"
    assert [l["speaker"] for l in doc["lines"]] == [0, 1, 1]  # 依發言長度重新編號
    assert doc["speakers"][0]["speaker"] == 0 and doc["speakers"][0]["seconds"] == 40.0
    assert doc["candidates"][0]["text"] == "王小明"
    assert doc["models"] == {"asr": "large-v3"} and doc["timing"] == {"asr": 12.5}
    txt = tb.transcript_text(doc)
    assert txt.splitlines() == ["[00:00] 講者1：我們來看王小明", "[00:40] 講者2：好的", "[1:01:40] 講者2：最後"]


def test_candidates_ignore_names_without_chinese():
    """實機：CKIP 會把英文名詞（sponsor、CTC）當人名；病人姓名候選只收含中文字的。"""
    lines = [{"start": 0, "end": 1, "text": "sponsor 說 CTC 要看王小明", "speaker": 0}]
    c = tb.phi_candidates(lines, ["sponsor", "CTC", "王小明"])
    assert [x["text"] for x in c] == ["王小明"]


def test_summaries_samples_unique():
    """實機：Whisper 會重複同一句；代表句不重複。"""
    lines = [{"start": 0, "end": 5, "text": "比如說現在", "speaker": 0},
             {"start": 5, "end": 9, "text": "比如說現在", "speaker": 0},
             {"start": 9, "end": 10, "text": "好", "speaker": 0}]
    assert tb.speaker_summaries(lines)[0]["samples"] == ["比如說現在", "好"]


def test_candidates_catch_numbers_split_across_lines():
    """自測：Whisper 把電話切成兩句（「…電話是09123」「345678,…」），合起來才看得出來。"""
    lines = [{"start": 0, "end": 1, "text": "媽媽的電話是09123", "speaker": 1},
             {"start": 1, "end": 2, "text": "345678,我先記下來", "speaker": 1}]
    c = tb.phi_candidates(lines, [])
    assert [x["text"] for x in c] == ["09123345678"]
    assert c[0]["lines"] == [0, 1]


def test_candidates_catch_bed_number_misheard_as_word():
    """實測：「8B 191 床」被聽成「這一床是」「芭比191」，床號要列成候選，且比人名「芭比」長、先換。"""
    lines = [{"start": 3, "end": 5, "text": "今天要報這一床是", "speaker": 0},
             {"start": 6, "end": 7, "text": "芭比191", "speaker": 0},
             {"start": 8, "end": 9, "text": "周小旭", "speaker": 0},
             {"start": 19, "end": 21, "text": "我們的芭比兒科病房", "speaker": 0}]
    c = tb.phi_candidates(lines, ["芭比", "周小旭"])
    assert [(x["text"], x["kind"]) for x in c] == [("芭比191", "bed"), ("周小旭", "person"), ("芭比", "person")]
    assert c[0]["lines"] == [1]


def test_misheard_ward_word_is_not_a_person_candidate():
    """「芭比」是病房 8B 的錯聽，不是人名；病房＋床號整串列成床號。"""
    from xiaoshen import phi

    phi.configure_wards(["8B"], ["芭比"])
    try:
        lines = [{"start": 0, "end": 1, "text": "今天要報是芭比152", "speaker": 0},
                 {"start": 1, "end": 2, "text": "我們的芭比兒科病房", "speaker": 0}]
        c = tb.phi_candidates(lines, ["芭比", "周小旭"])
    finally:
        phi.configure_wards([], [])
    assert [(x["text"], x["kind"]) for x in c] == [("芭比152", "bed")]


def test_edit_transcript_text_and_speaker():
    """使用者（2026-11-23）：主持人可以改逐字稿的字與講者；去識別候選依新文字重算（人名辨識找到的名字保留）。"""
    import copy

    lines = [{"start": 0.0, "end": 5.0, "text": "看王小明 12床", "speaker": 0},
             {"start": 5.0, "end": 9.0, "text": "病歷號 1234567", "speaker": 0},
             {"start": 9.0, "end": 12.0, "text": "好", "speaker": 1}]
    doc = {"version": 1, "id": "2026-11-22_0730", "lines": lines, "speakers": tb.speaker_summaries(lines),
           "candidates": tb.phi_candidates(lines, ["王小明"]), "models": {}, "timing": {}}
    before = copy.deepcopy(doc)
    new = tb.edit_transcript(doc, [{"i": 1, "text": "病歷號\n7654321", "speaker": 2}, {"i": 0, "text": "看王小明 13床"}])
    assert doc == before  # 原本的不動
    assert new["lines"][1] == {"start": 5.0, "end": 9.0, "text": "病歷號 7654321", "speaker": 2}
    assert new["lines"][0]["speaker"] == 0 and new["lines"][0]["text"] == "看王小明 13床"
    texts = {c["text"] for c in new["candidates"]}
    assert {"病歷號 7654321", "13床", "王小明"} <= texts and not any("1234567" in t or "12床" == t for t in texts)
    assert sorted(s["speaker"] for s in new["speakers"]) == [0, 1, 2]


@pytest.mark.parametrize("edit", [{"i": 3, "text": "x"}, {"i": -1, "text": "x"}, {"i": 0, "speaker": -1},
                                  {"i": 0, "speaker": 100}, {"i": 0, "text": "   "}])
def test_edit_transcript_rejects_bad_edits(edit):
    lines = [{"start": 0.0, "end": 1.0, "text": "a", "speaker": 0}]
    doc = {"lines": lines, "speakers": [], "candidates": []}
    with pytest.raises(ValueError):
        tb.edit_transcript(doc, [edit])

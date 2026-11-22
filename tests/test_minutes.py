"""生成會議記錄：講者合併 → 去識別 → 二次掃描 → Gemini（假）→ 換回真名 → 存 .md／.docx。"""

import json
from datetime import datetime

import pytest

from fakes import FakeLLM
from xiaoshen import meeting_store as ms
from xiaoshen.minutes import SECTIONS, generate_minutes

MID = "2026-11-22_0730"
NOW = datetime(2026, 11, 22, 9, 0)


@pytest.fixture
def transcript(config):
    lines = [
        {"start": 0.0, "end": 30.0, "text": "今天看王小明，病歷號 1234567，住 12床", "speaker": 0},
        {"start": 30.0, "end": 40.0, "text": "王小明的尿蛋白下降了", "speaker": 1},
        {"start": 40.0, "end": 45.0, "text": "我同意", "speaker": 2},
    ]
    doc = {"version": 1, "id": MID, "lines": lines,
           "speakers": [{"speaker": 0, "seconds": 30.0, "samples": []}, {"speaker": 1, "seconds": 10.0, "samples": []},
                        {"speaker": 2, "seconds": 5.0, "samples": []}],
           "candidates": [], "models": {}, "timing": {}}
    p = ms.paths(config, MID)
    p.transcript_json.parent.mkdir(parents=True, exist_ok=True)
    p.transcript_json.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


ITEMS = [{"text": "王小明", "kind": "person"}, {"text": "病歷號 1234567", "kind": "chart"},
         {"text": "12床", "kind": "bed"}]
NAMES = {0: "周醫師", 1: "林醫師", 2: "周醫師"}


def reply(md="## 一、今天的主題與簡報\n病人A 的病例。\n## 二、病例討論重點\n〔病歷號1〕 的病人A 尿蛋白下降。",
          corrections=None):
    return FakeLLM({"minutes_md": md, "corrections": corrections or []})


def run(config, tmp_path, llm, items=ITEMS, names=NAMES):
    return generate_minutes(config, llm, MID, names, items, "病例討論", NOW, private_dir=tmp_path / "private")


def test_sent_text_has_no_original_phi(config, transcript, tmp_path):
    llm = reply()
    run(config, tmp_path, llm)
    sent = llm.calls[0]["prompt"]
    for s in ("王小明", "1234567", "12床"):
        assert s not in sent
    assert "病人A" in sent and "〔病歷號1〕" in sent and "〔床號1〕" in sent
    assert "周醫師" in sent  # 醫師名字不換代號
    audit = (tmp_path / "private" / f"{MID}.sent.txt").read_text(encoding="utf-8")
    assert "王小明" not in audit and "病人A" in audit
    mapping = json.loads((tmp_path / "private" / f"{MID}.map.json").read_text(encoding="utf-8"))
    assert mapping["病人A"] == "王小明"


def test_blocked_when_residual_phi(config, transcript, tmp_path):
    llm = reply()
    out = run(config, tmp_path, llm, items=[{"text": "王小明", "kind": "person"}])  # 漏勾病歷號與床號
    assert out["status"] == "blocked"
    assert {h["text"] for h in out["hits"]} >= {"病歷號 1234567"}
    assert all(h["context"] for h in out["hits"])
    assert llm.calls == []  # 沒有送出
    assert not ms.paths(config, MID).minutes_md.exists()


def test_minutes_reidentified_and_saved(config, transcript, tmp_path):
    out = run(config, tmp_path, reply())
    p = ms.paths(config, MID)
    assert out["status"] == "ok" and out["md"] == str(p.minutes_md) and out["docx"] == str(p.minutes_docx)
    md = p.minutes_md.read_text(encoding="utf-8")
    assert md.splitlines()[0] == "# 晨會會議記錄｜2026-11-22｜病例討論"
    assert "發言者：周醫師、林醫師" in md and "出席" not in md
    assert "王小明 的病例" in md and "病歷號 1234567 的王小明" in md
    assert "病人A" not in md
    from docx import Document
    text = "\n".join(par.text for par in Document(str(p.minutes_docx)).paragraphs)
    assert "晨會會議記錄｜2026-11-22｜病例討論" in text and "王小明 的病例" in text
    assert ms.status(config, MID, NOW) == "minutes_done"


def test_corrections_appendix(config, transcript, tmp_path):
    run(config, tmp_path, reply(corrections=[{"from": "尿蛋百", "to": "尿蛋白"}, {"from": "病人A", "to": "病人A"}]))
    md = ms.paths(config, MID).minutes_md.read_text(encoding="utf-8")
    tail = md.split("附錄：依上下文更正的詞", 1)[1]
    assert "尿蛋百 → 尿蛋白" in tail
    assert "王小明 → 王小明" not in tail  # 沒改的不列


def test_corrections_appendix_empty(config, transcript, tmp_path):
    run(config, tmp_path, reply())
    md = ms.paths(config, MID).minutes_md.read_text(encoding="utf-8")
    assert "附錄：依上下文更正的詞" in md and "（無）" in md.split("附錄：依上下文更正的詞", 1)[1]


def test_same_name_merges_speakers(config, transcript, tmp_path):
    llm = reply()
    run(config, tmp_path, llm)
    sent = llm.calls[0]["prompt"]
    assert "講者" not in sent  # 每位講者都有名字
    assert sent.count("周醫師：") == 2 and "林醫師：" in sent  # 講者1、3 同名 → 同一人


def test_unnamed_speakers_keep_numbers(config, transcript, tmp_path):
    llm = reply()
    run(config, tmp_path, llm, names={0: "周醫師"})
    sent = llm.calls[0]["prompt"]
    assert "講者2：" in sent and "講者3：" in sent


def test_unresolved_codes_reported(config, transcript, tmp_path):
    out = run(config, tmp_path, reply(md="## 一、今天的主題與簡報\n病人Ａ 與 病人C"))
    assert out["status"] == "ok"
    assert out["unresolved"] == ["病人C"]
    assert "王小明 與 病人C" in ms.paths(config, MID).minutes_md.read_text(encoding="utf-8")


def test_prompt_has_required_sections(config, transcript, tmp_path):
    llm = reply()
    run(config, tmp_path, llm)
    call = llm.calls[0]
    for title in ("一、今天的主題與簡報", "二、病例討論重點", "三、教學重點", "四、提問與回答", "五、待辦／追蹤事項"):
        assert title in SECTIONS and title in call["system"] + call["prompt"]
    assert "（聽不清）" in call["system"]
    assert call["model"] == config.model_answer
    assert set(call["schema"]["properties"]) == {"minutes_md", "corrections"}


def test_minutes_model_overrides(config, transcript, tmp_path):
    config.minutes_model = "gemini-x"
    llm = reply()
    run(config, tmp_path, llm)
    assert llm.calls[0]["model"] == "gemini-x"


def test_speaker_names_never_pseudonymized(config, transcript, tmp_path):
    """審查：人名候選「周醫」是講者名字「周醫師」的一部分時，講者名字不能被換成病人代號。"""
    llm = reply()
    items = ITEMS + [{"text": "周醫", "kind": "person"}, {"text": "林醫師", "kind": "person"}]
    run(config, tmp_path, llm, items=items)
    sent = llm.calls[0]["prompt"]
    assert sent.count("周醫師：") == 2 and "林醫師：" in sent
    mapping = json.loads((tmp_path / "private" / f"{MID}.map.json").read_text(encoding="utf-8"))
    assert set(mapping.values()) == {"王小明", "病歷號 1234567", "12床"}


def test_md_to_html_for_viewing():
    """使用者：主持人按「檢視記錄」直接看會議記錄（標題、條列、粗體；內容要跳脫）。"""
    from xiaoshen.minutes import md_to_html

    html = md_to_html("# 晨會會議記錄｜2026-11-22\n\n發言者：周醫師\n\n## 一、今天的主題與簡報\n"
                      "- **病史**：<script>x</script> 水腫\n1. 第一點\n")
    assert "<h1>晨會會議記錄｜2026-11-22</h1>" in html and "<h2>一、今天的主題與簡報</h2>" in html
    assert "<p>發言者：周醫師</p>" in html
    assert "<ul><li><b>病史</b>：&lt;script&gt;x&lt;/script&gt; 水腫</li></ul>" in html
    assert "<ol><li>第一點</li></ol>" in html
    assert "<script>" not in html


def test_phone_questions_go_into_minutes_in_time_order(config, transcript, tmp_path):
    """使用者（2026-11-23）：學員手機打字的提問列入會議記錄；依錄音時間插進逐字稿，一樣去識別。"""
    from xiaoshen.minutes import PHONE_ASKER

    llm = reply()
    qs = [{"t": 35.0, "text": "王小明要不要加 ACEi？"}]
    generate_minutes(config, llm, MID, NAMES, ITEMS, "病例討論", NOW, private_dir=tmp_path / "private", questions=qs)
    prompt = llm.calls[0]["prompt"]
    lines = prompt.split("逐字稿：\n", 1)[1].strip().splitlines()
    assert lines[1].startswith("[00:30]")
    assert lines[2] == f"[00:35] {PHONE_ASKER}：病人A要不要加 ACEi？"  # 在 0:30 那段之後、0:40 那段之前
    assert lines[3].startswith("[00:40]")
    assert "王小明" not in prompt
    assert "手機" in llm.calls[0]["system"] and "四、提問與回答" in llm.calls[0]["system"]


def test_phone_question_phi_blocks_like_transcript(config, transcript, tmp_path):
    llm = reply()
    qs = [{"t": 5.0, "text": "病歷號 7654321 那位呢？"}]  # 沒勾：換完再掃一次會擋下
    res = generate_minutes(config, llm, MID, NAMES, ITEMS, "病例討論", NOW, private_dir=tmp_path / "private",
                           questions=qs)
    assert res["status"] == "blocked" and llm.calls == []

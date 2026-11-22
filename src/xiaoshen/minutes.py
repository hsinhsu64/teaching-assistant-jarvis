"""生成會議記錄（主持人按「送出整理」後在背景執行）。

1. 依主持人填的講者名字組逐字稿（同名＝同一人）；
2. 勾選的病人資料換成代號（對照表只存在筆電本機）；
3. 換完再用規則掃一次，還有命中就停下、不送出；
4. Gemini 只根據逐字稿整理（可依上下文改同音錯字，但全部列出；不確定寫「（聽不清）」）；
5. 代號換回真名，存 紀錄\\會議記錄\\<id>.md 與 .docx（會議記錄只在院內，不去識別）。
送出的文字另存本機稽核記錄（minutes_private\\<id>.sent.txt），30 天後和錄音一起刪除。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import eventlog
from . import meeting_store as ms
from .config import Config
from .deid import code_like, pseudonymize, reidentify, residual_phi
from .transcript_build import clock, speaker_label

SECTIONS = ("一、今天的主題與簡報", "二、病例討論重點", "三、教學重點", "四、提問與回答", "五、待辦／追蹤事項")
APPENDIX = "附錄：依上下文更正的詞"
PHONE_ASKER = "與會人員（手機提問）"  # 錄音期間學員手機「詢問老師」打字的提問
MIN_TIMEOUT = 300  # 一小時的逐字稿整理要幾分鐘

SYSTEM = (
    "你是醫院晨會的書記，把晨會逐字稿整理成會議記錄，給沒參加的人與學員看。用台灣繁體中文。\n"
    "規則：\n"
    "1. 只根據逐字稿；逐字稿沒有的內容一律不寫，不推測、不補充一般醫學知識。\n"
    "2. 逐字稿是語音辨識，有同音錯字。可以依上下文改正，但每一個改過的詞都要列在 corrections（from＝逐字稿原文，to＝改成的詞）。\n"
    "3. 聽不清楚、無法確定的地方寫「（聽不清）」，不要猜。\n"
    "4. 「病人A」「〔病歷號1〕」這類代號是去識別用的，照原樣保留，不要改寫、不要加空白、不要猜真名。\n"
    "5. minutes_md 用 Markdown，依序寫這五節，每節用「## 」開頭的標題，標題文字照抄：\n"
    + "".join(f"   ## {s}\n" for s in SECTIONS)
    + "   三、教學重點：主治醫師講的觀念、引用的指引或文獻。四、提問與回答：誰問、怎麼回答。"
      "沒有內容的節寫「（無）」。不要寫標題、發言者名單或附錄（程式會加）。\n"
    f"6. 「{PHONE_ASKER}」是學員用手機打字問的問題（不是口頭發言）：放進「四、提問與回答」並標明（手機提問）；"
    "回答依逐字稿裡老師的說明，逐字稿沒有回答就寫「（逐字稿中未回答）」。\n"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "minutes_md": {"type": "string"},
        "corrections": {"type": "array", "items": {"type": "object",
                                                   "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                                                   "required": ["from", "to"]}},
    },
    "required": ["minutes_md", "corrections"],
}


def _load(config: Config, mid: str) -> dict:
    return json.loads(ms.paths(config, mid).transcript_json.read_text(encoding="utf-8"))


def _name(speaker, names: dict[int, str]) -> str:
    n = (names.get(speaker) or "").strip() if speaker is not None else ""
    return n or speaker_label(speaker)


def attendees(doc: dict, names: dict[int, str]) -> list[str]:
    """發言者：依發言長度，同名只列一次。"""
    out: list[str] = []
    for s in doc.get("speakers", []):
        n = _name(s["speaker"], names)
        if n not in out:
            out.append(n)
    return out


def transcript_rows(doc: dict, names: dict[int, str]) -> list[list]:
    """[開始秒數, 名字, 文字]；同一人連續講的合成一段。"""
    rows: list[list] = []
    for line in doc["lines"]:
        who = _name(line.get("speaker"), names)
        text = " ".join(line["text"].split())
        if not text:
            continue
        if rows and rows[-1][1] == who:
            rows[-1][2] += text
        else:
            rows.append([line["start"], who, text])
    return rows


def add_questions(rows: list[list], questions: list[dict]) -> list[list]:
    """手機提問依錄音時間插進逐字稿（插在之後第一段發言之前）。"""
    out = [list(r) for r in rows]
    for q in sorted(questions or [], key=lambda q: float(q.get("t", 0))):
        text = " ".join(str(q.get("text", "")).split())  # 不可有換行：送出前逐行對應
        if not text:
            continue
        t = float(q.get("t", 0))
        i = next((k for k, r in enumerate(out) if r[0] > t and r[1] != PHONE_ASKER), len(out))
        out.insert(i, [t, PHONE_ASKER, text])
    return out


def render_rows(rows: list[list]) -> str:
    return "\n".join(f"[{clock(t)}] {who}：{text}" for t, who, text in rows)


def named_transcript(doc: dict, names: dict[int, str]) -> str:
    """[mm:ss] 名字：文字。"""
    return render_rows(transcript_rows(doc, names))


def _not_speaker(item: dict, people: list[str]) -> bool:
    """講者（醫師、學員）的名字不換代號：人名候選和任一講者名字互相包含就略過。"""
    t = (item.get("text") or "").strip()
    if item.get("kind") != "person" or not t:
        return True
    return not any(t in p or (len(p) >= 2 and p in t) for p in people)


def _context(text: str, hit: str, width: int = 20) -> str:
    i = text.find(hit)
    if i < 0:
        return hit
    return ("…" if i > width else "") + text[max(0, i - width):i + len(hit) + width] + (
        "…" if i + len(hit) + width < len(text) else "")


def _meeting_date(mid: str) -> str:
    return mid[:10]


def compose_md(mid: str, meeting_type: str, people: list[str], body: str, corrections: list[dict]) -> str:
    lines = [f"# 晨會會議記錄｜{_meeting_date(mid)}｜{meeting_type}", "", f"發言者：{'、'.join(people)}", "",
             body.strip(), "", f"## {APPENDIX}", ""]
    lines += [f"- {c['from']} → {c['to']}" for c in corrections] or ["（無）"]
    lines += ["", f"（另存：完整逐字稿 {mid}.txt）", ""]
    return "\n".join(lines)


_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _add_runs(par, text: str) -> None:
    pos = 0
    for m in _INLINE_BOLD.finditer(text):
        if m.start() > pos:
            par.add_run(text[pos:m.start()])
        par.add_run(m.group(1)).bold = True
        pos = m.end()
    if pos < len(text):
        par.add_run(text[pos:])


def write_docx(md: str, path: Path) -> None:
    """簡單的 Markdown → Word：標題（#、##、###）、條列（-、*、1.）、一般段落、**粗體**。"""
    from docx import Document

    doc = Document()
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            doc.add_heading(m.group(2).strip(), level=len(m.group(1)) - 1 if len(m.group(1)) > 1 else 0)
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            _add_runs(doc.add_paragraph(style="List Bullet"), m.group(1))
            continue
        m = re.match(r"^\s*\d+[.)、]\s+(.*)$", line)
        if m:
            _add_runs(doc.add_paragraph(style="List Number"), m.group(1))
            continue
        _add_runs(doc.add_paragraph(), line)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.docx")
    doc.save(str(tmp))
    tmp.replace(path)


def _inline_html(text: str) -> str:
    import html

    out, pos = [], 0
    for m in _INLINE_BOLD.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append(f"<b>{html.escape(m.group(1))}</b>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def md_to_html(md: str) -> str:
    """主持人畫面「檢視記錄」：和 write_docx 同樣的簡單 Markdown（標題、條列、編號、段落、**粗體**），內容全部跳脫。"""
    parts: list[str] = []
    open_list = ""

    def close():
        nonlocal open_list
        if open_list:
            parts.append(f"</{open_list}>")
            open_list = ""

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            close()
            n = len(m.group(1))
            parts.append(f"<h{n}>{_inline_html(m.group(2).strip())}</h{n}>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line) or re.match(r"^\s*\d+[.)、]\s+(.*)$", line)
        if m:
            kind = "ul" if re.match(r"^\s*[-*]\s", line) else "ol"
            if open_list != kind:
                close()
                parts.append(f"<{kind}>")
                open_list = kind
            parts.append(f"<li>{_inline_html(m.group(1))}</li>")
            continue
        close()
        parts.append(f"<p>{_inline_html(line)}</p>")
    close()
    return "".join(parts)


def generate_minutes(config: Config, llm, mid: str, speaker_names: dict[int, str], items: list[dict],
                     meeting_type: str, now: datetime, private_dir: Path | None = None,
                     questions: list[dict] | None = None) -> dict:
    private = Path(private_dir) if private_dir is not None else ms.private_dir()
    names = {int(k): v for k, v in speaker_names.items()}
    doc = _load(config, mid)
    rows = add_questions(transcript_rows(doc, names), questions or [])
    original = render_rows(rows)
    people = attendees(doc, names)
    # 只換每句的內容；講者名字另外加回，不會被當成病人
    body, mapping = pseudonymize("\n".join(r[2] for r in rows), [i for i in items if _not_speaker(i, people)])
    sent = render_rows([[t, who, text] for (t, who, _), text in zip(rows, body.split("\n"))])
    hits = residual_phi(sent, mapping)
    if hits:
        eventlog.log("minutes", action="blocked", id=mid, hits=len(hits))
        return {"status": "blocked", "hits": [{"text": h, "context": _context(sent, h)} for h in hits]}

    prompt = (f"會議：晨會（{meeting_type}），日期 {_meeting_date(mid)}。\n"
              f"發言者（依發言長度）：{'、'.join(people)}\n"
              f"請依規則整理成五節：{'、'.join(SECTIONS)}。\n\n逐字稿：\n{sent}\n")
    private.mkdir(parents=True, exist_ok=True)
    (private / f"{mid}.map.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    (private / f"{mid}.sent.txt").write_text(
        f"# 送出時間 {now.isoformat(timespec='seconds')}\n# system\n{SYSTEM}\n# prompt\n{prompt}", encoding="utf-8")
    model = config.minutes_model or config.model_answer
    eventlog.log("minutes", action="sent", id=mid, chars=len(sent))
    data = llm.generate_json(model, SYSTEM, prompt, SCHEMA,
                             timeout=max(float(config.ai_timeout_seconds), MIN_TIMEOUT))

    ignore = code_like(original)
    body, unresolved = reidentify(str(data.get("minutes_md") or ""), mapping, ignore=ignore)
    corrections = []
    for c in data.get("corrections") or []:
        frm, _ = reidentify(str(c.get("from", "")), mapping, ignore=ignore)
        to, _ = reidentify(str(c.get("to", "")), mapping, ignore=ignore)
        if frm.strip() and frm.strip() != to.strip():
            corrections.append({"from": frm.strip(), "to": to.strip()})
    md = compose_md(mid, meeting_type, people, body, corrections)
    p = ms.paths(config, mid)
    p.minutes_md.parent.mkdir(parents=True, exist_ok=True)
    write_docx(md, p.minutes_docx)
    p.minutes_md.write_text(md, encoding="utf-8")
    ms.mark_minutes_generated(config, mid, now)
    eventlog.log("minutes", action="saved", id=mid, unresolved=len(unresolved))
    return {"status": "ok", "docx": str(p.minutes_docx), "md": str(p.minutes_md), "unresolved": unresolved}

"""逐字稿的純邏輯（轉寫工作站與筆電退路共用；不碰模型，可單元測試）。

輸入：語音辨識的句子 [{start, end, text}]、分講者的片段 [{start, end, speaker}]、每群的聲紋（平均向量）。
輸出：逐字稿 json 的內容（每句的講者、每位講者的發言總長與代表句、去識別候選）與給人看的 txt。
"""

from __future__ import annotations

import math
import re

from .phi import find_phi_kinds, ward_words

FORMAT_VERSION = 1
_CJK = re.compile(r"[一-鿿]")


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_speakers(asr: list[dict], diar: list[dict]) -> list[dict]:
    """每句取重疊時間最多的講者；完全沒有重疊的 speaker=None。"""
    out = []
    for seg in asr:
        best, best_t = None, 0.0
        totals: dict[int, float] = {}
        for d in diar:
            t = _overlap(seg["start"], seg["end"], d["start"], d["end"])
            if t > 0:
                totals[d["speaker"]] = totals.get(d["speaker"], 0.0) + t
        for spk, t in totals.items():
            if t > best_t:
                best, best_t = spk, t
        out.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": seg["text"], "speaker": best})
    return out


def _cos(a, b) -> float:
    if a is None or b is None:
        return -2.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else -2.0


def _mean(vectors: list[tuple[list[float], float]]):
    """依發言長度加權的平均聲紋。"""
    vectors = [(v, w) for v, w in vectors if v is not None]
    if not vectors:
        return None
    total = sum(w for _, w in vectors) or 1.0
    dim = len(vectors[0][0])
    return [sum(v[i] * w for v, w in vectors) / total for i in range(dim)]


def merge_small_clusters(diar: list[dict], embeddings: dict[int, list[float]],
                         min_seconds: float = 30.0, max_groups: int = 8) -> dict[int, int]:
    """自由分群常分出幾十群：發言總長 < min_seconds 的小群依聲紋 cosine 併入最像的大群；
    還超過 max_groups 時，把最小的群併進最像的群，直到剩 max_groups。回傳 舊群 → 新群（依發言長度 0、1、2…）。"""
    dur: dict[int, float] = {}
    for d in diar:
        dur[d["speaker"]] = dur.get(d["speaker"], 0.0) + max(0.0, d["end"] - d["start"])
    if not dur:
        return {}
    emb = {k: (list(embeddings[k]) if k in embeddings and embeddings[k] is not None else None) for k in dur}
    big = [k for k in dur if dur[k] >= min_seconds] or [max(dur, key=dur.get)]
    # 群 → 成員
    groups: dict[int, list[int]] = {k: [k] for k in big}
    largest = max(big, key=dur.get)
    for k in dur:
        if k in groups:
            continue
        target = max(big, key=lambda g: (_cos(emb[k], emb[g]), dur[g])) if emb[k] is not None else largest
        groups[target].append(k)

    def g_dur(g):
        return sum(dur[m] for m in groups[g])

    def g_emb(g):
        return _mean([(emb[m], dur[m]) for m in groups[g]])

    while len(groups) > max(1, max_groups):
        smallest = min(groups, key=g_dur)
        rest = [g for g in groups if g != smallest]
        e = g_emb(smallest)
        target = max(rest, key=lambda g: (_cos(e, g_emb(g)), g_dur(g)))
        groups[target] += groups.pop(smallest)

    order = sorted(groups, key=lambda g: (-g_dur(g), g))
    return {m: new for new, g in enumerate(order) for m in groups[g]}


def speaker_summaries(lines: list[dict], samples: int = 3) -> list[dict]:
    """每位講者：發言總秒數與最長的幾句（給主持人認人）；依發言長度由長到短。"""
    by: dict[int, list[dict]] = {}
    for line in lines:
        if line.get("speaker") is not None:
            by.setdefault(line["speaker"], []).append(line)
    out = []
    for spk, ls in by.items():
        seconds = round(sum(l["end"] - l["start"] for l in ls), 1)
        texts: list[str] = []
        for l in sorted(ls, key=lambda l: -len(l["text"].strip())):
            t = l["text"].strip()
            if t and t not in texts:  # Whisper 會重複同一句
                texts.append(t)
        out.append({"speaker": spk, "seconds": seconds, "samples": texts[:samples]})
    out.sort(key=lambda x: (-x["seconds"], x["speaker"]))
    return out


def phi_candidates(lines: list[dict], names: list[str]) -> list[dict]:
    """去識別候選：規則（病歷號、身分證、電話、生日、床號）＋人名辨識找到的名字。
    {text, kind, count, lines}；同一字串只列一次；長的排前面（換代號時要先換長的）。"""
    found: dict[str, str] = {}
    # 同一人連續的句子接起來掃（Whisper 會把一支電話切成兩句；會議記錄送出前也是這樣接起來的）
    runs: list[tuple[str, list[int]]] = []
    for i, line in enumerate(lines):
        if runs and lines[i - 1].get("speaker") == line.get("speaker"):
            runs[-1] = (runs[-1][0] + line["text"].strip(), runs[-1][1] + [i])
        else:
            runs.append((line["text"].strip(), [i]))
    for text_run, _ in runs:
        for text, kind in find_phi_kinds(text_run):
            found.setdefault(text, kind)
    for name in names:
        name = (name or "").strip()
        # 人名辨識會把英文名詞（sponsor、CTC）也當人名：只收含中文字的（英文名字請主持人手動新增）
        # 病房名稱與它的錯聽（8B → 芭比）不是人名
        if len(name) >= 2 and _CJK.search(name) and name not in ward_words():
            found.setdefault(name, "person")
    out = []
    for text, kind in found.items():
        where = sorted({i for run, idx in runs if text in run for i in idx if _touches(lines, idx, i, run, text)})
        count = sum(run.count(text) for run, _ in runs)
        if count:
            out.append({"text": text, "kind": kind, "count": count, "lines": where[:50]})
    out.sort(key=lambda x: (-len(x["text"]), x["text"]))
    return out


def _touches(lines: list[dict], idx: list[int], i: int, run: str, text: str) -> bool:
    """第 i 句是否含有（或部分含有）run 裡某一次出現的 text。"""
    start = 0
    spans = {}
    for j in idx:
        t = lines[j]["text"].strip()
        spans[j] = (start, start + len(t))
        start += len(t)
    a, b = spans[i]
    pos = run.find(text)
    while pos >= 0:
        if pos < b and pos + len(text) > a:
            return True
        pos = run.find(text, pos + 1)
    return False


def build_transcript(mid: str, asr: list[dict], diar: list[dict], embeddings: dict[int, list[float]],
                     names: list[str], models: dict, seconds: dict) -> dict:
    mapping = merge_small_clusters(diar, embeddings)
    merged = [{**d, "speaker": mapping.get(d["speaker"], d["speaker"])} for d in diar]
    lines = assign_speakers(asr, merged)
    return {"version": FORMAT_VERSION, "id": mid, "lines": lines, "speakers": speaker_summaries(lines),
            "candidates": phi_candidates(lines, names), "models": dict(models), "timing": dict(seconds)}


MAX_SPEAKER = 99


def edit_transcript(doc: dict, edits: list[dict]) -> dict:
    """主持人改逐字稿（使用者 2026-11-23）：edits＝[{i, text?, speaker?}]。回傳新的 doc（原本的不動）；
    講者摘要與去識別候選依新內容重算（人名辨識找到的名字保留；新打進去的人名請在確認畫面手動新增）。"""
    lines = [dict(line) for line in doc.get("lines", [])]
    for e in edits:
        i = e.get("i")
        if not isinstance(i, int) or not 0 <= i < len(lines):
            raise ValueError(f"沒有第 {i} 句")
        if "text" in e:
            text = " ".join(str(e["text"] or "").split())  # 一句一行
            if not text:
                raise ValueError("文字不能是空的")
            lines[i]["text"] = text
        if "speaker" in e:
            spk = e["speaker"]
            if not isinstance(spk, int) or not 0 <= spk <= MAX_SPEAKER:
                raise ValueError("講者編號不對")
            lines[i]["speaker"] = spk
    names = [c["text"] for c in doc.get("candidates", []) if c.get("kind") == "person"]
    return {**doc, "lines": lines, "speakers": speaker_summaries(lines), "candidates": phi_candidates(lines, names)}


def clock(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def speaker_label(speaker) -> str:
    return f"講者{speaker + 1}" if speaker is not None else "講者?"


def transcript_text(doc: dict) -> str:
    return "\n".join(f"[{clock(l['start'])}] {speaker_label(l.get('speaker'))}：{l['text'].strip()}"
                     for l in doc["lines"]) + "\n"

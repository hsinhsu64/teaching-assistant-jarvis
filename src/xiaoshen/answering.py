"""兩條回答路線：參考資料／今天的簡報（逐頁），與 OpenEvidence。

模型給的每一點都要附原文與出處，由 grounding.match_quote 驗證；未通過的點丟棄。
字卡顯示的是來源裡實際的文字。模型寫的總結（summary_zh）未逐句驗證，只給主持人看。
全部丟棄就回「資料中沒有提到」／「找不到可靠來源」，不得用模型知識補答。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from . import persona
from .grounding import Match, match_quote, normalize_keep
from .llm import LLM, LLMError
from .oe_client import OEBlocked, OEError, OEResult, ask_openevidence
from .pubmed import PubMedError, PubMedRecord, search_pubmed

Source = tuple[str, list[str]]  # （標籤, 逐頁文字）

MAX_CARDS = 5
LITERATURE_ROUTES = ("openevidence", "pubmed")  # 字卡出處是文獻（可看摘要、開原文網頁）
NOT_IN_PAGES = "資料中沒有提到"
NOT_IN_OE = "找不到可靠來源"
NOT_IN_PUBMED = "PubMed 找不到可靠來源"
MODEL_KNOWLEDGE_LABEL = "Gemini 一般知識（未經文獻驗證）"
NOT_BY_MODEL = "我找不到可靠來源"

SYSTEM_PROMPT = (
    "只能根據提供的資料回答；每一點都必須附上逐字複製自資料的原文片段（quote）與其出處；"
    "原文片段保持資料原本的語言，不可翻譯、不可改寫任何數字或用字；"
    "原文片段必須是能直接支持該點的完整句子或片語（不可只引標題或單一名詞）；"
    "資料中沒有的內容不得回答，回 not_found；"
    "問題有歧義（例如未說明成人或兒童、劑量單位）時回 clarify 並用一句中文反問；"
    "字卡用繁體中文，藥名、劑量、疾病名維持英文；每點 ≤ 40 字；最多 5 點。"
)


# PubMed：每一篇相關文獻中與問題相關的內容都要整理成字卡，不限張數
PUBMED_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    "每點 ≤ 40 字；最多 5 點。",
    "每點 ≤ 80 字；每一篇相關文獻中與問題相關的內容都要整理成字卡，不限張數；同一篇的字卡排在一起。")
assert PUBMED_SYSTEM_PROMPT != SYSTEM_PROMPT

def interpret_system() -> str:
    """語音名詞校正的提示詞。"""
    return _SPOKEN_FIX_SYSTEM.replace("{ctx}", persona.context(persona.current()))


def general_knowledge_system() -> str:
    """資料庫查不到時，改用模型自身知識回答的提示詞。"""
    return _GENERAL_KNOWLEDGE_SYSTEM.replace("{ctx}", persona.context(persona.current()))


# 送 OpenEvidence 之前先看問題要不要反問（保留；OE 原文不再經助理改寫）
CLARIFY_PROMPT = (
    "判斷下面的臨床問題送去文獻搜尋前是否需要先反問。只有在問題有會改變答案的歧義時才反問，"
    "例如未說明成人或兒童、劑量單位、哪一種疾病；問題清楚就回 clarify=false。"
    "反問時 question 用一句繁體中文。不要回答問題本身。"
)
CLARIFY_SCHEMA = {"type": "object", "properties": {"clarify": {"type": "boolean"}, "question": {"type": "string"}},
                  "required": ["clarify", "question"]}


def clarify_question(question: str, llm: LLM, model: str) -> str:
    """需要反問就回一句中文反問，否則回空字串。模型失敗時當作不用反問（不卡住查詢）。"""
    try:
        data = llm.generate_json(model, CLARIFY_PROMPT, f"問題：{question}", CLARIFY_SCHEMA)
    except LLMError:
        return ""
    if data.get("clarify") is True and str(data.get("question") or "").strip():
        return str(data["question"]).strip()
    return ""


# 語音問題：英文專有名詞常被聽成發音相近的普通字（實測「Bartter 和 Gitelman」→「butter 和 ghee」）
SPOKEN_HINT = (
    "\n這個問題來自語音辨識，英文藥名、病名可能被聽成發音相近的普通字。若問題中的詞在資料中找不到，"
    "但資料中有發音相近的專有名詞，請以該名詞理解問題，並把理解後的問題寫在 interpreted_question；"
    "沒有把握就回 clarify 反問。"
)


# 允許用資料中的數值與規則推導（計算、套用判讀步驟），但推導標明，且所依據的每一項都要是已驗證字卡
DERIVE_HINT = (
    "\n可以根據資料中的數值與規則做計算或套用資料中的判讀步驟，寫在 derivations：每項 conclusion_zh 寫出結論與算式，"
    "uses 列出所依據的 cards 編號（從 0 起）；所依據的每個數值與規則都必須各自是一張附原文的 card。"
    "推導不得使用資料以外的知識或數值。"
    "推導前先確認數值的條件（例如靜脈血或動脈血、成人或兒童、單位）與規則的適用範圍一致；"
    "不一致或資料沒寫清楚時不要推導，改在 derivation_notes 用一句中文說明為什麼沒推導。"
)


def derived_label() -> str:
    return f"{persona.current().display_name}推導（未經主治確認）："


def note_label() -> str:
    return f"{persona.current().display_name}說明（未經主治確認）："


@dataclass
class Derivation:
    text: str
    uses: list[int]      # 依據的字卡（已驗證字卡清單中的位置）
    sources: list[str]   # 依據字卡的出處（不重複，依出現順序）


@dataclass
class Card:
    point_zh: str
    quote: str
    source: str
    refs: list[int] = field(default_factory=list)  # OpenEvidence 文獻編號（字卡用）
    doc: str = ""   # 出處是簡報／參考資料時：資料名稱（「今天的簡報」或參考資料檔名）——顯示那一頁用（§6.2a）
    page: int = 0   # 同上：第幾頁（從 1 起）


@dataclass
class Figure:
    name: str
    caption: str
    path: str    # 本機檔案（只給伺服器用，不送到畫面）
    source: str  # 例：「[12] Isaac 2025, Cochrane Database Syst Rev」


@dataclass
class Answer:
    id: str
    question: str
    route: Literal["openevidence", "pubmed", "model", "references", "deck"]
    status: Literal["answered", "not_found", "clarify", "error"]
    summary_zh: str = ""
    cards: list[Card] = field(default_factory=list)
    clarify_zh: str = ""
    error: str = ""         # 給投影幕看的一行
    error_detail: str = ""  # 技術細節，只給主持人
    fallback_from: str = ""  # 「今天的簡報」等：原本的資料沒有提到，這是改查 OpenEvidence 的結果
    fallback_note: str = ""  # 投影與語音照用的一句：「今天的簡報、OpenEvidence 都沒有結果，以下為 PubMed 結果」
    figures: list[Figure] = field(default_factory=list)
    interpreted: str = ""  # 語音問題經校正後的理解（與原問題不同才有），投影顯示「（助理名）理解為：…」
    tables: list[dict] = field(default_factory=list)  # OpenEvidence 原文表格 {header, rows, sources}
    references: dict[int, dict] = field(default_factory=dict)  # 字卡上引用到的 OpenEvidence 文獻
    derivations: list[Derivation] = field(default_factory=list)  # 助理依資料推導的結論（標明）
    derivation_notes: list[str] = field(default_factory=list)  # 條件不合而沒推導的說明
    # 回答當下的簡報（顯示「那一頁」用；簡報之後被換掉也能找回原本那份，§6.2a、審查 #2）
    deck_path: str = ""
    deck_hash: str = ""
    # OE 簡報（§6.2c）：OpenEvidence 結果不做字卡，完整照原文做成簡報
    oe: "OEResult | None" = None
    oe_deck: str = ""      # 簡報 PDF 路徑
    oe_pages: int = 0
    oe_play: str = ""      # oe_playing／oe_confirm（正在播簡報，先問）／oe_waiting_quiz（派題中，先等）

    def to_dict(self, with_quotes: bool = True, for_display: bool = False) -> dict:
        d = {
            "id": self.id, "question": self.question, "route": self.route, "status": self.status,
            "summary_zh": self.summary_zh, "clarify_zh": self.clarify_zh, "error": self.error,
            "error_detail": self.error_detail, "fallback_from": self.fallback_from,
            "fallback_note": self.fallback_note,
            "interpreted": self.interpreted, "tables": self.tables,
            "derivations": [{"text": d.text, "sources": d.sources, "uses": d.uses} for d in self.derivations],
            "derivation_notes": list(self.derivation_notes),
            "cards": [{"point_zh": c.point_zh, "source": c.source,
                       # 可以叫出的原文（§6.2a）：literature＝摘要／原文網頁；page＝那一頁；""＝沒有
                       "origin": "page" if c.doc else ("literature" if c.refs and self.route in LITERATURE_ROUTES
                                                       else ""),
                       **({"quote": display_quote(c.quote)} if with_quotes else {})}
                      for c in self.cards],
            "figures": [{"name": f.name, "caption": f.caption, "source": f.source,
                         "url": f"/ai/figure/{self.id}/{i}"} for i, f in enumerate(self.figures)],
            "oe_deck": {"pages": self.oe_pages, "play": self.oe_play} if self.oe_deck else None,
        }
        if for_display:
            del d["error_detail"]
            if self.status == "answered":
                del d["summary_zh"]  # 模型總結未逐句驗證，不上投影
        return d


def friendly_error(e: Exception) -> str:
    msg = str(e)
    if "GEMINI_API_KEY" in msg:
        return "找不到 AI 金鑰（GEMINI_API_KEY）"
    if "逾時" in msg or "DEADLINE" in msg:
        return "AI 回答逾時，請稍後再問"
    if re.search(r"\b(429|500|503|504)\b|UNAVAILABLE|RESOURCE_EXHAUSTED", msg):
        return "AI 服務忙碌，請稍後再問"
    return "AI 呼叫失敗，請稍後再問"


def display_quote(text: str) -> str:
    """顯示用的引文：OpenEvidence 表格列「| A | B | C |」→「A：B ／ C」，去掉 markdown 粗體符號。驗證用的原文不動。"""
    t = text.replace("**", "").replace("__", "").strip()
    if t.startswith("|") or t.count("|") >= 2:
        cells = [c.strip() for c in t.strip("|").split("|") if c.strip()]
        if len(cells) >= 2:
            return f"{cells[0]}：{' ／ '.join(cells[1:])}"
    return t


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _schema(card_props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["answered", "not_found", "clarify"]},
            "summary_zh": {"type": "string"},
            "clarify_zh": {"type": "string"},
            "cards": {"type": "array", "items": {
                "type": "object",
                "properties": {"point_zh": {"type": "string"}, "quote": {"type": "string"}, **card_props},
                "required": ["point_zh", "quote", *required],
            }},
        },
        "required": ["status", "summary_zh", "cards"],
    }


PAGES_SCHEMA = _schema({"source_label": {"type": "string"}, "page": {"type": "integer"}}, ["source_label", "page"])
_DERIVATIONS = {"type": "array", "items": {"type": "object", "properties": {
    "conclusion_zh": {"type": "string"}, "uses": {"type": "array", "items": {"type": "integer"}}},
    "required": ["conclusion_zh", "uses"]}}
PAGES_SCHEMA = {**PAGES_SCHEMA, "properties": {**PAGES_SCHEMA["properties"], "derivations": _DERIVATIONS,
                                                "derivation_notes": {"type": "array", "items": {"type": "string"}}}}
SPOKEN_PAGES_SCHEMA = {**PAGES_SCHEMA, "properties": {**PAGES_SCHEMA["properties"],
                                                      "interpreted_question": {"type": "string"}}}
OE_SCHEMA = _schema({"citation_index": {"type": "integer"}}, ["citation_index"])


def pages_block(sources: list[Source]) -> str:
    """每頁前面加「[標籤 第 N 頁]」標記，讓模型能指出頁碼。"""
    parts = []
    for label, pages in sources:
        for i, text in enumerate(pages, start=1):
            if not text.strip():
                continue  # 空白頁略過（「目前這一頁」只留一頁有字，頁碼仍是原本的）
            parts.append(f"[{label} 第 {i} 頁]\n{text.strip()}")
    return "\n\n".join(parts)


def check_sources(sources: list[Source], max_chars: int) -> str:
    """回傳錯誤訊息；空字串代表可以送出。超過上限直接拒絕，不截斷。"""
    total = sum(len(p) for _, pages in sources for p in pages)
    if total > max_chars:
        return f"參考資料太大（約 {total:,} 字），請指定單一份或較短的資料"
    if sum(len(p.strip()) for _, pages in sources for p in pages) < 5:
        return "資料沒有可讀的文字（可能是掃描檔），無法讀取"
    return ""


_LABEL_PAGE = re.compile(r"^(.*?)\s*第\s*(\d+)\s*頁$")


def locate(label: str, page, quote: str, by_label: dict[str, list[str]]) -> tuple[str, int, Match] | None:
    """驗證引文在指定來源頁；回傳（來源標籤, 頁碼, 比對結果）。
    模型常把標籤寫成「今天的簡報 第 39 頁」：去掉後綴；page 欄位缺就用後綴的頁碼，兩者矛盾就不採用。"""
    m = _LABEL_PAGE.match(label.strip())
    if label not in by_label and m and m.group(1) in by_label:
        label, suffix_page = m.group(1), int(m.group(2))
        if not isinstance(page, int) or isinstance(page, bool):
            page = suffix_page
        elif page != suffix_page:
            return None
    pages = by_label.get(label)
    if pages is None or not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= len(pages):
        return None
    found = match_quote(quote, pages[page - 1])
    return (label, page, found) if found else None


OCR_MARK = "[圖片辨識]"  # 與 slide_ocr.OCR_MARK 相同；投影片圖片抄錄文字接在這個標記之後


def source_name(label: str, page: int, page_text: str, m: Match) -> str:
    """出處文字；引文落在圖片抄錄部分時加註「（圖片辨識）」。"""
    mark = normalize_keep(page_text).find(OCR_MARK)
    suffix = "（圖片辨識）" if mark >= 0 and m.end > mark else ""  # 有任何一段落在抄錄文字裡就標
    return f"{label} 第 {page} 頁{suffix}"


def page_quote_match(label: str, page, quote: str, by_label: dict[str, list[str]]) -> Match | None:
    hit = locate(label, page, quote, by_label)
    return hit[2] if hit else None


def _finish(answer: Answer, data: dict, cards: list[Card], empty_msg: str, limit: int | None = MAX_CARDS) -> Answer:
    status = data.get("status")
    if status == "clarify":
        answer.status = "clarify"
        answer.clarify_zh = str(data.get("clarify_zh") or "")
        return answer
    if status == "answered" and cards:
        answer.status = "answered"
        answer.summary_zh = str(data.get("summary_zh") or "")
        answer.cards = cards[:limit] if limit else list(cards)
        return answer
    answer.status = "not_found"
    answer.summary_zh = empty_msg
    return answer


def answer_from_pages(question: str, sources: list[Source], llm: LLM, model: str, max_chars: int,
                      route: Literal["references", "deck"] = "references", spoken: bool = False,
                      focus_page: int | None = None) -> Answer:
    answer = Answer(_new_id(), question, route, "error")
    problem = check_sources(sources, max_chars)
    if problem:
        answer.error = problem
        return answer
    focus = ""
    if focus_page and sources:
        focus = (f"問題問的是「{sources[0][0]} 第 {focus_page} 頁」（目前投影中的這一頁），"
                 "請以該頁為主，必要時引用其他頁的內容。\n")
    prompt = f"{focus}問題：{question}\n\n資料：\n{pages_block(sources)}"
    try:
        data = llm.generate_json(model, SYSTEM_PROMPT + DERIVE_HINT + (SPOKEN_HINT if spoken else ""), prompt,
                                 SPOKEN_PAGES_SCHEMA if spoken else PAGES_SCHEMA)
    except LLMError as e:
        answer.error = friendly_error(e)
        answer.error_detail = str(e)
        return answer
    interpreted = str(data.get("interpreted_question") or "").strip() if spoken else ""
    if interpreted and interpreted != question.strip():
        answer.interpreted = interpreted
    by_label = dict(sources)
    cards = []
    kept: dict[int, int] = {}  # 模型給的字卡編號 → 驗證通過後的位置
    for raw_i, c in enumerate(data.get("cards") or []):
        if not isinstance(c, dict):
            continue
        label, page, quote = str(c.get("source_label") or ""), c.get("page"), str(c.get("quote") or "")
        if hit := locate(label, page, quote, by_label):
            src_label, src_page, m = hit
            kept[raw_i] = len(cards)
            cards.append(Card(str(c.get("point_zh") or ""), m.text,
                              source_name(src_label, src_page, by_label[src_label][src_page - 1], m),
                              doc=src_label, page=src_page))
    answer = _finish(answer, data, cards, NOT_IN_PAGES, limit=None)
    if answer.status == "answered":
        derivations = _derivations(data.get("derivations"), kept, answer.cards)
        # 顯示前 MAX_CARDS 張，另外保留推導用到的卡（推導的每個依據都要看得到）；索引重新對應
        show = sorted(set(range(min(MAX_CARDS, len(answer.cards)))) | {u for d in derivations for u in d.uses})
        remap = {old: new for new, old in enumerate(show)}
        answer.cards = [answer.cards[i] for i in show]
        for d in derivations:
            d.uses = [remap[u] for u in d.uses]
        answer.derivations = derivations
        answer.derivation_notes = [str(n).strip() for n in data.get("derivation_notes") or [] if str(n).strip()][:3]
    return answer


def _derivations(raw, kept: dict[int, int], cards: list[Card]) -> list[Derivation]:
    """推導只保留「所依據的每一張字卡都通過驗證」的；沒有依據的推導不採用。"""
    out = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        uses = d.get("uses")
        text = str(d.get("conclusion_zh") or "").strip()
        if not text or not isinstance(uses, list) or not uses:
            continue
        if not all(isinstance(u, int) and not isinstance(u, bool) and u in kept for u in uses):
            continue
        mapped = [kept[u] for u in uses]
        sources = list(dict.fromkeys(cards[i].source for i in mapped))
        out.append(Derivation(text, mapped, sources))
    return out


_MARKER = re.compile(r"\[(\d+(?:\s*[,\-]\s*\d+)*)\]")


def sentence_citations(answer_text: str, m: Match) -> set[int]:
    """引文所在句子裡的文獻編號（[1]、[1,2]、[1-3]）。卡片標的編號必須在其中。"""
    src = normalize_keep(answer_text)
    t_open, t_close = src.rfind("<mdtable>", 0, m.start), src.find("</mdtable>", m.end)
    if t_open >= 0 and t_close >= 0 and src.find("</mdtable>", t_open) == t_close:
        # 引文在表格裡：只看同一列（表格裡沒有句號，不能用句子切）
        start = max(src.rfind("| |", 0, m.start), t_open) + 1
        row_end = src.find("| |", max(m.start, m.end - 1))  # 引文可能以該列的結尾「|」收尾
        end = row_end + 1 if 0 <= row_end < t_close else t_close
    else:
        start = max(src.rfind(". ", 0, m.start), src.rfind("。", 0, m.start)) + 1
        ends = [i for i in (src.find(". ", m.end), src.find("。", m.end)) if i >= 0]
        end = min(ends) + 1 if ends else len(src)
    found: set[int] = set()
    for group in _MARKER.findall(src[start:end]):
        for part in re.split(r"\s*,\s*", group):
            if "-" in part:
                a, b = (int(x) for x in part.split("-")[:2])
                found.update(range(a, b + 1))
            else:
                found.add(int(part))
    return found


INTERPRET_SYSTEM = (
    "你只負責校正語音辨識錯誤：使用者的問題來自語音辨識，英文藥名、病名可能被聽成發音相近的普通字。"
    "參考今天簡報的專有名詞，把聽錯的名詞改回正確名詞；其他字一律不改、不補充、不回答問題。沒有聽錯就原樣回傳。"
)
INTERPRET_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}


_SPOKEN_FIX_SYSTEM = (
    "你只負責校正語音辨識錯誤：使用者在{ctx}用語音發問，英文藥名、病名、檢驗名常被聽成發音相近的字"
    "（例如 Giotomine syndrome 其實是 Gitelman syndrome，butter 其實是 Bartter）。"
    "用你的醫學知識，並參考提供的專有名詞（可能沒有），把聽錯的名詞改回最可能的正確名詞；"
    "其他字一律不改、不補充、不回答問題。沒有聽錯就原樣回傳。"
    "如果改了名詞但沒有把握（有兩個以上可能，或發音差很多），unsure 填 true，"
    "並在 clarify_zh 用一句中文反問，例如「你是說 Gitelman syndrome 嗎？」。"
)
SPOKEN_FIX_SCHEMA = {"type": "object", "properties": {
    "question": {"type": "string"}, "unsure": {"type": "boolean"}, "clarify_zh": {"type": "string"}},
    "required": ["question", "unsure", "clarify_zh"]}


@dataclass
class Interpretation:
    question: str
    clarify_zh: str = ""  # 有值：沒把握，先反問、不查


def interpret_spoken(question: str, terms: list[str], llm: LLM, model: str) -> Interpretation:
    """語音問題查詢前先校正聽錯的醫學名詞：不限今天簡報的詞彙。失敗就用原問題。"""
    vocab = f"專有名詞：{'、'.join(terms)}\n\n" if terms else ""
    try:
        data = llm.generate_json(model, interpret_system(), f"{vocab}問題：{question}", SPOKEN_FIX_SCHEMA, timeout=20)
    except LLMError:
        return Interpretation(question)
    fixed = str(data.get("question") or "").strip() or question
    clarify = str(data.get("clarify_zh") or "").strip()
    if data.get("unsure") and clarify and fixed.strip() != question.strip():
        return Interpretation(fixed, clarify)
    return Interpretation(fixed)


def interpret_question(question: str, terms: list[str], llm: LLM, model: str) -> str:
    """語音問題送 OpenEvidence 前，用今天簡報的詞彙校正聽錯的名詞。失敗就用原問題。"""
    if not terms:
        return question
    try:
        data = llm.generate_json(model, INTERPRET_SYSTEM,
                                 f"專有名詞：{'、'.join(terms)}\n\n問題：{question}", INTERPRET_SCHEMA, timeout=20)
    except LLMError:
        return question
    fixed = str(data.get("question") or "").strip()
    return fixed or question


def _table_with_sources(table: dict, by_index: dict[int, dict]) -> dict:
    """表格逐字照登；表中出現的文獻編號在表下列出出處。"""
    marks: list[int] = []
    for row in table["rows"]:
        for cell in row:
            for group in _MARKER.findall(cell):
                for n in re.split(r"\s*,\s*", group):
                    if n.isdigit() and int(n) not in marks:
                        marks.append(int(n))
    return {"header": table["header"], "rows": table["rows"][:15],
            "sources": [citation_label(by_index[n]) for n in sorted(marks) if n in by_index]}


def citation_label(c: dict) -> str:
    who = c.get("first_author") or c.get("title") or "OpenEvidence"
    tail = ", ".join(x for x in (c.get("journal"),) if x)
    head = f"[{c['index']}] {who} {c.get('year') or ''}".rstrip()
    label = f"{head}, {tail}" if tail else head
    return f"{label}. doi:{c['doi']}" if c.get("doi") else label  # 來源要能查到原文


PUBMED_QUERY_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}, "broader": {"type": "string"}},
                       "required": ["query", "broader"]}
PUBMED_QUERY_PROMPT = (
    "把下面的臨床問題轉成 PubMed 搜尋字串：英文關鍵字或 MeSH 詞，可用 AND／OR，不加年份或文章類型限制。"
    "query 是精準版；broader 是放寬版（精準版查不到時用）。只輸出搜尋字串，不要回答問題。"
)


def answer_from_pubmed(question: str, llm: LLM, model: str, query_model: str,
                       search: Callable[[str], list[PubMedRecord]] = search_pubmed) -> Answer:
    """查 PubMed 摘要回答；字卡原文要逐字對得上該篇摘要，出處標期刊 citation。"""
    answer = Answer(_new_id(), question, "pubmed", "error")
    try:
        q = llm.generate_json(query_model, PUBMED_QUERY_PROMPT, f"問題：{question}", PUBMED_QUERY_SCHEMA)
        query, broader = str(q.get("query") or question).strip(), str(q.get("broader") or "").strip()
        recs = search(query)
        if not recs and broader and broader != query:
            recs = search(broader)
    except PubMedError as e:
        answer.error = str(e)
        return answer
    except LLMError as e:
        answer.error = friendly_error(e)
        answer.error_detail = str(e)
        return answer
    if not recs:
        answer.status = "not_found"
        answer.summary_zh = NOT_IN_PUBMED
        return answer
    by_index = {i: r for i, r in enumerate(recs, 1)}
    articles = "\n\n".join(f"[{i}] {r.citation()}\nTitle: {r.title}\nAbstract: {r.abstract}"
                            for i, r in by_index.items())
    prompt = (f"問題：{question}\n\nPubMed 文獻摘要（英文原文；citation_index 填文章編號，"
              f"quote 必須逐字複製自該篇的 Title 或 Abstract）：\n{articles}")
    try:
        data = llm.generate_json(model, PUBMED_SYSTEM_PROMPT, prompt, OE_SCHEMA)
    except LLMError as e:
        answer.error = friendly_error(e)
        answer.error_detail = str(e)
        return answer
    cards = []
    for c in data.get("cards") or []:
        if not isinstance(c, dict):
            continue
        idx, quote = c.get("citation_index"), str(c.get("quote") or "")
        rec = by_index.get(idx)
        if rec and (m := match_quote(quote, f"{rec.title}\n{rec.abstract}")):
            cards.append(Card(str(c.get("point_zh") or ""), m.text, rec.citation(), refs=[idx]))
    cards.sort(key=lambda c: c.refs[0])  # 同一篇的字卡排在一起（依搜尋順序）
    answer = _finish(answer, data, cards, NOT_IN_PUBMED, limit=None)
    if answer.status == "answered":
        answer.references = {i: by_index[i].reference(i) for i in sorted({r for c in answer.cards for r in c.refs})}
    return answer


def answer_from_openevidence(question: str, script: Path, llm: LLM, model: str,
                             ask: Callable[[str, Path], OEResult] = ask_openevidence) -> Answer:
    """OpenEvidence 的結果完整照原文做成 OE 簡報（§6.2c），不再由助理濃縮成字卡。
    有非空白的回答就算有結果；空白回 not_found，無法使用回 error。"""
    answer = Answer(_new_id(), question, "openevidence", "error")
    try:
        oe = ask(question, script)
    except (OEBlocked, OEError) as e:
        answer.error = str(e)
        return answer
    if not (oe.raw or oe.answer or "").strip():
        answer.status = "not_found"
        answer.summary_zh = NOT_IN_OE
        return answer
    answer.status = "answered"
    answer.oe = oe
    answer.references = {c["index"]: c for c in oe.citations if c.get("index") is not None}
    return answer


# OpenEvidence、PubMed 都沒有結果時，改由 Gemini Pro 用自身知識回答。
# 沒有原文可以驗證，所以標示「未經文獻驗證」，且不准列出或編造文獻（模型最容易在這裡捏造出處）。
_GENERAL_KNOWLEDGE_SYSTEM = (
    "你是{ctx}的教學助理。資料庫查不到相關文獻，請用你的醫學知識簡短回答；"
    "不得列出、引用或編造任何文獻、指引名稱、作者、年份、期刊或網址；"
    "沒有把握或可能過時的內容不要回答，回 not_found；"
    "問題有歧義（例如未說明成人或兒童、劑量單位）時回 clarify 並用一句中文反問；"
    "字卡用繁體中文，藥名、劑量、疾病名維持英文；每點 ≤ 40 字；最多 5 點；劑量要寫清楚適用對象。"
)
MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["answered", "not_found", "clarify"]},
        "summary_zh": {"type": "string"},
        "clarify_zh": {"type": "string"},
        "cards": {"type": "array", "items": {"type": "object", "properties": {"point_zh": {"type": "string"}},
                                             "required": ["point_zh"]}},
    },
    "required": ["status", "summary_zh", "clarify_zh", "cards"],
}


def answer_from_model_knowledge(question: str, llm: LLM, model: str) -> Answer:
    """最後一層：模型自身知識。每張字卡的出處都是「Gemini 一般知識（未經文獻驗證）」，沒有原文片段。"""
    answer = Answer(_new_id(), question, "model", "error")
    try:
        data = llm.generate_json(model, general_knowledge_system(), f"問題：{question}", MODEL_SCHEMA)
    except LLMError as e:
        answer.error = friendly_error(e)
        answer.error_detail = str(e)
        return answer
    cards = [Card(p, "", MODEL_KNOWLEDGE_LABEL)
             for c in data.get("cards") or [] if isinstance(c, dict) and (p := str(c.get("point_zh") or "").strip())]
    return _finish(answer, data, cards, NOT_BY_MODEL)

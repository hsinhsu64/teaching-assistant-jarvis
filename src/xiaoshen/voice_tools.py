"""語音模型可呼叫的工具。回答與出題一律走 Phase 2 的 JobRunner（出處驗證後的字卡）。

語音模型只拿到驗證過的字卡重點（point；出處只上投影，不唸），拿不到未驗證的模型總結。
控制指令（上一頁、派題、收卷…）只回「請用主持人畫面操作」，不執行（spec §5.3）。
"""

from __future__ import annotations

import re
import time

from pathlib import Path

from . import eventlog
from . import persona
from .answering import derived_label, note_label
from .jobs import JobRunner
from .library import Library, rank_names
from .live_backend import ToolCall
from .phi import find_phi
from .state import VOICE_CONFIRM_TTL as CONFIRM_TTL
from .state import AppState

_SYSTEM_INSTRUCTION = (
    "你是{ctx}的助理{name}，只能用繁體中文（台灣用語）說話，絕對不要說日文、韓文或其他語言；"
    "藥名、劑量、疾病名維持英文，語氣簡短。\n"
    "一、你不得用自己的知識回答任何醫學或臨床內容。任何臨床問題一律呼叫 ask 工具；呼叫前先說一句「我查一下」。"
    "要求整理資料內容（例如列出重點、摘要、列出可以討論的討論主題、比較、解釋這頁）也一律呼叫 ask"
    "（說「這份簡報」「簡報內容」時 source 填 deck，「這頁」填 page），question 照使用者的要求寫；"
    "不得自己回答無法提供或聽不懂，除非真的聽不清楚。\n"
    "二、ask 回傳後只照念 message（一句話：找到幾張字卡、已投影，有警示也寫在 message 裡），"
    "預設不要念字卡內容、不要念出處（使用者決定：所有回答預設不念）；不得增加、推論或補充。"
    "使用者要求「念出來」「念第 N 張」時呼叫 read_answer（card 填 N，全部填 0）並照念 message；"
    "OE 簡報播放中說「念出來」也呼叫 read_answer；說「打開第 N 篇文獻」呼叫 open_citation（number 填 N）並照念 message。"
    "ask 回傳 oe_confirm 時照念 message 並等對方回答，對方說要才呼叫 play_deck，name 填「OE 簡報」。"
    "ask 回傳 confirm_query 時照念 message（念出要查的問題）並等對方回答：對方說「對」「沒錯」「查吧」才再呼叫 ask，"
    "question 照剛剛那句、帶 confirmed=true；對方說出正確的詞就用改好的問題再呼叫 ask（不帶 confirmed）；"
    "對方說不要、取消就不查。不得自己替對方確認。"
    "回傳 not_found、clarify、choose、blocked、expired 或 error 時，照念 message。"
    "使用者問「這張」「這頁」「這張投影片」的內容時，ask 的 source 填 page。\n"
    "三、使用者要求出題時呼叫 make_quiz，並說明草稿已送到主持人畫面，需主持人審閱後才會發出。\n"
    "四、使用者要求打開或使用某份資料時呼叫 open_reference。\n"
    "五、翻頁：下一頁呼叫 next_page，上一頁呼叫 prev_page，「播放第 X 頁」「跳到第 X 頁」呼叫 goto_page（page 填 X）；"
    "前後都不要說話（翻頁成功不出聲），失敗時照念 message。注意「播放第 X 頁」是跳頁，「播放 xxx 簡報」才是 play_deck。"
    "派題、發出考卷、公布答案、收卷、收起抽屜、存圖等控制指令"
    "一律呼叫 host_only_command，然後說「請用主持人畫面操作」。\n"
    "六、只叫你的名字或喚醒詞「{wake}」、還沒說要求時（逐字稿可能只有這個詞，或被聽成發音相近的字），不要回應，安靜等對方說完要求。"
    "非醫學的寒暄可以簡短回應。不知道或聽不清楚就請對方再說一次。\n"
    "七、使用者要求播放某份簡報時呼叫 play_deck（說「今天的簡報」時 name 填「今天的簡報」）；要求結束簡報、回到主畫面時呼叫 end_deck。"
    "play_deck 回傳 confirm 時照念 message 並等對方回答，對方說要才再呼叫一次並帶 confirm=true；回傳 choose 時照念 message，"
    "對方說出是哪一份後再呼叫一次。其他狀態照念 message。\n"
    "八、使用者要求「顯示第 N 張的原文」「看第 N 張的出處」時呼叫 show_source（card 填 N）；"
    "要求「打開第 N 張的原文網頁」時呼叫 show_source 並帶 kind=web；要求「關掉原文」「關閉全文」「收起原文」時呼叫 close_source；"
    "投影上正在顯示原文時，「收起字卡」「關閉字卡」也是指關掉原文，呼叫 close_source。"
    "只說「顯示原文」沒說第幾張就填 1。照念 message。\n"
    "九、唯一例外：投影上已打開原文時，要求「整理這篇／這份原文的重點」「把原文做摘要」時，先說「我讀一下」再呼叫 summarize_source（不是 ask）；"
    "完成時只念 message，不要念簡報內容。要求「播放摘要簡報」「再看一次摘要」時呼叫 play_deck，name 填「摘要簡報」；"
    "「播放 OE 簡報」時 name 填「OE 簡報」。"
    "摘要簡報播放中要求「關閉摘要」「關閉原文」「關掉摘要簡報」時呼叫 close_source。"
)

_SOURCE = {"type": "string", "enum": ["openevidence", "pubmed", "references", "deck", "page"],
           "description": "openevidence=搜尋文獻（未指定資料時，查不到會自動再查 PubMed）；pubmed=使用者指定查 PubMed；"
                          "references=參考資料；deck=今天的簡報；page=目前這一頁"}

def system_instruction() -> str:
    p = persona.current()
    return (_SYSTEM_INSTRUCTION.replace("{ctx}", persona.context(p)).replace("{name}", p.display_name)
            .replace("{wake}", persona.wake_word()))


TOOLS: list[dict] = [
    {"name": "ask", "description": "查詢臨床問題，或整理資料內容（列重點、摘要、討論主題、比較），回傳經出處驗證的字卡",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string"}, "source": _SOURCE,
         "reference_name": {"type": "string", "description": "使用者指名的參考資料名稱（可省略）"},
         "confirmed": {"type": "boolean",
                       "description": "上一次回傳 confirm_query 後，使用者已說「對」確認要查這句"}},
         "required": ["question"]}},
    {"name": "make_quiz", "description": "依來源出 n 題選擇題草稿，送到主持人畫面審閱",
     "parameters": {"type": "object", "properties": {
         "n": {"type": "integer"}, "source": _SOURCE, "reference_name": {"type": "string"},
         "focus": {"type": "string", "description": "出題重點（可省略）"}}, "required": ["n"]}},
    {"name": "open_reference", "description": "選定一份參考資料，之後的問題以它為準",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "next_page", "description": "簡報翻到下一頁", "parameters": {"type": "object", "properties": {}}},
    {"name": "prev_page", "description": "簡報翻回上一頁", "parameters": {"type": "object", "properties": {}}},
    {"name": "goto_page", "description": "簡報跳到第 page 頁（從 1 開始數，使用者說的頁碼）",
     "parameters": {"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"]}},
    {"name": "host_only_command", "description": "使用者要求只有主持人能做的控制指令（派題、公布答案、收卷、收起抽屜等）",
     "parameters": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}},
    {"name": "play_deck", "description": "播放指定的簡報（投影幕切到簡報）",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"},
         "confirm": {"type": "boolean", "description": "使用者已確認要停止目前的簡報、改播這份"}},
         "required": ["name"]}},
    {"name": "end_deck", "description": "結束簡報，投影幕回到主畫面", "parameters": {"type": "object", "properties": {}}},
    {"name": "show_source", "description": "在投影幕顯示第 card 張字卡出處的原文（文獻摘要或簡報／參考資料那一頁）；kind=web 打開原文網頁",
     "parameters": {"type": "object", "properties": {
         "card": {"type": "integer", "description": "投影上字卡的編號，從 1 起"},
         "kind": {"type": "string", "enum": ["auto", "web"]}}, "required": ["card"]}},
    {"name": "close_source", "description": "關掉投影幕上的出處原文", "parameters": {"type": "object", "properties": {}}},
    {"name": "summarize_source", "description": "把投影上目前打開的原文整份整理成摘要簡報並播放",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "open_citation", "description": "OE 簡報的參考文獻：在投影幕打開第 number 篇文獻的網頁",
     "parameters": {"type": "object", "properties": {"number": {"type": "integer", "description": "文獻編號 [N]"}},
                    "required": ["number"]}},
    {"name": "read_answer", "description": "使用者要求才念：念出字卡內容（card=0 全部、N 第 N 張）；OE 簡報播放中念目前這一頁",
     "parameters": {"type": "object", "properties": {"card": {"type": "integer", "description": "第幾張，0 為全部"}}}},
]

TODAY_DECK = ("今天的簡報", "今天", "這份")
SUMMARY_DECK = ("摘要簡報", "摘要")
OE_DECK = ("OE 簡報", "OE簡報", "OE", "OpenEvidence 簡報")
OE_PLAY_MESSAGE = {
    "oe_confirm": "OpenEvidence 的結果好了，要播放嗎？",
    "oe_waiting_quiz": "OpenEvidence 的結果已備好，收題後說「播放 OE 簡報」",
}
def _footer_line_re() -> "re.Pattern[str]":
    name = re.escape(persona.current().display_name)
    return re.compile(rf"^\s*(\d+\s*/\s*\d+|{name} · .*)\s*$")

SEARCH_ROUTES = ("openevidence", "pubmed")  # 送外部搜尋的來源：英文名詞要先確認（§5.4）
_ENGLISH = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_NOT_TERMS = {"openevidence", "pubmed", "oe", "ai", "pmid", "qr", "ok", "gemini"}
EXPIRED = {"status": "expired", "message": "剛剛要確認的查詢已超過 3 分鐘，請再說一次問題"}


def english_terms(text: str) -> set[str]:
    """問題裡的英文字（小寫），不含資料庫名等非醫學用語。"""
    return {w.lower() for w in _ENGLISH.findall(text or "") if len(w) >= 2 and w.lower() not in _NOT_TERMS}


def needs_confirm(question: str) -> bool:
    return bool(english_terms(question))


BLOCKED = {"status": "blocked", "message": "剛剛那句可能含病人識別資訊，沒有送出查詢。"}
REFUSED = {"status": "refused", "message": "請用主持人畫面操作"}
ALREADY_DONE = {"status": "already_done", "message": ""}  # 沒有新的話卻又要翻頁：不翻、不出聲
PAGE_LOG_ARGS = {"goto_page", "play_deck"}  # 只有這些工具的參數記進記錄（其他可能含問題內容）
DRAFTS_BUSY = {"status": "refused", "message": "主持人畫面已有題目草稿或正在出題，請主持人先處理"}


class VoiceTools:
    def __init__(self, state: AppState, jobs: JobRunner, library: Library, viewer=None):
        self.state = state
        self.jobs = jobs
        self.library = library
        self.viewer = viewer  # 顯示出處原文（§6.2a）
        self.turn_text = ""  # 本輪使用者說的話（Live 逐字稿），用來做識別資訊檢查
        self._heard = 0               # 收到幾段使用者的話（不分輪）
        self._heard_at_move: int | None = None  # 上次翻頁時的 _heard；翻頁要有新的話（實機一直往下翻）
        self._heard_at_confirm: int | None = None  # 念出「要查的是…對嗎」時的 _heard；確認要有新的話

    def hear(self, text: str) -> None:
        self.turn_text += text
        self._heard += 1

    def _repeat_move(self) -> bool:
        return self._heard_at_move is not None and self._heard == self._heard_at_move

    def _moved(self) -> None:
        self._heard_at_move = self._heard

    def begin_turn(self) -> None:
        self.turn_text = ""

    def _alert(self) -> dict:
        self.state.voice_phi_alert = True
        self.state._changed()
        return dict(BLOCKED)

    def _phi_blocked(self, *texts: str) -> bool:
        """本輪逐字稿與工具參數（模型轉述的問題、資料名稱、出題重點）都要檢查。"""
        return any(find_phi(t) for t in (self.turn_text, *texts) if t)

    def _route(self, source: str, reference_name: str) -> str:
        """有指名或已選定參考資料時，除非明說簡報，一律走參考資料（模型常省略 source）。"""
        if source in ("deck", "page", "pubmed"):  # 明說的來源不被已選的參考資料蓋掉
            return source
        if reference_name or (self.state.voice_ref and self.state.voice_ref in self.library.docs):
            return "references"
        return source

    def _resolve_refs(self, source: str, name: str) -> tuple[list[str], dict | None]:
        """回傳（參考資料名稱清單, 需要直接回給語音模型的結果）。"""
        if source != "references":
            return [], None
        if name:
            found = self.library.find(name)
            if not found:
                return [], {"status": "not_found", "message": "找不到這份參考資料", "cards": []}
            exact = [d for d in found if d.name.lower() == name.strip().lower()]
            if len(found) > 1 and not exact:
                names = [d.name for d in found]
                self.state.ref_candidates = names
                self.state._changed()
                return [], {"status": "choose", "message": "找到幾份，請在主持人畫面選擇", "candidates": names}
            return [(exact or found)[0].name], None
        if self.state.voice_ref and self.state.voice_ref in self.library.docs:
            return [self.state.voice_ref], None
        return [], None

    def handle(self, call: ToolCall) -> dict:
        try:
            handler = getattr(self, f"_{call.name}", None)
            if handler is None or call.name.startswith("_"):
                result = {"status": "error", "message": f"{persona.current().display_name}暫時無法處理"}
            else:
                result = handler(**call.args)
        except Exception:
            result = {"status": "error", "message": f"{persona.current().display_name}暫時無法處理"}
        extra = {"args": call.args} if call.name in PAGE_LOG_ARGS else {}
        eventlog.log("tool", name=call.name, status=result.get("status"),
                     **({"page": result["page"]} if "page" in result else {}), **extra)
        return result

    def _ask(self, question: str, source: str = "openevidence", reference_name: str = "",
             confirmed: bool = False) -> dict:
        if self._phi_blocked(question, reference_name):
            return self._alert()
        source = self._route(source, reference_name)
        refs, early = self._resolve_refs(source, reference_name)
        if early:
            return early
        spoken, checked = True, False
        if source in SEARCH_ROUTES and needs_confirm(question):
            gate = self._confirm_gate(question, source, refs, confirmed)
            if isinstance(gate, dict):
                return gate
            question, spoken, checked = gate, False, True  # 確認過的文字照查：不再自動改字、不再反問
        job = self.jobs.ask(question, source, refs, spoken=spoken, confirmed=checked)
        if not job:
            return self._alert()
        ans = self.jobs.wait_answer(job)
        if ans is None:
            return {"status": "error", "message": "查詢逾時或被新的問題取代", "cards": []}
        if ans.status == "answered":
            # 所有回答預設不念內容，要求才念（§6.2c）；警示（fallback_note）照念
            note = ans.fallback_note
            if ans.oe_deck:
                status = ans.oe_play or "oe_playing"
                said = OE_PLAY_MESSAGE.get(status, f"OpenEvidence 的結果已投影，共 {ans.oe_pages} 頁")
                return {"status": status if status != "oe_playing" else "answered",
                        "message": f"{note}，{said}" if note else said}
            said = f"找到 {len(ans.cards)} 張字卡，已投影"
            return {"status": "answered", "message": f"{note}，{said}" if note else said}
        message = {"not_found": ans.summary_zh, "clarify": ans.clarify_zh}.get(ans.status, ans.error)
        return {"status": ans.status, "message": message, "cards": []}

    def _confirm_gate(self, question: str, source: str, refs: list[str], confirmed: bool) -> dict | str:
        """英文名詞的語音查詢：回傳要念的確認訊息（dict），或已確認、可以查的問題（str）。"""
        st = self.state
        pending = st.voice_confirm
        if pending is not None and st.active_voice_confirm() is None:
            st.clear_voice_confirm()
            self._heard_at_confirm = None
            if confirmed:
                return dict(EXPIRED)
            pending = None
        if pending is not None and confirmed and pending["route"] == source:
            same = english_terms(question) in (english_terms(pending["question"]), english_terms(pending["original"]))
            if same:
                if self._heard_at_confirm is not None and self._heard > self._heard_at_confirm:
                    st.clear_voice_confirm()
                    self._heard_at_confirm = None
                    return pending["question"]
                return self._confirm_message(pending["question"])  # 沒有新的話：模型自己說了「對」
        proposed = self.jobs.propose_spoken(question).question or question
        if self._phi_blocked(proposed):
            return self._alert()
        st.set_voice_confirm(proposed, question, source, refs)
        self._heard_at_confirm = self._heard
        return self._confirm_message(proposed)

    @staticmethod
    def _confirm_message(question: str) -> dict:
        return {"status": "confirm_query", "message": f"要查的是：{question}，對嗎？"}

    def _make_quiz(self, n: int, source: str = "deck", reference_name: str = "", focus: str = "") -> dict:
        if self._phi_blocked(focus, reference_name):
            return self._alert()
        if self.state.drafts or self.state.quiz_job:
            return dict(DRAFTS_BUSY)  # 不蓋掉主持人正在審的草稿
        source = self._route(source, reference_name)
        refs, early = self._resolve_refs(source, reference_name)
        if early:
            return early
        try:
            job = self.jobs.make_quiz(int(n), source, refs, focus)
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        if not job:
            return self._alert()
        result = self.jobs.wait_quiz(job)
        if result is None:
            return {"status": "error", "message": "出題逾時"}
        count, notes = result
        if count == 0:
            return {"status": "error", "message": "；".join(notes) or "沒有通過出處驗證的題目"}
        return {"status": "drafted", "count": count, "message": f"已產生 {count} 題草稿，請主持人審閱"}

    def _open_reference(self, name: str) -> dict:
        found = self.library.find(name)
        if not found:
            return {"status": "not_found", "message": "找不到這份參考資料"}
        exact = [d for d in found if d.name.lower() == name.strip().lower()]
        if len(found) > 1 and not exact:
            names = [d.name for d in found]
            self.state.ref_candidates = names
            self.state._changed()
            return {"status": "choose", "message": "找到幾份，請在主持人畫面選擇", "candidates": names}
        self.state.voice_ref = (exact or found)[0].name
        self.state.ref_candidates = []
        self.state._changed()
        return {"status": "ok", "message": f"已選擇 {self.state.voice_ref}"}

    def _play_deck(self, name: str, confirm: bool = False) -> dict:
        st = self.state
        if name.strip() in OE_DECK:  # 做好但還沒播的 OE 簡報，或再播一次最近的（§6.2c）
            if st.oe_pending is not None and self.jobs.play_pending_oe():
                return {"status": "ok", "message": "開始播放 OE 簡報"}
            last = st.last_temp.get("oe")
            if last is None:
                return {"status": "not_found", "message": "還沒有 OE 簡報"}
            if not st.play_summary(last, kind="oe"):
                return {"status": "error", "message": "OE 簡報開不起來"}
            return {"status": "ok", "message": "開始播放 OE 簡報"}
        if name.strip() in SUMMARY_DECK:  # 再播一次最近的摘要簡報（§6.2b）
            if st.last_summary is None:
                return {"status": "not_found", "message": "還沒有摘要簡報"}
            if not st.play_summary(st.last_summary):
                return {"status": "error", "message": "摘要簡報開不起來"}
            return {"status": "ok", "message": "開始播放摘要簡報"}
        if name.strip() in TODAY_DECK and st.answer_deck is not None:
            path = Path(st.answer_deck.path)
        else:
            by_name = {p.stem: p for p in st.deck_paths}
            names = rank_names(name, by_name)
            if not names:
                return {"status": "not_found", "message": "找不到這份簡報"}
            exact = [n for n in names if n.lower() == name.strip().lower()]
            if len(names) > 1 and not exact:
                st.deck_candidates = names
                st._changed()
                return {"status": "choose", "message": "找到幾份：" + "、".join(names) + "，是哪一份？",
                        "candidates": names}
            path = by_name[(exact or names)[0]]
        target = path.stem
        if st.presenting and st.deck is not None:
            current = st.deck.name
            if Path(st.deck.path) == path:
                return {"status": "ok", "message": f"已經在播放「{target}」"}
            if not (confirm and st.pending_switch == target):
                st.pending_switch = target  # 要口頭確認才換片（§6.0）
                return {"status": "confirm", "message": f"要停止「{current}」、改播「{target}」嗎？"}
        st.play(path)
        if not st.presenting or st.deck is None or st.deck.name != target:
            return {"status": "error", "message": f"「{target}」開不起來"}
        return {"status": "ok", "message": f"開始播放「{target}」"}

    def _end_deck(self) -> dict:
        if not self.state.presenting:
            return {"status": "ok", "message": "目前不在簡報中"}
        was_summary = self.state.summary_path is not None
        self.state.stop_presenting()
        if was_summary:
            return {"status": "ok", "message": "已回到原本的畫面"}
        return {"status": "ok", "message": "已回到主畫面"}

    def _next_page(self) -> dict:
        if not self.state.presenting:
            return {"status": "not_presenting", "message": "目前不在簡報中"}
        if self._repeat_move():
            return dict(ALREADY_DONE)
        self.state.next()
        self._moved()
        page = self.state.page + 1
        return {"status": "ok", "page": page, "message": f"已翻到第 {page} 頁"}

    def _prev_page(self) -> dict:
        if not self.state.presenting:
            return {"status": "not_presenting", "message": "目前不在簡報中"}
        if self._repeat_move():
            return dict(ALREADY_DONE)
        self.state.prev()
        self._moved()
        return {"status": "ok", "page": self.state.page + 1}

    def _goto_page(self, page: int) -> dict:
        st = self.state
        if not st.presenting or st.deck is None:
            return {"status": "not_presenting", "message": "目前不在簡報中"}
        n = st.deck.page_count
        page = int(page)
        if not 1 <= page <= n:
            return {"status": "out_of_range", "message": f"沒有第 {page} 頁，這份簡報共 {n} 頁"}
        if self._repeat_move():
            return dict(ALREADY_DONE)
        st.goto(page - 1)
        self._moved()
        return {"status": "ok", "page": page}

    def _show_source(self, card: int, kind: str = "auto") -> dict:
        if self.viewer is None:
            return {"status": "error", "message": "原文功能未啟用"}
        return self.viewer.show(int(card), "web" if kind == "web" else "auto")

    def _close_source(self) -> dict:
        st = self.state
        if not st.source_view and st.summary_path is not None:
            # 摘要簡報播放中說「關閉原文」「關閉摘要」：關掉摘要簡報、回到原本的畫面（2026-11-05 實機）
            st.stop_presenting()
            return {"status": "ok", "message": "已關閉摘要簡報"}
        if self.viewer is None:
            return {"status": "error", "message": "原文功能未啟用"}
        return self.viewer.close()

    def _summarize_source(self) -> dict:
        if self.viewer is None:
            return {"status": "error", "message": "無法整理原文"}
        if not self.state.source_view:
            return {"status": "no_source", "message": "請先打開要整理的原文"}
        job = self.jobs.summarize_source(self.viewer)
        if not job:
            return {"status": "busy", "message": "正在查詢或整理中，請稍等"}
        res = self.jobs.wait_summary(job, 180)
        if res is None:
            self.jobs.cancel_summary(job)
            return {"status": "error", "message": "整理太久，已取消"}
        if res.get("status") != "ok":
            return {"status": "error", "message": res.get("message") or "整理失敗"}
        return {"status": "ok", "message": f"整理好了，共 {res['pages']} 頁"}

    def _open_citation(self, number: int) -> dict:
        if self.viewer is None:
            return {"status": "error", "message": "原文功能未啟用"}
        return self.viewer.open_reference(f"n:{int(number)}")

    def _read_answer(self, card: int = 0) -> dict:
        """使用者要求才念：字卡（全部或第 N 張，不念出處）；OE 簡報播放中念目前這一頁的原文。"""
        st = self.state
        if st.temp_kind == "oe" and st.presenting and st.deck is not None:
            footer = _footer_line_re()
            lines = [l for l in st.deck.page_text(st.page).splitlines() if l.strip() and not footer.match(l)]
            return {"status": "ok", "message": " ".join(lines) or "這一頁沒有文字"}
        ans = st.answer
        if ans is None or ans.status != "answered" or not ans.cards:
            return {"status": "not_found", "message": "目前沒有可以念的字卡"}
        n = int(card or 0)
        if n == 0:
            parts = [c.point_zh for c in ans.cards]
            if ans.derivations:
                parts.append(derived_label() + "；".join(d.text for d in ans.derivations))
            parts += [note_label() + x for x in ans.derivation_notes]
            return {"status": "ok", "message": "。".join(parts)}
        if not 1 <= n <= len(ans.cards):
            return {"status": "not_found", "message": f"沒有第 {n} 張字卡"}
        return {"status": "ok", "message": ans.cards[n - 1].point_zh}

    def _host_only_command(self, action: str = "") -> dict:
        return dict(REFUSED)

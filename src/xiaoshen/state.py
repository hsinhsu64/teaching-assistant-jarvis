"""應用狀態：目前簡報／頁碼／抽屜／作答，以及依題庫自動派題。

所有會改變畫面的方法結束時呼叫 on_change()，由伺服器接到 WebSocket 廣播。
任何附帶動作（寫紀錄等）失敗只記警告，不得中斷翻頁或作答。
"""

from __future__ import annotations

import secrets
import threading
import uuid
import time
from pathlib import Path
from typing import Callable, Literal

from .answering import Answer
from .config import Config
from .converter import ConversionReport, Converter, convert_pending, find_sources, list_deck_entries, needs_conversion
from .deck import Deck, DeckError, list_decks
from .host_phone import HostPairing
from .persona import app_info
from .phone_mic import PhoneMic
from .phi import find_phi
from .quiz import Question, QuizClosed, QuizRun
from .records import save_record

VOICE_CONFIRM_TTL = 180  # 語音查詢等確認的秒數，過了作廢（spec §5.4，2026-11-06）

Audience = Literal["display", "host"]


DECK_CONVERTING_MESSAGE = "這份簡報要先轉成 PDF，需要一段時間，好了會自動播放"


class AppState:
    def __init__(self, config: Config, bank: dict[tuple[str, int], Question],
                 on_change: Callable[[], None] | None = None):
        self.config = config
        self.bank = bank
        self.on_change = on_change
        # 狀態會被事件迴圈、FastAPI 執行緒池與 AI 背景工作同時修改；「檢查再寫入」都要在鎖內
        self.lock = threading.RLock()
        self.deck: Deck | None = None
        # 前端用來讓圖片快取失效：每個行程、每次開檔都不同（避免重啟後投影沿用舊簡報的快取）
        self.deck_version = ""
        self._open_count = 0
        self.page = 0
        self.drawer: Literal["hidden", "quiz", "results", "answer", "working", "teacher"] = "hidden"
        self.run: QuizRun | None = None
        self.token = secrets.token_urlsafe(16)
        self.public_base: str | None = None
        self.phone_reachable = True  # 晨會中定期檢查；通道斷了就不再顯示 QR Code
        self.phone_pending = False  # 新網址還在確認手機連不連得上：先不顯示 QR Code
        self.warnings: list[str] = []
        self.checks: list[dict] = []
        self.conversion_check: dict | None = None  # 簡報 pptx 轉檔狀態，併入自我檢查
        self._auto_dispatched: set[tuple[str, int]] = set()
        # Phase 2：AI 回答與出題草稿（草稿只給主持人畫面）
        self.answer: Answer | None = None
        self.results_base: Answer | None = None  # 目前的結果：最近一次查到文獻的回答（問「哪一篇最新」用）
        self.answer_job: dict | None = None
        self.quiz_job: dict | None = None
        self.drafts: list[Question] = []
        self.draft_kind: Literal["single", "exam"] | None = None
        self.draft_notes: list[str] = []
        self.show_quotes = False
        self.figure_zoom: int | None = None   # 投影放大中的 OpenEvidence 附圖
        self.source_view: dict | None = None  # 投影顯示中的出處原文（§6.2a）：摘要／那一頁／原文網頁
        self.source_gen = 0                   # 每次關閉或換回答 +1；查詢中的舊要求據此作廢
        self.web_closer = None                # 關掉投影上的原文網頁視窗（啟動時接上）
        self._web_close_thread: threading.Thread | None = None
        self.page_ocr: dict[int, str] = {}     # 投影片圖片辨識（0-based 頁 → 抄錄文字）
        self.ocr_progress: dict | None = None  # {done, total[, failed]}
        self.phi_hits: list[str] = []
        # Phase 3：語音
        self.voice_phi_alert = False          # 語音逐字稿疑似含識別資訊（黃燈）
        self.voice_ref: str | None = None     # 語音選定的參考資料
        self.ref_candidates: list[str] = []   # 名稱有多份符合時，給主持人選
        # 語音問題含英文名詞：搜尋前先確認（§5.4）。{question, original, route, refs, at}
        self.voice_confirm: dict | None = None
        self.voice_status = "off"             # off／standby／listening／thinking／speaking／offline
        self.voice_transcript: list[str] = [] # 「你：…」「（助理名）：…」最近 20 行（只給主持人）
        self.wake_handler: Callable[[], None] | None = None   # 由 launcher 接上 VoiceSession.wake
        self.stop_handler: Callable[[], None] | None = None
        self.quit_handler: Callable[..., None] | None = None  # 主持人「結束程式」／語音結束；由 launcher 接上
        self._last_phone_wake = float("-inf")
        # 主畫面（§6.0）：開機在主畫面，說「播放 xxx 簡報」才播放
        self.presenting = False
        # 原文重點摘要簡報（§6.2b）：臨時播放，不取代「今天的簡報」；結束後回到原本的畫面
        self.summary_path: Path | None = None   # 臨時簡報（摘要簡報或 OE 簡報）播放中
        self.temp_kind: str | None = None        # "summary"／"oe"（§6.2c）
        self.last_temp: dict[str, Path] = {}     # 每一種最近一份：語音「播放摘要簡報／OE 簡報」重播
        self.oe_pending: Path | None = None      # 做好但還沒播的 OE 簡報（正在播簡報要先問、派題中先等）
        self._summary_restore: tuple | None = None  # (簡報, 頁, 是否播放中)
        self.summary_note = ""  # 最近一次整理失敗的原因（只給主持人看）
        # 手機（§6.3a，2026-11-18 修訂）：投影控制與搜尋文獻都預設關，主持人在「手機操作」區打開；「詢問老師」一直開著
        self.oe_recheck: Callable | None = None  # 主持人按「重新檢查」OpenEvidence（由 launcher 接上）
        self.host_pairing = HostPairing()  # 主持人手機（§6.3b）
        self.phone_mic = PhoneMic()        # 主持人手機的麥克風（§6.3b，2026-11-08）
        self.phone_control_enabled = False
        self.phone_search_enabled = False
        self.teacher_questions: list[dict] = []  # {id, text, phi_hits}：只在記憶體、只給主持人看
        self.teacher_shown: list[str] = []       # 投影抽屜上顯示中的提問（可以同時好幾題，依按的順序）
        self.deck_paths: list[Path] = []       # 簡報\ 內的 PDF 與還沒轉的 pptx（launcher 定期更新）
        # 把一份 pptx 轉成 PDF 並回傳 PDF 路徑（launcher 設定；None＝這台電腦不能轉）
        self.deck_converter: Callable[[Path], Path] | None = None
        self.deck_converting: dict | None = None  # {"name", "play"}：要播的 pptx 正在轉成 PDF
        self._convert_thread: threading.Thread | None = None
        self.deck_candidates: list[str] = []  # 語音說的簡報名稱有多份符合
        self.pending_switch: str | None = None  # 播放中要改播的簡報，等口頭確認
        self._last_page: dict[str, int] = {}  # 每份簡報上次播放停的頁（只在記憶體）
        self._user_line_open = False          # 「你」那一句還沒說完 → 先不上投影
        self._line_turns: list[int] = []      # voice_transcript 每一行屬於第幾輪（個資檢查整輪合併）
        self._turn_no = 0
        self.voice_cloud_ok = True            # 最近一次連語音雲端是否成功（斷網時 voice_status 會自己回 standby）
        self.recorder = None                  # 會議錄音器（recording.Recorder；launcher 接上）

    def set_voice_confirm(self, question: str, original: str, route: str, refs: list[str]) -> None:
        self.voice_confirm = {"question": question, "original": original, "route": route,
                              "refs": list(refs), "at": time.time()}
        self._changed()

    def clear_voice_confirm(self) -> None:
        if self.voice_confirm is not None:
            self.voice_confirm = None
            self._changed()

    def active_voice_confirm(self) -> dict | None:
        """還在等確認的語音查詢；超過 VOICE_CONFIRM_TTL 秒視為作廢（回傳 None）。"""
        c = self.voice_confirm
        if c is None or time.time() - c["at"] > VOICE_CONFIRM_TTL:
            return None
        return c

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    # ---- 簡報 ----

    def open_deck(self, path: Path) -> None:
        try:
            self.deck = Deck(path)
        except DeckError as e:
            self.deck = None
            self.warnings.append(str(e))
        self._new_version(path)
        self.page_ocr = {}
        self.ocr_progress = None
        self.page = 0
        # 不清空 _auto_dispatched：同一場重播同一份簡報，已自動派過的題不再自動派（§6.0）
        self._changed()

    def _new_version(self, path) -> None:
        self._open_count += 1
        try:
            mtime = Path(path).stat().st_mtime_ns
        except OSError:
            mtime = 0
        self.deck_version = f"{self.token[:8]}-{self._open_count}-{mtime}"

    @property
    def answer_deck(self) -> Deck | None:
        """回答與出題用的「今天的簡報」：摘要簡報播放中仍是原本那份（摘要簡報不當作出處，§6.2b）。"""
        if self.summary_path is not None and self._summary_restore is not None:
            return self._summary_restore[0]
        return self.deck

    @property
    def last_summary(self) -> Path | None:
        return self.last_temp.get("summary")

    @last_summary.setter
    def last_summary(self, path: Path | None) -> None:
        if path is None:
            self.last_temp.pop("summary", None)
        else:
            self.last_temp["summary"] = Path(path)

    def play_summary(self, path: Path, deck: Deck | None = None, kind: str = "summary") -> bool:
        """投影幕播放摘要簡報（從第 1 頁）。開不起來就留在原畫面並回傳 False。
        deck：呼叫端先開好的（不在 state lock 裡等 PDF_LOCK，審查 #4）。"""
        if deck is None:
            try:
                deck = Deck(path)
            except DeckError as e:
                with self.lock:
                    self.warnings.append(str(e))
                self._changed()
                return False
        with self.lock:
            if self.summary_path is None:
                self._summary_restore = (self.deck, self.page, self.presenting)
            self.deck, self.page, self.presenting = deck, 0, True
            self.summary_path = Path(path)
            self.temp_kind = kind
            self.last_temp[kind] = Path(path)
            if kind == "oe":
                self.drawer = "hidden"  # OE 簡報本身就是回答，抽屜會蓋住右半邊（§6.2c）
            self.deck_candidates = []
            self.pending_switch = None
            self._new_version(path)
            if self.source_view is not None:
                self.close_source_view()
        self._changed()
        return True

    def _end_summary(self, resume: bool) -> None:
        """離開摘要簡報。resume：回到原本的畫面（原本在播就接著播那一頁）。呼叫端持有 lock。"""
        deck, page, was_presenting = self._summary_restore or (None, 0, False)
        self.summary_path = None
        self.temp_kind = None
        self._summary_restore = None
        if deck is not None:
            self.deck, self.page = deck, page
            self._last_page[deck.name] = page
            self._new_version(deck.path)
        else:  # 叫出摘要前沒有開簡報：摘要簡報不能留下來變成「今天的簡報」（審查 #1）
            self.deck, self.page = None, 0
        if resume:
            self.presenting = was_presenting

    def set_deck_paths(self, paths: list[Path]) -> None:
        paths = list(paths)
        if paths == self.deck_paths:
            return
        self.deck_paths = paths
        names = {p.stem for p in paths}
        self.deck_candidates = [n for n in self.deck_candidates if n in names]
        self._changed()

    def list_deck_files(self) -> list[Path]:
        """簡報清單：自動轉檔開著時含還沒轉的 pptx（選到時才轉），關掉時只列 PDF。"""
        folder = self.config.folder("簡報")
        return list_deck_entries(folder) if self.config.auto_convert_pptx else list_decks(folder)

    def refresh_deck_list(self) -> None:
        """重新列出簡報；還沒開任何簡報時打開最新的那份（「今天的簡報」，不投影）。
        最新的還是 pptx 就先不開，等它轉好（不改開比較舊的 PDF）。"""
        paths = self.list_deck_files()
        self.set_deck_paths(paths)
        if (self.deck is None and self.deck_converting is None and paths
                and paths[0].suffix.lower() == ".pdf"):
            self.open_deck(paths[0])

    def today_deck_path(self) -> Path | None:
        """「今天的簡報」：開著的那份；還沒開（例如最新的是還沒轉的 pptx）就是清單裡最新的。"""
        if self.answer_deck is not None:
            return Path(self.answer_deck.path)
        return self.deck_paths[0] if self.deck_paths else None

    def request_deck(self, path: Path, play: bool) -> Literal["ready", "converting", "busy", "error"]:
        """主持人要打開（play=False）或播放這份簡報。

        PDF（或已轉好的 pptx）馬上打開／播放，回傳 "ready"；還沒轉的 pptx 在背景轉成 PDF，
        回傳 "converting"，轉完自動打開／播放。同時只轉一份：轉別份時回傳 "busy"。"""
        path = Path(path)
        if path.suffix.lower() != ".pdf" and not needs_conversion(path):
            path = path.with_suffix(".pdf")
        if path.suffix.lower() == ".pdf":
            self.play(path) if play else self.open_deck(path)
            return "ready"
        with self.lock:
            busy = self.deck_converting
            if busy is not None:
                if busy["name"] != path.stem:
                    return "busy"
                busy["play"] = busy["play"] or play  # 轉檔中又按「播放」：轉完直接播放
                self._changed()
                return "converting"
            if self.deck_converter is None:
                self.warnings.append(f"這台電腦沒有 PowerPoint，無法轉換：{path.name}（請先手動匯出 PDF）")
                self._changed()
                return "error"
            self.deck_converting = {"name": path.stem, "play": play}
            self._convert_thread = threading.Thread(target=self._convert_then_open, args=(path,), daemon=True)
            self._convert_thread.start()
        self._changed()
        return "converting"

    def play_after_conversion(self) -> str | None:
        """主持人在轉檔中按「播放簡報」：轉好直接播放。回傳正在轉的簡報名稱；沒有在轉就回傳 None。"""
        with self.lock:
            if self.deck_converting is None:
                return None
            self.deck_converting["play"] = True
            name = self.deck_converting["name"]
        self._changed()
        return name

    def deck_request_reply(self, result: str) -> dict:
        """request_deck 不是 "ready" 時要告訴主持人的話（語音照念、手機與主持人畫面顯示）。"""
        if result == "converting":
            return {"status": "converting", "message": DECK_CONVERTING_MESSAGE}
        if result == "busy":
            name = (self.deck_converting or {}).get("name", "另一份簡報")
            return {"status": "busy", "message": f"正在把「{name}」轉成 PDF，轉好後再播放別份"}
        return {"status": "error", "message": "這台電腦沒有 PowerPoint，無法轉換這份簡報，請先從 PowerPoint 匯出 PDF"}

    def _convert_then_open(self, src: Path) -> None:
        pdf = None
        try:
            pdf = self.deck_converter(src)
        except Exception as e:
            self.warnings.append(f"簡報轉檔失敗：{src.name}（{e or e.__class__.__name__}）")
        with self.lock:
            want, self.deck_converting = self.deck_converting, None
        self.set_deck_paths(self.list_deck_files())
        if pdf is not None and want is not None:
            if want["play"]:
                self.play(pdf)
            elif not self.presenting:
                self.open_deck(pdf)
        self._changed()

    def play(self, path: Path) -> None:
        """投影幕開始播放這份簡報：同一份接續上次播放的頁，另一份從第 1 頁開始。不自動派題。"""
        with self.lock:
            if self.summary_path is not None:
                self._end_summary(resume=False)  # 摘要簡報播放中改播別份：之後不再跳回原畫面
            if self.deck is None or Path(self.deck.path) != Path(path):
                try:
                    Deck(path)  # 先確認開得起來：開不起來就留在原本的簡報（播放中不能變成空白）
                except DeckError as e:
                    self.warnings.append(str(e))
                    self._changed()
                    return
                self.open_deck(path)
            if self.deck is None:
                return
            self.page = max(0, min(self._last_page.get(self.deck.name, 0), self.deck.page_count - 1))
            self.presenting = True
            self.deck_candidates = []
            self.pending_switch = None
            if self.source_view is not None:
                self.close_source_view()  # 換到簡報畫面：原文畫面會蓋住投影片（審查 #9）
        self._changed()

    def stop_oe_deck(self) -> bool:
        """主持人按「收回 OE 簡報」：回到叫出 OE 簡報前的畫面。
        現在播的不是 OE 簡報就不動，避免連按時把原本的簡報也關掉。"""
        with self.lock:
            if not (self.presenting and self.temp_kind == "oe"):
                return False
            self.stop_presenting()
            return True

    def stop_presenting(self) -> None:
        """回到主畫面，記住這份簡報停在哪一頁。"""
        with self.lock:
            if not self.presenting:
                return
            if self.summary_path is not None:  # 結束摘要簡報：回到叫出摘要之前的畫面
                self._end_summary(resume=True)
                self.pending_switch = None
                if self.source_view is not None:
                    self.close_source_view()
                self._changed()
                return
            if self.deck is not None:
                self._last_page[self.deck.name] = self.page
            self.presenting = False
            self.pending_switch = None
            if self.source_view is not None:
                self.close_source_view()
        self._changed()

    def reload_deck(self) -> None:
        """同一份簡報內容更新（例如 pptx 改過重轉）：重新載入，盡量停在原頁。"""
        if self.deck is None:
            return
        try:
            self.deck = Deck(self.deck.path)
        except DeckError as e:
            self.warnings.append(str(e))
            return
        self._open_count += 1
        self.deck_version = f"{self.token[:8]}-{self._open_count}-{self.deck.path.stat().st_mtime_ns}"
        self.page_ocr = {}
        self.ocr_progress = None
        self.page = max(0, min(self.page, self.deck.page_count - 1))
        self._changed()

    def goto(self, page: int) -> None:
        if self.deck is None:
            return
        with self.lock:
            self.page = max(0, min(page, self.deck.page_count - 1))
            self.figure_zoom = None
            self._auto_dispatch()
        self._changed()

    def next(self) -> None:
        self.goto(self.page + 1)

    def prev(self) -> None:
        self.goto(self.page - 1)

    def _current_key(self) -> tuple[str, int] | None:
        return (self.deck.name, self.page + 1) if self.deck else None

    def _auto_dispatch(self) -> None:
        if not self.presenting or self.summary_path is not None:  # 主畫面預覽、摘要簡報都不派題
            return
        key = self._current_key()
        if key is None or key not in self.bank or key in self._auto_dispatched:
            return
        if self.run is not None and self.run.status == "open":
            return
        self._auto_dispatched.add(key)
        self.start_run("single", [self.bank[key]], notify=False)

    # ---- 作答 ----

    def dispatch_current(self) -> QuizRun:
        key = self._current_key()
        if key is None or key not in self.bank:
            raise LookupError("本頁沒有題庫題目")
        return self.start_run("single", [self.bank[key]])

    def start_run(self, kind: Literal["single", "exam"], questions: list[Question],
                  notify: bool = True) -> QuizRun:
        with self.lock:
            if self.run is not None and self.run.status == "open":
                raise RuntimeError("目前已有開放中的題目")
            self.run = QuizRun(kind, questions)
            self.drawer = "quiz"
            if (self.answer_job or {}).get("kind") == "summary":
                self.answer_job = None  # 開始派題：放棄整理中的摘要簡報（§6.2b）
            self.figure_zoom = None
            if self.source_view is not None:
                self.close_source_view()  # 原文畫面在最上層，會蓋住題目與 QR（審查 #9）
        if notify:
            self._changed()
        return self.run

    def submit(self, client_id: str, answers: dict[str, int], final: bool) -> None:
        if self.run is None:
            raise QuizClosed("目前沒有題目")
        self.run.submit(client_id, answers, final)
        self._changed()

    def _close_run(self) -> None:
        if self.run is None or self.run.status == "closed":
            return
        self.run.close()
        self.drawer = "results"
        try:
            save_record(self.config.folder("紀錄"), self.deck.name if self.deck else "無簡報", self.run)
        except Exception as e:
            self.warnings.append(f"作答紀錄寫入失敗：{e}")
        self._changed()

    def reveal(self) -> None:
        """公布單題答案。"""
        self._close_run()

    def collect(self) -> None:
        """收卷。"""
        self._close_run()

    def hide_drawer(self) -> None:
        self.drawer = "hidden"
        self.teacher_shown = []  # 收起後再投影別題，前面的不會又冒出來
        self.figure_zoom = None
        self._changed()

    def close_source_view(self) -> None:
        """關閉出處原文畫面（含投影上的原文網頁視窗）。
        世代編號 +1：還在查詢中的舊要求回來時會被丟掉（審查 #1）。
        關網頁視窗可能要好幾秒（結束瀏覽器），另開執行緒做，不佔著呼叫者手上的 lock（審查 #3）。"""
        had_web = bool(self.source_view and self.source_view.get("kind") == "web")
        self.source_view = None
        self.source_gen += 1
        if had_web or self.web_closer is not None:
            self.close_web_async()

    def close_web_async(self) -> None:
        closer = self.web_closer
        if closer is None:
            return

        def run():
            try:
                closer()
            except Exception:
                pass  # 視窗已被手動關掉

        self._web_close_thread = threading.Thread(target=run, daemon=True)
        self._web_close_thread.start()

    def wait_web_close(self, timeout: float = 5) -> None:
        t = self._web_close_thread
        if t is not None:
            t.join(timeout)

    def set_answer(self, answer) -> None:
        """換上新的回答：字卡編號已換，舊的出處原文畫面一併關閉（§6.2a）。呼叫端持有 lock。"""
        self.answer = answer
        self.oe_pending = None  # 舊回答還沒播的 OE 簡報作廢
        if self.source_view is not None:
            self.close_source_view()
        else:
            self.source_gen += 1  # 查詢中的舊要求作廢

    def set_check(self, name: str, level: str, detail: str) -> None:
        """執行中更新某一項自我檢查（例如 OpenEvidence 提問被驗證擋下）。"""
        for check in self.checks:
            if check.get("name") == name:
                check["level"], check["detail"] = level, detail

    # ---- 手機「詢問老師」（§6.3a）：問題只送到主持人畫面，主持人決定要不要投影 ----
    MAX_TEACHER_QUESTIONS = 50

    def add_teacher_question(self, text: str) -> str:
        from .phi import find_phi

        qid = uuid.uuid4().hex[:8]
        with self.lock:
            self.teacher_questions.append({"id": qid, "text": text, "phi_hits": find_phi(text)})
            del self.teacher_questions[:-self.MAX_TEACHER_QUESTIONS]
        self._keep_for_minutes(text)
        self._changed()
        return qid

    def _keep_for_minutes(self, text: str) -> None:
        """錄音期間的提問另存給那一場的會議記錄（使用者 2026-11-23：只收開始～停止之間的）。"""
        rec = self.recorder
        mid = rec.id if rec is not None else None
        if not mid:
            return
        from . import meeting_store

        try:
            meeting_store.add_question(meeting_store.private_dir(), mid, rec.elapsed, text)
        except OSError:
            pass  # 存不了不影響主持人畫面

    def _teacher(self, qid: str) -> dict | None:
        return next((q for q in self.teacher_questions if q["id"] == qid), None)

    def show_teacher_question(self, qid: str) -> bool:
        """投影抽屜顯示這一題（不顯示是誰問的）；疑似含識別資訊的不能投影。"""
        with self.lock:
            q = self._teacher(qid)
            if q is None or q["phi_hits"]:
                return False
            if self.drawer != "teacher":
                self.teacher_shown = []
            if qid not in self.teacher_shown:
                self.teacher_shown.append(qid)
            self.drawer = "teacher"
        self._changed()
        return True

    def hide_teacher_question(self, qid: str) -> bool:
        """從投影上收回這一題；最後一題也收回就關抽屜。"""
        with self.lock:
            if qid not in self.teacher_shown:
                return False
            self.teacher_shown.remove(qid)
            if not self.teacher_shown and self.drawer == "teacher":
                self.drawer = "hidden"
        self._changed()
        return True

    def remove_teacher_question(self, qid: str) -> bool:
        with self.lock:
            q = self._teacher(qid)
            if q is None:
                return False
            self.teacher_questions.remove(q)
            if qid in self.teacher_shown:
                self.teacher_shown.remove(qid)
                if not self.teacher_shown and self.drawer == "teacher":
                    self.drawer = "hidden"
        self._changed()
        return True

    PHONE_WAKE_COOLDOWN = 10.0

    def phone_wake(self) -> bool:
        """手機「發問」：叫醒會議室麥克風。回傳是否被忽略（冷卻中或已在對話）。語音未啟用 → LookupError。"""
        with self.lock:
            if self.wake_handler is None or self.voice_status == "off":
                raise LookupError("語音未啟用")
            now = time.monotonic()
            if self.voice_status != "standby" or now - self._last_phone_wake < self.PHONE_WAKE_COOLDOWN:
                return True
            self._last_phone_wake = now
        self.wake_handler()
        return False

    def add_transcript(self, who: str, text: str) -> None:
        """語音逐字稿是一段一段來的：同一個人連續說的話接在同一行。"""
        prefix = f"{who}："
        self._user_line_open = who == "你"
        if (self.voice_transcript and self.voice_transcript[-1].startswith(prefix)
                and self._line_turns and self._line_turns[-1] == self._turn_no):  # 不跨輪合併
            self.voice_transcript[-1] += text
        else:
            self.voice_transcript.append(prefix + text)
            self._line_turns.append(self._turn_no)
            del self.voice_transcript[:-20]
            del self._line_turns[:-20]
        self._changed()

    def close_transcript_line(self) -> None:
        """「你」那一句說完了（工具呼叫或回合結束）→ 可以上投影。"""
        self._user_line_open = False
        self._changed()

    def end_transcript_turn(self) -> None:
        """一輪對話結束（turn_complete）：之後的話算下一輪。"""
        self._user_line_open = False
        self._turn_no += 1
        self._changed()

    def display_transcript(self) -> list[str]:
        """投影用的對話：含個資的句子遮蔽；「你」還沒說完的那一句先不顯示。
        「你」的話可能被助理的話切成幾段（逐字稿晚到），同一輪的「你」要合併起來檢查，整輪一起遮。"""
        lines = list(self.voice_transcript)
        turns = list(self._line_turns)
        if lines and self._user_line_open and lines[-1].startswith("你："):
            lines.pop()
        said: dict[int, str] = {}
        for line, turn in zip(self.voice_transcript, turns):
            if line.startswith("你："):
                said[turn] = said.get(turn, "") + line[2:]
        out = []
        for line, turn in zip(lines, turns):
            who, sep, text = line.partition("：")
            hit = sep and (find_phi(text) or (who == "你" and find_phi(said.get(turn, ""))))
            out.append(f"{who}：（含個資，已隱藏）" if hit else line)
        return out

    def voice_down(self) -> bool:
        """語音不在線（未啟用、離線、或最近一次連雲端失敗）→ 主持人可用「播放簡報」（§6.0）。"""
        return self.voice_status in ("off", "offline") or not self.voice_cloud_ok

    def run_is_open(self) -> bool:
        return self.run is not None and self.run.status == "open"

    # ---- AI 回答與草稿 ----

    def show_answer(self) -> None:
        if self.answer is not None and not self.run_is_open():
            self.drawer = "answer"
            self._changed()

    def toggle_quotes(self) -> None:
        self.show_quotes = not self.show_quotes
        self._changed()

    def publish_drafts(self) -> QuizRun:
        with self.lock:
            if not self.drafts:
                raise LookupError("沒有題目草稿")
            kind = self.draft_kind or ("single" if len(self.drafts) == 1 else "exam")
            run = self.start_run(kind, list(self.drafts))  # 已有開放中的題目會拋 RuntimeError，草稿保留
            self.drafts = []
            self.draft_notes = []
        self._changed()
        return run

    def remove_draft(self, qid: str) -> None:
        with self.lock:
            self.drafts = [q for q in self.drafts if q.id != qid]
        self._changed()

    # ---- 快照 ----

    def phone_url(self) -> str | None:
        if not self.public_base or not self.phone_reachable or self.phone_pending:
            return None
        return f"{self.public_base}/q?t={self.token}"

    def snapshot(self, audience: Audience) -> dict:
        run = None
        if self.run is not None:
            run = self.run.view_for_host() if audience == "host" else self.run.view_for_display()
        snap = {
            "deck": ({"name": self.deck.name, "page_count": self.deck.page_count,
                      "version": self.deck_version, "temp_kind": self.temp_kind} if self.deck else None),
            "page": self.page,
            "drawer": self.drawer,
            "teacher_questions_shown": ([{"text": q["text"]} for i in self.teacher_shown
                                         if (q := self._teacher(i)) is not None] if self.drawer == "teacher" else []),
            "run": run,
            "phone_url": self.phone_url(),
            "phone_pending": self.phone_pending,
            "deck_converting": dict(self.deck_converting) if self.deck_converting else None,
            "app": app_info(),
            "keys": {"next": self.config.keys_next, "prev": self.config.keys_prev,
                     "drawer": self.config.keys_drawer, "wake": self.config.keys_wake},
        }
        answer = (self.answer.to_dict(with_quotes=audience == "host" or self.show_quotes,
                                      for_display=audience == "display") if self.answer else None)
        if answer and answer.get("oe_deck") and answer["oe_deck"]["play"] == "oe_playing" and not (
                self.temp_kind == "oe" and str(self.summary_path) == self.answer.oe_deck):
            answer["oe_deck"]["play"] = "oe_ended"  # OE 簡報已收回：主持人畫面改顯示「已結束」＋再播一次
        snap["ai_answer"] = answer
        snap["answer_job"] = dict(self.answer_job) if self.answer_job else None
        snap["show_quotes"] = self.show_quotes
        snap["figure_zoom"] = self.figure_zoom
        snap["source_view"] = self.source_view
        snap["voice_status"] = self.voice_status
        snap["voice_phi_alert"] = self.voice_phi_alert
        pending = self.active_voice_confirm()
        snap["voice_confirm"] = ({"question": pending["question"]} | (
            {"route": pending["route"]} if audience == "host" else {})) if pending else None
        snap["presenting"] = self.presenting
        snap["voice_down"] = self.voice_down()
        rec = self.recorder
        snap["recording"] = {"active": bool(rec and rec.is_recording), "elapsed": int(rec.elapsed) if rec else 0,
                             "id": rec.id if rec else None}
        snap["decks"] = [{"name": p.stem} for p in self.deck_paths]
        snap["deck_candidates"] = list(self.deck_candidates)
        snap["voice_transcript"] = (list(self.voice_transcript) if audience == "host"
                                    else self.display_transcript())
        if audience == "host":
            snap["summary_note"] = self.summary_note
            snap["recording"]["available"] = rec is not None
            snap["recording"]["saved"] = rec.last_path.name if rec and rec.last_path else ""
            snap["recording"]["error"] = rec.last_error if rec else ""
            snap["host_phone"] = {"paired": self.host_pairing.paired}
            snap["phone_control_enabled"] = self.phone_control_enabled
            snap["phone_search_enabled"] = self.phone_search_enabled
            snap["teacher_questions"] = [{"id": q["id"], "text": q["text"], "phi": bool(q["phi_hits"]),
                                          "phi_hits": list(q["phi_hits"]),
                                          "shown": self.drawer == "teacher" and q["id"] in self.teacher_shown}
                                         for q in self.teacher_questions]
            snap["quiz_job"] = dict(self.quiz_job) if self.quiz_job else None
            snap["drafts"] = [q.to_dict() for q in self.drafts]
            snap["draft_kind"] = self.draft_kind
            snap["draft_notes"] = list(self.draft_notes)
            snap["phi_hits"] = list(self.phi_hits)
            snap["ocr_progress"] = self.ocr_progress
            snap["voice_ref"] = self.voice_ref
            snap["ref_candidates"] = list(self.ref_candidates)
            snap["warnings"] = list(self.warnings)
            snap["checks"] = list(self.checks)
            snap["has_bank_question"] = self._current_key() in self.bank
        return snap


def check_phone_link(state: AppState, http_get: Callable[[str], int]) -> None:
    """晨會中定期呼叫：確認手機作答網址仍可連，更新自我檢查與投影上的 QR Code。"""
    if not state.public_base:
        return
    try:
        ok = http_get(state.public_base + "/healthz") == 200
    except Exception:
        ok = False
    changed = ok != state.phone_reachable
    state.phone_reachable = ok
    for check in state.checks:
        if check.get("name") == "手機作答":
            new_level = "ok" if ok else "fail"
            if check.get("level") != new_level:
                changed = True
            check["level"] = new_level
            check["detail"] = state.public_base if ok else "對外通道中斷，手機暫時無法作答"
    if changed:
        state._changed()


def _set_conversion_check(state: AppState, level: str, detail: str) -> None:
    check = {"name": "簡報轉檔", "level": level, "detail": detail}
    state.conversion_check = check
    for i, existing in enumerate(state.checks):
        if existing.get("name") == "簡報轉檔":
            state.checks[i] = dict(check)


def refresh_presentations(state: AppState, convert: Converter | None, reported: set) -> ConversionReport:
    """把「簡報」裡需要轉的 pptx／ppt 轉成 PDF。convert 為 None 表示這台電腦沒有 PowerPoint。"""
    folder = state.config.folder("簡報")
    if not state.config.auto_convert_pptx:
        _set_conversion_check(state, "warn", "已關閉自動轉檔，請自己匯出 PDF")
        return ConversionReport()
    if convert is None:
        pending = [p.name for p in find_sources(folder) if not p.with_suffix(".pdf").exists()]
        if pending:
            _set_conversion_check(state, "warn", "這台電腦沒有 PowerPoint，無法轉換：" + "、".join(pending)
                                  + "（請先手動匯出 PDF）")
        return ConversionReport()
    report = convert_pending(folder, convert, reported)
    for name, err in report.failed:
        state.warnings.append(f"簡報轉檔失敗：{name}（{err}）")
    if report.failed or (state.conversion_check or {}).get("level") == "warn" and report.attempted:
        failed = [name for name, _ in report.failed] or [n for n in report.attempted if n not in report.converted]
        if failed:
            _set_conversion_check(state, "warn", "轉檔失敗：" + "、".join(failed) + "（會自動重試）")
    if report.converted and not report.failed:
        _set_conversion_check(state, "ok", "已轉換：" + "、".join(report.converted))
    if state.deck is not None and state.deck.name in {Path(n).stem for n in report.converted}:
        state.reload_deck()
    elif report.converted or report.failed:
        state._changed()
    return report

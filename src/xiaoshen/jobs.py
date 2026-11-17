"""背景 AI 工作：回答與出題各一個工作槽，新工作取代舊工作，舊結果回來時丟棄。

送出前先在本機做識別資訊檢查；命中就不送任何雲端。
所有例外都在執行緒內轉成畫面上的一行訊息，不外漏、不影響翻頁與作答。
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from .answering import (Answer, Source, answer_from_model_knowledge, answer_from_openevidence, answer_from_pages,
                        answer_from_pubmed, interpret_spoken)
from . import eventlog
from . import deck as deckmod
from .config import Config
from .library import Library
from .llm import LLM
from .oe_client import BLOCKED_MSG, ask_openevidence
from .phi import find_phi
from .quizgen import MAX_N, MIN_N, generate_questions
from .slide_ocr import OCR_MARK
from .source_text import SourceUnreadable
from .summary import SummaryError, summarize
from .summary_deck import render_summary_pdf, summary_filename
from .oe_deck import oe_deck_filename, render_oe_deck
from .answering import clarify_question
from .state import AppState
from .vocabulary import deck_terms

Route = Literal["openevidence", "pubmed", "references", "deck", "page"]
ROUTES = ("openevidence", "pubmed", "references", "deck", "page")
DECK_LABEL = "今天的簡報"
FALLBACK_LABEL = {"deck": DECK_LABEL, "page": "目前這一頁", "references": "參考資料"}
_SUPERSEDED = object()


def _outcome(label: str, ans: Answer) -> tuple[str, str]:
    """（來源, 無法使用的原因）；查過但沒有結果時原因是空字串。"""
    if ans.status != "error":
        return label, ""
    if label == "OpenEvidence":
        return label, "OpenEvidence 需要重新驗證" if ans.error == BLOCKED_MSG else "OpenEvidence 無法使用"
    if label == "PubMed":
        return label, "PubMed 暫時無法連線"
    return label, f"{label}無法使用"


def _sources_note(outcomes: list[tuple[str, str]]) -> str:
    """「今天的簡報、OpenEvidence 都沒有結果、PubMed 暫時無法連線」：沒有結果與無法使用分開講
    （2026-11-05 實機：OE 被驗證擋、NCBI 當機，卻顯示「都沒有結果」）。"""
    empty = [label for label, why in outcomes if not why]
    parts = [("、".join(empty) + (" 都沒有結果" if len(empty) > 1 else " 沒有結果"))] if empty else []
    return "、".join(parts + [why for _, why in outcomes if why])


class JobRunner:
    def __init__(self, state: AppState, llm: LLM, config: Config, library: Library,
                 ask: Callable = ask_openevidence, pubmed: Callable | None = None,
                 knowledge_fallback: bool = False):
        self.state = state
        self.pubmed_fn = pubmed  # None：不查 PubMed（測試預設，避免連網）；啟動時接 search_pubmed
        # PubMed 也沒有 → Gemini 自身知識；測試預設關閉
        self.knowledge_fallback = knowledge_fallback
        self.llm = llm
        self.config = config
        self.library = library
        self.ask_fn = ask
        self._threads: list[threading.Thread] = []
        self._fix: dict[str, dict] = {}  # 每個查詢：是否語音、名詞是否已校正過（每題最多一次）
        self._last_quiz: tuple[list[Source], str] | None = None
        # 語音要等結果才能唸：工作結束時把結果放這裡並通知
        self._done = threading.Condition()
        self._results: dict[str, object] = {}
        self._oe_decks: dict[str, object] = {}  # 做好的 OE 簡報（路徑 → 已開好的 Deck）

    def _publish(self, job_id: str, result: object) -> None:
        with self._done:
            self._results[job_id] = result
            self._done.notify_all()

    def _wait(self, job_id: str, timeout: float):
        with self._done:
            if not self._done.wait_for(lambda: job_id in self._results, timeout):
                return None
            result = self._results.pop(job_id)
        return None if result is _SUPERSEDED else result

    def wait_answer(self, job_id: str, timeout: float = 210) -> Answer | None:
        """等這個問題的回答；被較新的問題取代或逾時回傳 None。"""
        return self._wait(job_id, timeout)

    def wait_summary(self, job_id: str, timeout: float = 180) -> dict | None:
        """等摘要簡報：{"status": "ok", pages, unverified, path}、{"status": "error", message}、
        被取消或取代 {"status": "superseded"}；逾時回傳 None（審查 #2：兩者要分開，才不會說錯原因）。"""
        with self._done:
            if not self._done.wait_for(lambda: job_id in self._results, timeout):
                return None
            result = self._results.pop(job_id)
        return {"status": "superseded", "message": "已取消整理"} if result is _SUPERSEDED else result

    def wait_quiz(self, job_id: str, timeout: float = 120) -> tuple[int, list[str]] | None:
        """等出題結果：（草稿題數, 備註）；被取代或逾時回傳 None。"""
        return self._wait(job_id, timeout)

    # ---- 共用 ----

    def _phi_blocked(self, text: str) -> bool:
        hits = find_phi(text)
        self.state.phi_hits = hits
        if hits:
            self.state.warnings.append("疑似含病人識別資訊，未送出：" + "、".join(hits))
            self.state._changed()
        return bool(hits)

    def _spawn(self, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, daemon=True)
        self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()

    def wait_idle(self, timeout: float = 10) -> None:
        """測試與開發用：等所有背景工作結束。"""
        end = time.time() + timeout
        for t in list(self._threads):
            t.join(max(0, end - time.time()))

    def wait_for(self, cond: Callable[[], bool], timeout: float = 10) -> None:
        end = time.time() + timeout
        while not cond() and time.time() < end:
            time.sleep(0.01)

    def sources(self, route: str, ref_names: list[str]) -> list[Source]:
        """依路線組出（標籤, 逐頁文字）。找不到資料時拋 ValueError（訊息給主持人看）。"""
        if route in ("deck", "page"):
            deck = self.state.answer_deck  # 摘要簡報不當作出處（§6.2b）
            if deck is None:
                raise ValueError("目前沒有開啟簡報")
            ocr = self.state.page_ocr
            pages = [deck.page_text(i) + (f"\n{OCR_MARK}\n{ocr[i]}" if ocr.get(i) else "")
                     for i in range(deck.page_count)]
            # 「目前這一頁」也送整份簡報（以該頁為主，可引用其他頁的規則），焦點頁由 ask() 指定
            return [(DECK_LABEL, pages)]
        if route == "references":
            if ref_names:
                missing = [n for n in ref_names if n not in self.library.docs]
                if missing:
                    raise ValueError("找不到參考資料：" + "、".join(missing))
                docs = [self.library.docs[n] for n in ref_names]
            else:
                docs = [d for d in self.library.docs.values() if not d.scanned]
            if not docs:
                raise ValueError("參考資料資料夾沒有可讀的資料")
            return [(d.name, d.pages) for d in docs]
        raise ValueError(f"未知的來源：{route}")

    # ---- 回答 ----

    def _vocabulary(self) -> list[str]:
        """今天簡報的專有名詞＋參考資料檔名（給語音問題校正用）。"""
        deck = self.state.answer_deck
        terms = deck_terms([deck.page_text(i) for i in range(deck.page_count)]) if deck else []
        return terms + [n for n in self.library.docs if n not in terms]

    def ask(self, text: str, route: str, ref_names: list[str], spoken: bool = False,
            confirmed: bool = False) -> str:
        """回傳工作 id；命中識別資訊則不送出並回傳空字串。
        confirmed：使用者已確認過的語音問題，送 OpenEvidence 前不再反問（§5.4）。"""
        if route not in ROUTES:
            raise ValueError(f"未知的來源：{route}")
        if self._phi_blocked(text):
            return ""
        job_id = uuid.uuid4().hex[:8]
        st = self.state
        if route == "page" and st.summary_path is not None:
            route = "deck"  # 摘要簡報播放中問「這頁」：改查今天的簡報（§6.2b）
        focus_page = st.page + 1 if route == "page" else None  # 問的當下的頁，不是查詢完成時的頁
        asked_deck = st.answer_deck  # 問的當下的簡報：之後換簡報，「那一頁」仍指這份（審查 #2）
        with st.lock:
            st.answer_job = {"id": job_id, "kind": "answer", "label": text, "route": route, "started_at": time.time()}
            if not st.run_is_open():
                st.drawer = "working"
        st._changed()

        def work() -> None:
            # 語音問題：第一次照原句查，查不到才校正聽錯的醫學名詞再重查（講對時不用多等）
            self._fix[job_id] = {"spoken": spoken, "done": False}
            try:
                if route == "pubmed":
                    ans = self._retry_fixed(job_id, text, route, self._pubmed(text, text),
                                            lambda q: self._pubmed(text, q))
                elif route == "openevidence":
                    ans = self._retry_fixed(job_id, text, route, self._oe(text, text, ask_back=not confirmed),
                                            lambda q: self._oe(text, q, ask_back=not confirmed))
                else:
                    srcs = self.sources(route, ref_names)
                    ans = answer_from_pages(text, srcs, self.llm, self.config.model_answer,
                                            self.config.max_reference_chars,
                                            route="references" if route == "references" else "deck", spoken=spoken,
                                            focus_page=focus_page)
                    if asked_deck is not None:
                        ans.deck_path, ans.deck_hash = str(asked_deck.path), asked_deck.content_hash
                if route not in ("openevidence", "pubmed") and ans.status == "not_found":
                    ans = self._fallback_to_openevidence(job_id, text, route, ans)
                if route == "openevidence" and ans.status == "error" and ans.error == BLOCKED_MSG:
                    st.set_check("OpenEvidence", "warn", "搜尋路線停用：" + BLOCKED_MSG)
                if route == "openevidence" and ans.status in ("not_found", "error"):
                    ans = self._fallback_to_pubmed(job_id, text, ans, tried=[_outcome("OpenEvidence", ans)])
                if route == "pubmed" and ans.status in ("not_found", "error"):
                    ans = self._fallback_to_model(job_id, text, ans, [_outcome("PubMed", ans)])
                if (ans.route == "openevidence" and ans.status == "answered" and ans.oe is not None
                        and (st.answer_job or {}).get("id") == job_id):  # 已被取代就不做（審查 I1）
                    ans = self._build_oe_deck(ans)
            except ValueError as e:
                ans = Answer(job_id, text, _answer_route(route), "error", error=str(e))
            except Exception as e:  # 不讓執行緒例外外漏
                ans = Answer(job_id, text, _answer_route(route), "error", error=f"查詢失敗：{e.__class__.__name__}")
            finally:
                self._fix.pop(job_id, None)
            with st.lock:
                if (st.answer_job or {}).get("id") != job_id:
                    self._publish(job_id, _SUPERSEDED)
                    return  # 已被較新的問題取代
                st.set_answer(ans)
                st.figure_zoom = None
                st.card = None
                st.answer_job = None
                if not st.run_is_open():
                    st.drawer = "answer"
                if ans.oe_deck:
                    self._place_oe_deck(ans)
            self._publish(job_id, ans)
            st._changed()

        self._spawn(work)
        return job_id

    def propose_spoken(self, text: str):
        """語音問題送外部搜尋前要給使用者確認的版本：先校正聽錯的醫學名詞（§5.4，2026-11-06）。"""
        fast = (self.config.model_fallbacks or [self.config.model_answer])[0]
        return interpret_spoken(text, self._vocabulary(), self.llm, fast)

    def _interpret(self, job_id: str, text: str, route: str) -> tuple[str, Answer | None]:
        """語音問題校正聽錯的醫學名詞。回傳（校正後問題, 沒把握時的反問回答）。"""
        fast = (self.config.model_fallbacks or [self.config.model_answer])[0]
        r = interpret_spoken(text, self._vocabulary(), self.llm, fast)
        if r.clarify_zh:
            return r.question, Answer(job_id, text, _answer_route(route), "clarify", clarify_zh=r.clarify_zh,
                                      interpreted=r.question if r.question != text else "")
        return r.question, None

    def _retry_fixed(self, job_id: str, text: str, route: str, ans: Answer,
                     rerun: Callable[[str], Answer]) -> Answer:
        """語音問題這一關查不到 → 校正聽錯的名詞再查一次（每題最多校正一次）。
        校正後還是查不到：把校正後的問題記在 interpreted，後面的資料庫就用它查。"""
        f = self._fix.get(job_id)
        if not f or not f["spoken"] or f["done"] or ans.status != "not_found" or ans.interpreted:
            return ans
        f["done"] = True
        query, early = self._interpret(job_id, text, route)
        if early:
            return early  # 沒把握：先反問
        if query.strip() == text.strip():
            return ans
        again = rerun(query)
        again.question, again.interpreted = text, query
        if again.status in ("answered", "clarify"):
            return again
        ans.interpreted = query
        return ans

    def _oe(self, text: str, query: str, ask_back: bool = True) -> Answer:
        # 送 OpenEvidence 前先看要不要反問（保留）；已確認過的語音問題不反問
        question = clarify_question(query, self.llm, self.config.model_clarify) if ask_back else ""
        if question:
            return Answer(uuid.uuid4().hex[:8], text, "openevidence", "clarify", clarify_zh=question)
        oe = answer_from_openevidence(query, self.config.oe_script, self.llm, self.config.model_answer,
                                      ask=self.ask_fn)
        oe.question = text
        if query != text:
            oe.interpreted = query
        return oe

    def _fallback_to_openevidence(self, job_id: str, text: str, route: str, ans: Answer) -> Answer:
        """簡報／參考資料沒有提到 → 自動改查 OpenEvidence。投影標明來源已改變。"""
        label = FALLBACK_LABEL[route]
        st = self.state
        with st.lock:
            if (st.answer_job or {}).get("id") != job_id:
                return ans  # 已被新問題取代，不再多查
            st.answer_job = {**st.answer_job, "route": "openevidence", "label": f"{label}沒有，改查 OpenEvidence：{text}"}
        st._changed()
        query = ans.interpreted or text  # 語音聽錯的名詞已在簡報路線被校正
        oe = self._oe(text, query)
        if oe.status != "answered":  # 簡報裡沒有這個詞、OE 也查不到：校正（Giotomine → Gitelman）再查
            oe = self._retry_fixed(job_id, text, "openevidence", oe, lambda q: self._oe(text, q))
            if oe.status == "clarify":
                return oe
            ans.interpreted = ans.interpreted or oe.interpreted
        if oe.status == "answered":
            oe.fallback_from = label
            oe.fallback_note = f"{label}中沒有提到，以下為 OpenEvidence 結果"
            oe.question = text
            oe.interpreted = ans.interpreted
            return oe
        ans.summary_zh = f"{ans.summary_zh}；OpenEvidence：{oe.summary_zh or oe.error or '找不到可靠來源'}"
        return self._fallback_to_pubmed(job_id, text, ans, tried=[(label, ""), _outcome("OpenEvidence", oe)],
                                        from_deck=True)

    def _pubmed(self, text: str, query: str) -> Answer:
        fast = (self.config.model_fallbacks or [self.config.model_answer])[0]
        pm = answer_from_pubmed(query, self.llm, self.config.model_answer, fast, search=self.pubmed_fn)
        pm.question = text
        if query != text:
            pm.interpreted = query
        return pm

    def _fallback_to_pubmed(self, job_id: str, text: str, ans: Answer, tried: list[tuple[str, str]],
                            from_deck: bool = False) -> Answer:
        """OpenEvidence 查不到或無法使用 → 改查 PubMed（搜尋不限 OpenEvidence）。
        tried：前面已查過的來源與無法使用的原因，例如 [("今天的簡報", ""), ("OpenEvidence", "OpenEvidence 需要重新驗證")]。"""
        if self.pubmed_fn is None:
            return self._fallback_to_model(job_id, text, ans, tried)
        st = self.state
        with st.lock:
            if (st.answer_job or {}).get("id") != job_id:
                return ans
            st.answer_job = {**st.answer_job, "route": "pubmed", "label": f"改查 PubMed：{text}"}
        st._changed()
        pm = self._pubmed(text, ans.interpreted or text)
        if pm.status != "answered":
            pm = self._retry_fixed(job_id, text, "pubmed", pm, lambda q: self._pubmed(text, q))
            if pm.status == "clarify":
                return pm
            ans.interpreted = ans.interpreted or pm.interpreted
        if pm.status == "answered":
            pm.interpreted = pm.interpreted or ans.interpreted
            pm.fallback_from = ans.fallback_from or ("" if from_deck else "OpenEvidence")
            pm.fallback_note = f"{_sources_note(tried)}，以下為 PubMed 結果"
            return pm
        ans.summary_zh = f"{ans.summary_zh}；PubMed：{pm.summary_zh or pm.error or '找不到可靠來源'}".lstrip("；")
        return self._fallback_to_model(job_id, text, ans, tried + [_outcome("PubMed", pm)])

    def _fallback_to_model(self, job_id: str, text: str, ans: Answer, tried: list[tuple[str, str]]) -> Answer:
        """資料庫都沒有結果或無法使用 → Gemini Pro 自身知識，明確標示未經文獻驗證。"""
        if not self.knowledge_fallback:
            return ans
        st = self.state
        with st.lock:
            if (st.answer_job or {}).get("id") != job_id:
                return ans
            st.answer_job = {**st.answer_job, "route": "model", "label": f"文獻沒有，改用 Gemini 一般知識：{text}"}
        st._changed()
        f = self._fix.get(job_id)
        if f and f["spoken"] and not f["done"] and not ans.interpreted:  # 資料庫都無法使用、還沒校正過
            f["done"] = True
            query, early = self._interpret(job_id, text, "openevidence")
            if early:
                return early
            if query != text:
                ans.interpreted = query
        gem = answer_from_model_knowledge(ans.interpreted or text, self.llm, self.config.model_answer)
        if gem.status in ("answered", "clarify"):
            gem.question, gem.interpreted = text, ans.interpreted
            gem.fallback_note = f"{_sources_note(tried)}，以下為 Gemini 一般知識，未經文獻驗證"
            return gem
        return ans

    # ---- 出題 ----

    def _auto_focus(self) -> str:
        st = self.state
        parts = []
        if st.deck is not None and st.summary_path is None:
            parts.append(f"目前投影：{st.deck.name} 第 {st.page + 1} 頁")
        if st.answer is not None and st.answer.status == "answered":
            parts.append(f"最近一次回答：{st.answer.summary_zh}")
        return "；".join(parts)

    # ---- OE 簡報（§6.2c）----

    def _build_oe_deck(self, ans: Answer) -> Answer:
        """把 OpenEvidence 原文做成簡報（排版放轉圖子行程）；失敗就回一行錯誤。"""
        t0 = time.monotonic()
        path = self.config.folder("紀錄") / "OE簡報" / oe_deck_filename(ans.question, datetime.now(), ans.id)
        renderer = deckmod._RENDERER
        try:
            args = (ans.question, ans.oe, ans.fallback_note, path)
            info = renderer.run(render_oe_deck, *args) if renderer else render_oe_deck(*args)
            self._oe_decks[str(path)] = deckmod.Deck(path)  # 先開好，不在 state lock 裡等 PDF_LOCK
        except Exception as e:
            return Answer(ans.id, ans.question, "openevidence", "error", error="OpenEvidence 簡報製作失敗",
                          error_detail=f"{e.__class__.__name__}: {e}", fallback_note=ans.fallback_note)
        ans.oe_deck, ans.oe_pages = str(path), info.pages
        eventlog.log("oe_deck", pages=info.pages, figures=info.figures, missing_figures=info.missing_figures,
                     refs=info.refs, seconds=round(time.monotonic() - t0, 1))
        return ans

    def _place_oe_deck(self, ans: Answer) -> None:
        """呼叫端持有 state lock。沒在播簡報 → 直接播；正在播 → 先問；派題中 → 先等。"""
        st = self.state
        path = Path(ans.oe_deck)
        if st.run_is_open():
            st.oe_pending, ans.oe_play = path, "oe_waiting_quiz"
        elif st.presenting and st.temp_kind is None:
            st.oe_pending, ans.oe_play = path, "oe_confirm"
        else:
            st.drawer = "hidden"
            ans.oe_play = "oe_playing" if st.play_summary(path, self._oe_decks.pop(str(path), None), kind="oe") \
                else "oe_confirm"

    def play_pending_oe(self) -> bool:
        """播放做好但還沒播的 OE 簡報（語音說要、或主持人按「播放 OE 簡報」）。"""
        st = self.state
        with st.lock:
            path = st.oe_pending
        if path is None:
            return False
        deck = self._oe_decks.pop(str(path), None) or deckmod.Deck(path)
        with st.lock:
            if st.oe_pending != path:
                return False
            st.oe_pending = None
            ok = st.play_summary(path, deck, kind="oe")
            if ok and st.answer is not None and st.answer.oe_deck == str(path):
                st.answer.oe_play = "oe_playing"
        st._changed()
        return ok

    def play_answer_oe(self) -> bool:
        """主持人按「播放 OE 簡報」：還沒播的先播；已經收回的再播一次。"""
        st = self.state
        if st.oe_pending is not None:
            return self.play_pending_oe()
        ans = st.answer
        if ans is None or not ans.oe_deck or not Path(ans.oe_deck).exists():
            return False
        ok = st.play_summary(Path(ans.oe_deck), kind="oe")
        if ok:
            ans.oe_play = "oe_playing"
            st._changed()
        return ok

    # ---- 原文重點摘要簡報（§6.2b）----

    def summarize_source(self, viewer) -> str:
        """把投影上的原文整份整理成摘要簡報；完成後存檔並在投影幕播放。回傳工作 id。
        佔用回答的工作槽：新的提問、開始派題或取消 → 這次整理作廢，不搶投影。"""
        job_id = uuid.uuid4().hex[:8]
        st, cfg = self.state, self.config
        with st.lock:
            if st.answer_job is not None:
                return ""  # 同時只能有一份整理；也不能丟掉還在查的問題（審查 #2、#3）
            view = st.source_view or {}
            kind = view.get("kind", "")
            st.summary_note = ""
            st.answer_job = {"id": job_id, "kind": "summary", "label": f"整理重點：{view.get('citation') or ''}".rstrip("："),
                             "started_at": time.time()}
        st._changed()

        def work() -> None:
            t0 = time.monotonic()
            doc = summary = path = summary_deck = None
            pages = 0
            try:
                doc = viewer.read_full()
                left = max(30.0, cfg.summary_timeout_seconds - (time.monotonic() - t0))
                summary = summarize(doc, self.llm, cfg.model_summary, left, cfg.summary_max_chars,
                                    cfg.summary_chunk_chars)
                path = cfg.folder("紀錄") / "摘要簡報" / summary_filename(doc.title, datetime.now())
                renderer = deckmod._RENDERER
                # 排版會卡住 GIL（30 張約 1 秒）：有轉圖子行程就交給它（審查 #4）
                pages = renderer.run(render_summary_pdf, summary, path) if renderer else render_summary_pdf(summary, path)
                summary_deck = deckmod.Deck(path)  # 先開好，不在 state lock 裡等 PDF_LOCK
                result = {"status": "ok", "pages": pages, "unverified": summary.unverified, "path": str(path)}
            except (SourceUnreadable, SummaryError) as e:
                result = {"status": "error", "message": str(e)}
            except Exception:  # 不讓執行緒例外外漏
                result = {"status": "error", "message": "整理失敗，請再試一次"}
            with st.lock:
                if (st.answer_job or {}).get("id") != job_id:
                    result = None  # 已被新的提問、派題或取消取代：存好的檔案留著，不搶投影
                else:
                    # 先換上摘要簡報再清工作槽：畫面不會出現「已完成、但還是舊簡報」的空檔
                    if result["status"] == "ok" and not st.play_summary(path, summary_deck):
                        result = {"status": "error", "message": "無法播放摘要簡報"}
                    st.answer_job = None
                    if result["status"] != "ok":
                        st.summary_note = f"整理重點失敗：{result['message']}"
            eventlog.log("summary", kind=kind, status=result["status"] if result else "superseded",
                         full_text=doc.full_text if doc else None, source_pages=len(doc.pages) if doc else 0,
                         slides=len(summary.slides) if summary else 0, deck_pages=pages,
                         unverified=summary.unverified if summary else 0,
                         seconds=round(time.monotonic() - t0, 1))
            self._publish(job_id, _SUPERSEDED if result is None else result)
            st._changed()

        self._spawn(work)
        return job_id

    def cancel_summary(self, job_id: str | None = None) -> bool:
        """取消整理中的摘要；給 job_id 就只取消那一份（審查 #2：不能取消到別人的）。"""
        st = self.state
        with st.lock:
            job = st.answer_job or {}
            if job.get("kind") != "summary" or (job_id is not None and job.get("id") != job_id):
                return False
            st.answer_job = None
        st._changed()
        return True

    def make_quiz(self, n: int, route: str, ref_names: list[str], focus: str) -> str:
        if not MIN_N <= n <= MAX_N:
            raise ValueError(f"題數需介於 {MIN_N}–{MAX_N}")
        if route not in ROUTES:
            raise ValueError(f"未知的來源：{route}")
        if focus and self._phi_blocked(focus):
            return ""
        if n == 1 and not focus:
            focus = self._auto_focus()
        job_id = uuid.uuid4().hex[:8]
        st = self.state
        with st.lock:
            st.quiz_job = {"id": job_id, "kind": "quiz", "label": f"出 {n} 題", "route": route, "started_at": time.time()}
        st._changed()

        def work() -> None:
            try:
                srcs = self.sources(route, ref_names)
                questions, notes = generate_questions(n, srcs, self.llm, self.config.model_quiz,
                                                      self.config.max_reference_chars, focus)
                result = (questions, notes, srcs)
            except Exception as e:
                result = (None, [f"出題失敗：{e or e.__class__.__name__}"], None)
            with st.lock:
                if (st.quiz_job or {}).get("id") != job_id:
                    self._publish(job_id, _SUPERSEDED)
                    return
                questions, notes, srcs = result
                if questions is not None:
                    st.drafts = questions
                    st.draft_kind = "single" if n == 1 else "exam"
                    self._last_quiz = (srcs, focus)
                st.draft_notes = notes
                st.quiz_job = None
            self._publish(job_id, (len(questions or []), notes))
            st._changed()

        self._spawn(work)
        return job_id

    def regenerate(self, qid: str) -> str:
        """以上次出題的來源重出 1 題，替換同位置的草稿。"""
        if self._last_quiz is None or not any(q.id == qid for q in self.state.drafts):
            raise LookupError("找不到這題草稿")
        srcs, focus = self._last_quiz
        job_id = uuid.uuid4().hex[:8]
        st = self.state
        with st.lock:
            st.quiz_job = {"id": job_id, "kind": "quiz", "label": "重出 1 題", "route": "", "started_at": time.time()}
        st._changed()

        def work() -> None:
            try:
                questions, _ = generate_questions(1, srcs, self.llm, self.config.model_quiz,
                                                  self.config.max_reference_chars, focus)
                note = "" if questions else "重出失敗：沒有通過驗證的題目"
            except Exception as e:
                questions, note = [], f"重出失敗：{e or e.__class__.__name__}"
            with st.lock:
                if (st.quiz_job or {}).get("id") != job_id:
                    return
                if questions:
                    st.drafts = [questions[0] if q.id == qid else q for q in st.drafts]
                if note:
                    st.draft_notes = [*st.draft_notes, note]
                st.quiz_job = None
            st._changed()

        self._spawn(work)
        return job_id


def _answer_route(route: str) -> str:
    return {"page": "deck"}.get(route, route)

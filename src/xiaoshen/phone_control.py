"""手機打字發問與投影控制（spec §6.3a）。

- 打字發問：直接查詢並上投影；送出前本機做識別資訊檢查（JobRunner.ask 命中就不送）。
- 投影控制只有翻頁、播放、結束；其餘指令一律不開放。正在播放時不能改播別份。
- 手機送來的文字一律當成問題，不會被解讀成指令。
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Callable

from . import eventlog, persona

MAX_QUESTION = 300
COOLDOWN_SECONDS = 30       # 同一支手機兩題之間
GLOBAL_ASK_SECONDS = 20     # 全部手機合計兩題之間（換分頁、換 cid 也繞不過，審查 I2）
TEACHER_SECONDS = 15        # 「詢問老師」同一支手機兩題之間
NAV_SECONDS = 0.5           # 翻頁最快間隔（全部手機合計）
PLAY_SECONDS = 3.0          # 播放／結束最快間隔（全部手機合計）
PAUSED = "主持人已暫停手機投影控制"
SEARCH_OFF = "主持人未開放手機搜尋網路"
PHONE_ROUTES = ("openevidence",)  # 「搜尋網路」只查文獻（2026-11-05 修訂）
TODAY = "今天的簡報"


def _msg(status: str, message: str) -> dict:
    return {"status": status, "message": message}


class PhoneControl:
    def __init__(self, state, jobs, clock: Callable[[], float] = time.monotonic):
        self.state = state
        self.jobs = jobs
        self.clock = clock
        self._last_ask: dict[str, float] = {}
        self._last_teacher: dict[str, float] = {}
        self._last_any_ask = -math.inf
        self._last_nav = -math.inf
        self._last_play = -math.inf
        self._last_end = -math.inf

    def _throttled(self, attr: str, seconds: float) -> bool:
        now = self.clock()
        if now - getattr(self, attr) < seconds:
            return True
        setattr(self, attr, now)
        return False

    # ---- 打字發問 ----

    # ---- 詢問老師：只送到主持人畫面（一直開著） ----

    def teacher(self, cid: str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return _msg("error", "請輸入問題")
        if len(text) > MAX_QUESTION:
            return _msg("error", f"問題太長（最多 {MAX_QUESTION} 字）")
        now = self.clock()
        wait = TEACHER_SECONDS - (now - self._last_teacher.get(cid, -math.inf))
        if wait > 0:
            return _msg("busy", f"請 {math.ceil(wait)} 秒後再送")
        self._last_teacher[cid] = now
        self.state.add_teacher_question(text)
        eventlog.log("teacher_question")  # 不記內容、不記是哪支手機
        return _msg("ok", "已送給老師")

    # ---- 搜尋網路（主持人開放才能用，預設關閉） ----

    def ask(self, cid: str, text: str, route: str) -> dict:
        if not self.state.phone_search_enabled:
            return _msg("off", SEARCH_OFF)
        text = (text or "").strip()
        if not text:
            return _msg("error", "請輸入問題")
        if len(text) > MAX_QUESTION:
            return _msg("error", f"問題太長（最多 {MAX_QUESTION} 字）")
        if route not in PHONE_ROUTES:
            return _msg("error", "查詢方式不正確")
        if self.jobs is None:
            return _msg("error", "AI 功能未啟用")
        st = self.state
        now = self.clock()
        wait = max(COOLDOWN_SECONDS - (now - self._last_ask.get(cid, -math.inf)),
                   GLOBAL_ASK_SECONDS - (now - self._last_any_ask))
        if wait > 0:
            return _msg("busy", f"請 {math.ceil(wait)} 秒後再送")
        with st.lock:
            if st.run_is_open():
                return _msg("busy", "作答進行中，暫不開放發問")
            if st.answer_job is not None:
                return _msg("busy", f"{persona.current().display_name}正在查詢，請稍後再送")
            job = self.jobs.ask(text, route, [], spoken=False)
        if not job:
            eventlog.log("phone_ask", status="blocked")
            return _msg("blocked", "疑似含病人識別資訊，沒有送出")
        self._last_ask[cid] = now
        self._last_any_ask = now
        eventlog.log("phone_ask", status="ok", route=route)  # 不記問題內容、不記是哪支手機
        return _msg("ok", "已送出，結果會顯示在投影幕上")

    # ---- 投影控制 ----

    def view(self) -> dict:
        st = self.state
        deck = st.deck
        quiz = st.run_is_open()  # 派題中不給播放選項（播放會收起題目與 QR，審查 I1）
        return {
            "app": persona.app_info(),
            "paused": not st.phone_control_enabled,
            "control_enabled": st.phone_control_enabled,
            "search_enabled": st.phone_search_enabled,
            "quiz_open": quiz,
            "presenting": st.presenting,
            "page": st.page + 1 if deck else 0,
            "page_count": deck.page_count if deck else 0,
            "deck": deck.name if deck else "",
            "temp_kind": st.temp_kind,
            "decks": [] if quiz else [p.stem for p in st.deck_paths],
            "has_oe": not quiz and (st.oe_pending is not None or "oe" in st.last_temp),
            "has_summary": not quiz and "summary" in st.last_temp,
        }

    def nav(self, action: str, page: int | None = None) -> dict:
        st = self.state
        if not st.phone_control_enabled:
            return _msg("paused", PAUSED)
        if not st.presenting or st.deck is None:
            return _msg("error", "目前不在簡報中")
        if self._throttled("_last_nav", NAV_SECONDS):
            return _msg("busy", "翻太快了，請稍等")
        n = st.deck.page_count
        if action == "next":
            st.next()
        elif action == "prev":
            st.prev()
        elif action == "goto":
            if page is None or not 1 <= int(page) <= n:
                return _msg("error", f"沒有第 {page} 頁，這份簡報共 {n} 頁")
            st.goto(int(page) - 1)
        else:
            return _msg("error", "不支援的操作")
        eventlog.log("phone_nav", action=action, page=st.page + 1)
        return _msg("ok", f"第 {st.page + 1} 頁")

    def play(self, kind: str, name: str = "") -> dict:
        st = self.state
        if not st.phone_control_enabled:
            return _msg("paused", PAUSED)
        if st.run_is_open():
            return _msg("refused", "作答進行中，暫不開放播放")
        if st.presenting:
            return _msg("refused", "正在播放簡報，只能翻頁或結束")
        if self._throttled("_last_play", PLAY_SECONDS):
            return _msg("busy", "請稍等幾秒再操作")
        if kind == "deck":
            path = self._deck_path(name)
            if path is None:
                return _msg("not_found", "找不到這份簡報")
            st.play(path)
            ok = st.presenting
        elif kind == "oe":
            if st.oe_pending is not None:
                ok = bool(self.jobs and self.jobs.play_pending_oe())
            elif "oe" in st.last_temp:
                ok = st.play_summary(st.last_temp["oe"], kind="oe")
            else:
                return _msg("not_found", "還沒有 OE 簡報")
        elif kind == "summary":
            if "summary" not in st.last_temp:
                return _msg("not_found", "還沒有摘要簡報")
            ok = st.play_summary(st.last_temp["summary"])
        else:
            return _msg("error", "不支援的操作")
        eventlog.log("phone_play", kind=kind, status="ok" if ok else "error")
        return _msg("ok", "開始播放") if ok else _msg("error", "開不起來")

    def end(self) -> dict:
        st = self.state
        if not st.phone_control_enabled:
            return _msg("paused", PAUSED)
        if not st.presenting:
            return _msg("error", "目前不在簡報中")
        if self._throttled("_last_end", PLAY_SECONDS):
            return _msg("busy", "請稍等幾秒再操作")
        st.stop_presenting()
        eventlog.log("phone_end")
        return _msg("ok", "已結束簡報")

    def _deck_path(self, name: str) -> Path | None:
        st = self.state
        name = (name or "").strip()
        if name == TODAY and st.answer_deck is not None:
            return Path(st.answer_deck.path)
        return next((p for p in st.deck_paths if p.stem == name), None)

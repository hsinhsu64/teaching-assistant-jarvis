"""語音狀態機：待命（只在本機聽喚醒詞）→ 叫醒（串流到 Live）→ 一問一答＋追問 → 回待命。

- 待命／離線：麥克風只進本機喚醒詞偵測；喚醒詞叫醒、「下一頁」直接翻頁。音訊不出本機。
- 一問一答＋追問：叫醒後 idle_seconds 內要開始問；助理答完後 followup_seconds 內可追問，
  沒人追問就回待命。旁人持續講話不會延長：送上雲端的收音總長不超過 max_listen_seconds。
- 只有「聆聽中」才送麥克風音訊；查詢中、說話中都不送（半雙工＋不把後續報告送上雲端）。
- 對話中的「下一頁」由 Live 呼叫 next_page 處理，本機不重複翻頁。
- Live 連不上或斷線 → 「語音離線」，offline_seconds 後回待命；翻頁照常。
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterable, Callable

import numpy as np

from . import eventlog
from .audio_io import BLOCK, MIC_RATE, to_float
from .live_backend import LiveBackend
from .phi import find_phi
from .zh import to_traditional
from . import persona
from .voice_tools import TOOLS, system_instruction
from .wakeword import NEXT, PREV

ACTIVE = ("listening", "thinking", "speaking")
PREROLL_BLOCKS = 50  # 連線中最多先存 5 秒
BLOCK_SECONDS = BLOCK / MIC_RATE
PAGE_TOOLS = {"next_page", "prev_page", "goto_page"}  # 翻頁成功不出聲
SILENT_STATUSES = {"ok", "already_done"}  # already_done：沒有新的話又要翻頁，不翻也不出聲
SETTLE_TOOLS = {"ask", "make_quiz"}  # 這些工具要先等逐字稿到齊再做識別資訊檢查
# 本機判斷「已開始說」：雲端逐字稿要講完才回來（實測 11.5 秒的問題，15.1 秒才收到），
# 不能靠它判斷，否則講超過 8 秒就被當成沒人說話而回待命。連續 3 塊（0.3 秒）夠大聲才算，咳嗽一聲不算。
SPEECH_BLOCKS = 3


class VoiceSession:
    def __init__(self, state, detector, backend: LiveBackend, tools, mic: AsyncIterable[bytes], speaker,
                 idle_seconds: float = 8.0, clock: Callable[[], float] = time.monotonic,
                 offline_seconds: float = 30.0, followup_seconds: float = 6.0,
                 max_listen_seconds: float = 60.0, settle_seconds: float = 0.8,
                 context: Callable[[], str] | None = None, speech_rms: float = 0.008):
        self.state = state
        self.detector = detector
        self.backend = backend
        self.tools = tools
        self.mic = mic
        self.speaker = speaker
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.offline_seconds = offline_seconds
        self.followup_seconds = followup_seconds
        self.max_listen_seconds = max_listen_seconds
        self.settle_seconds = settle_seconds
        self.speech_rms = speech_rms       # 麥克風音量超過這個值算有人在說話（安靜會議室實測約 0.001～0.003）
        self._loud = 0                     # 連續夠大聲的區塊數
        self.context = context             # 叫醒時附加到系統提示的內容（今天簡報的詞彙）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._tool_task: asyncio.Task | None = None
        self._live = None
        self._sid = 0                      # 每次叫醒一個編號；舊 session 的回呼不得改到新狀態
        self._wake_requested = False
        self._stop_requested = False
        self._mute_reply = False           # 只翻頁成功時，這一輪的回話不播（翻頁不出聲）
        # 回話裡唸到自己的名字（喚醒詞）時，喇叭的聲音會被本機喚醒詞聽到而自己打斷自己（實機 2026-11-19）：
        # 這段話播完之前不因喚醒詞中斷
        self._said_name = False
        self._turn_output = ""
        self._turn_tools: set[str] = set() # 這一輪呼叫過的工具：有翻頁以外的就不靜音
        self._window_until = 0.0          # 這個時間前要開始說（問題或追問），否則回待命
        self._in_turn = False              # 使用者已開始說這一題
        self._listened = 0.0               # 這次叫醒已送上雲端的收音秒數
        self._offline_until = 0.0
        self._preroll: list[bytes] = []    # 叫醒到連上 Live 之間說的話（一口氣說「（喚醒詞），請收卷」）

    # ---- 外部呼叫（任何執行緒） ----

    def wake(self) -> None:
        self._call(lambda: setattr(self, "_wake_requested", True))

    def stop(self) -> None:
        self._call(lambda: setattr(self, "_stop_requested", True))

    def _call(self, fn: Callable[[], None]) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop:
            fn()
        else:
            loop.call_soon_threadsafe(fn)

    # ---- 主迴圈 ----

    def _status(self) -> str:
        return self.state.voice_status

    def _set_status(self, status: str) -> None:
        if self.state.voice_status != status:
            self.state.voice_status = status
            self.state._changed()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._set_status("standby")
        try:
            async for block in self.mic:
                await self._on_block(block)
        finally:
            self._cancel_tasks()

    async def _on_block(self, block: bytes) -> None:
        status = self._status()
        if self._stop_requested:
            self._stop_requested = False
            self._wake_requested = False
            if status in ACTIVE:
                self._end_session("stop")
                return
        if status == "offline" and self.clock() >= self._offline_until:
            self._set_status("standby")
            status = "standby"
        if self._wake_requested:
            self._wake_requested = False
            if status == "standby":
                eventlog.log("wake", source="remote")
                self._start_session(block)  # 沒有喚醒詞：這一塊可能已是問題開頭
                return
        if status in ACTIVE:
            await self._active_block(block)
            return
        for hit in self.detector.feed(to_float(block)):
            if hit in (NEXT, PREV):
                if self.state.presenting:  # 主畫面上說「下一頁」「上一頁」不動作（§6.0）
                    before = self.state.page + 1
                    self.state.next() if hit == NEXT else self.state.prev()
                    eventlog.log("page", source="kws", word=hit, before=before, after=self.state.page + 1)
            elif hit == self.detector.wake_label and status == "standby":
                eventlog.log("wake", source="kws")
                # 含喚醒詞的這一塊不送雲端：實機雲端把喚醒詞聽成 ciao，回 Hello
                self._start_session()
                return

    def _open_window(self, seconds: float) -> None:
        self._in_turn = False
        self._loud = 0
        self._window_until = self.clock() + seconds

    def _heard_speech(self, block: bytes) -> bool:
        x = to_float(block)
        loud = x.size > 0 and float(np.sqrt(np.mean(x * x))) >= self.speech_rms
        self._loud = self._loud + 1 if loud else 0
        return self._loud >= SPEECH_BLOCKS

    async def _active_block(self, block: bytes) -> None:
        playing = self.speaker.is_playing()
        status = self._status()
        if status == "speaking" and not playing:
            self._said_name = False  # 播完了：恢復可用喚醒詞插話
            self._set_status("listening")
            self._open_window(self.followup_seconds)
            status = "listening"
        if status == "speaking":
            # 說話中麥克風不送雲端，但本機繼續聽喚醒詞：聽到就中斷
            if self.detector.wake_label in self.detector.feed(to_float(block)) and not self._said_name:
                self._barge_in()
            return
        if status == "listening" and not self._in_turn and self._heard_speech(block):
            self._in_turn = True  # 已開始說：不因等待時間到而結束（逐字稿講完才會到）
        if status == "listening" and not self._in_turn and self.clock() >= self._window_until:
            self._end_session("idle")
            return
        if self._live is None:
            if len(self._preroll) < PREROLL_BLOCKS:
                self._preroll.append(block)
            return
        if status != "listening" or playing:
            return  # 查詢中、說話中不送
        if self._listened >= self.max_listen_seconds:
            self._end_session("max_listen")
            return
        try:
            await self._live.send_audio(block)
            self._listened += BLOCK_SECONDS
        except Exception:
            self._go_offline()

    def _barge_in(self) -> None:
        """說話中被喚醒詞叫到：停播、這一輪剩下的回話都不播，改聽新的問題。"""
        eventlog.log("barge_in", source="kws")
        self.speaker.stop()
        self.detector.reset()
        self._mute_reply = True  # 到這一輪結束（turn_complete／interrupted）為止
        self.state.close_transcript_line()
        self._set_status("listening")
        self._open_window(self.followup_seconds)

    # ---- 對話 ----

    def _start_session(self, block: bytes | None = None) -> None:
        self._sid += 1
        self._preroll = [block] if block else []
        self._listened = 0.0
        self._set_status("listening")
        self._open_window(self.idle_seconds)
        self.tools.begin_turn()
        self._task = asyncio.create_task(self._converse(self._sid))

    def _cancel_tasks(self) -> None:
        for t in (self._task, self._tool_task):
            if t and not t.done():
                t.cancel()
        self._task = None
        self._tool_task = None

    def _end_session(self, reason: str = "other") -> None:
        if self._status() in ACTIVE:
            eventlog.log("session_end", reason=reason)  # 查「怎麼突然回待命」用
        self._sid += 1
        self._mute_reply = False
        self._turn_tools = set()
        self.state.pending_switch = None  # 換片確認只在同一次對話內有效
        self.speaker.stop()
        self._cancel_tasks()
        self._live = None
        self._preroll = []
        self.detector.reset()
        self._set_status("standby")

    def _go_offline(self) -> None:
        eventlog.log("session_end", reason="offline")
        self._sid += 1
        self.state.pending_switch = None
        if self.state.voice_cloud_ok:
            self.state.voice_cloud_ok = False
            self.state._changed()
        self._cancel_tasks()
        self._live = None
        self._preroll = []
        self.speaker.stop()
        self._offline_until = self.clock() + self.offline_seconds
        self.detector.reset()
        self._set_status("offline")

    def _system_prompt(self) -> str:
        try:
            return system_instruction() + (self.context() if self.context else "")
        except Exception:
            return system_instruction()  # 詞彙只是輔助，出錯就不用

    async def _converse(self, sid: int) -> None:
        try:
            async with self.backend.connect(self._system_prompt(), TOOLS) as live:
                if sid != self._sid:
                    return
                if not self.state.voice_cloud_ok:
                    self.state.voice_cloud_ok = True
                    self.state._changed()
                while self._preroll:  # 先補送連線期間說的話
                    await live.send_audio(self._preroll.pop(0))
                    self._listened += BLOCK_SECONDS
                self._live = live
                async for ev in live.events():
                    if sid != self._sid:
                        return
                    await self._on_event(live, ev, sid)
                    if ev.kind == "closed":
                        break
            if sid == self._sid and self._status() in ACTIVE:
                self._end_session("closed")  # 雲端結束了這次對話
        except asyncio.CancelledError:
            raise
        except Exception:
            if sid == self._sid:
                self._go_offline()

    async def _on_event(self, live, ev, sid: int) -> None:
        if ev.kind == "audio":
            if self._mute_reply:
                return  # 翻頁成功不出聲
            self.speaker.play(ev.data)
            self._set_status("speaking")
        elif ev.kind == "input_text":
            text = to_traditional(ev.text)  # 逐字稿常是簡體：顯示與識別資訊檢查都用繁體
            self.tools.hear(text)
            self.state.add_transcript("你", text)
            if self._status() == "listening":
                self._in_turn = True
            if find_phi(self.tools.turn_text) and not self.state.voice_phi_alert:
                self.state.voice_phi_alert = True
                self.state._changed()
        elif ev.kind == "output_text":
            self._turn_output += to_traditional(ev.text)
            label = getattr(self.detector, "wake_label", "")
            if label and label in self._turn_output:
                self._said_name = True
            if not self._mute_reply:
                self.state.add_transcript(persona.current().display_name, ev.text)
        elif ev.kind == "tool_call":
            self._turn_tools |= {c.name for c in ev.calls}
            self._mute_reply = False  # 同一輪又呼叫別的工具（例如翻頁後查詢）：回答照常出聲
            self.state.close_transcript_line()
            self._set_status("thinking")
            # 另開 task：等工具時事件迴圈繼續收逐字稿（逐字稿可能比工具呼叫晚到）
            self._tool_task = asyncio.create_task(self._run_tools(live, ev.calls, sid))
        elif ev.kind == "interrupted":
            self._mute_reply = False
            self._said_name = False
            self._turn_output = ""
            self._turn_tools = set()
            self.speaker.stop()
        elif ev.kind == "turn_complete":
            self._turn_output = ""  # _said_name 留到這段話播完
            self._mute_reply = False
            self._turn_tools = set()
            self.state.end_transcript_turn()
            self.tools.begin_turn()
            if not self.speaker.is_playing() and self._status() != "thinking":
                self._set_status("listening")
                self._open_window(self.followup_seconds)

    async def _run_tools(self, live, calls, sid: int) -> None:
        if self.settle_seconds and any(c.name in SETTLE_TOOLS for c in calls):
            await asyncio.sleep(self.settle_seconds)
        results = []
        for call in calls:
            results.append((call, await asyncio.to_thread(self.tools.handle, call)))
        if sid != self._sid:
            return
        # 只翻頁且成功：接下來這輪的回話不播、不上逐字稿（翻頁不出聲）
        # 晚完成的翻頁工具不能把同一輪後來的查詢回答也靜音：看整輪呼叫過的工具
        self._mute_reply = (self._turn_tools <= PAGE_TOOLS
                            and all(r.get("status") in SILENT_STATUSES for _, r in results))
        try:
            await live.send_tool_results(results)
        except Exception:
            self._go_offline()
            return
        if self._status() == "thinking" and not self.speaker.is_playing():
            self._set_status("listening")
            self._open_window(self.followup_seconds)
            self._in_turn = True  # 助理接著會說答案，不因這段空檔結束

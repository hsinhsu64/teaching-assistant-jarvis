import asyncio

import pytest

from xiaoshen.live_backend import LiveEvent, ToolCall
from xiaoshen.voice import VoiceSession

from fake_live import FakeLiveBackend

BLOCK = b"\x01\x00" * 1600  # 100 ms


class FakeDetector:
    wake_label = "小森"

    def __init__(self):
        self.pending: list[str] = []
        self.feeds = 0
        self.resets = 0

    def feed(self, samples):
        self.feeds += 1
        hits, self.pending = self.pending, []
        return hits

    def reset(self):
        self.resets += 1


class FakeMic:
    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.q.get()


class FakeSpeaker:
    def __init__(self):
        self.played = 0
        self.playing = False
        self.stops = 0

    def play(self, pcm):
        self.played += len(pcm)
        self.playing = True

    def is_playing(self):
        return self.playing

    def stop(self):
        self.stops += 1
        self.playing = False


class FakeTools:
    def __init__(self, state):
        self.state = state
        self.turn_text = ""
        self.calls = []
        self.begins = 0

    def begin_turn(self):
        self.begins += 1
        self.turn_text = ""

    def hear(self, text):
        self.turn_text += text

    def handle(self, call):
        self.seen_text = self.turn_text
        if getattr(self, "gate", None) is not None:
            self.gate.wait(5)
        self.calls.append(call.name)
        if call.name == "next_page":
            self.state.next()
        return dict(getattr(self, "result", None) or {"status": "ok"})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Rig:
    def __init__(self, state, backend):
        self.state = state
        self.backend = backend
        self.detector = FakeDetector()
        self.mic = FakeMic()
        self.speaker = FakeSpeaker()
        self.tools = FakeTools(state)
        self.clock = Clock()
        self.session = VoiceSession(state, self.detector, backend, self.tools, self.mic, self.speaker,
                                    idle_seconds=8, clock=self.clock, offline_seconds=30,
                                    followup_seconds=6, max_listen_seconds=60, settle_seconds=0)
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.session.run())
        await settle()

    async def push(self, n=1):
        for _ in range(n):
            await self.mic.q.put(BLOCK)
            await settle()

    async def close(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass


async def settle(rounds=20):
    for _ in range(rounds):
        await asyncio.sleep(0)


async def wait_until(cond, timeout=3.0):
    """工具在另一個執行緒跑：等到條件成立（例如這一輪結束）才檢查，避免沒等到回話就誤判。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "等不到條件成立"
        await asyncio.sleep(0.01)


@pytest.fixture
def run():
    def _run(coro):
        return asyncio.run(coro)
    return _run


def test_standby_sends_nothing_to_cloud(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        assert state.voice_status == "standby"
        await rig.push(10)
        assert rig.backend.connects == 0 and rig.backend.audio_bytes == 0
        assert rig.detector.feeds == 10
        await rig.close()
    run(go())


def test_wake_word_opens_session_and_streams(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        assert rig.backend.connects == 1 and state.voice_status == "listening"
        await rig.push(3)
        assert rig.backend.audio_bytes == 3 * len(BLOCK)  # 不含叫醒那一塊（雲端常把喚醒詞聽錯，例如聽成 ciao）
        assert "不得用自己的知識" in rig.backend.systems[0]
        await rig.close()
    run(go())


def test_local_next_page_in_standby(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["下一頁"]
        await rig.push()
        assert state.page == 1 and rig.backend.connects == 0
        await rig.close()
    run(go())


def test_local_prev_page_in_standby(state, run):
    # 「上一頁」和「下一頁」一樣，不叫喚醒詞也能在本機翻
    async def go():
        state.goto(2)
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["上一頁"]
        await rig.push()
        assert state.page == 1 and rig.backend.connects == 0
        await rig.close()
    run(go())


def test_local_page_turn_is_logged(state, run, tmp_path):
    import json

    from xiaoshen import eventlog

    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["下一頁"]
        await rig.push()
        await rig.close()
    eventlog.configure(tmp_path)
    try:
        run(go())
    finally:
        eventlog.configure(None)
    rows = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    assert {"event": "page", "source": "kws", "word": "下一頁", "before": 1, "after": 2}.items() <= rows[-1].items()


def test_local_prev_page_ignored_on_hud(state, run):
    async def go():
        state.goto(2)
        state.stop_presenting()
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["上一頁"]
        await rig.push()
        assert state.page == 2
        await rig.close()
    run(go())


def test_local_next_page_ignored_on_hud(state, run):
    async def go():
        state.stop_presenting()
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["下一頁"]
        await rig.push()
        assert state.page == 0
        await rig.close()
    run(go())


def test_next_page_keyword_ignored_while_active(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        feeds = rig.detector.feeds
        rig.detector.pending = ["下一頁"]
        await rig.push(2)
        assert state.page == 0 and rig.detector.feeds == feeds
        await rig.close()
    run(go())


def test_no_wake_or_page_while_speaking(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend([LiveEvent("audio", data=b"\x00" * 4800)]))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert rig.speaker.played == 4800 and state.voice_status == "speaking"
        sent = rig.backend.audio_bytes
        await rig.push(3)
        assert rig.backend.audio_bytes == sent  # 說話中不送麥克風
        rig.speaker.playing = False
        await rig.push()
        assert state.voice_status == "listening" and rig.backend.audio_bytes == sent + len(BLOCK)
        await rig.close()
    run(go())


def test_saying_wake_word_interrupts_reply(state, run):
    # 助理說話時說喚醒詞就中斷，接著聽新的問題（本機辨識，不送雲端）
    async def go():
        script = [LiveEvent("audio", data=b"A" * 4800), 0.2, LiveEvent("audio", data=b"B" * 4800),
                  LiveEvent("output_text", text="後半段"), LiveEvent("turn_complete")]
        rig = Rig(state, FakeLiveBackend(script))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert state.voice_status == "speaking" and rig.speaker.played == 4800
        stops, resets = rig.speaker.stops, rig.detector.resets
        rig.detector.pending = ["小森"]
        await rig.push()
        assert rig.speaker.stops == stops + 1 and rig.detector.resets == resets + 1
        assert state.voice_status == "listening"
        await asyncio.sleep(0.3)
        await settle()
        assert rig.speaker.played == 4800  # 被打斷那一輪剩下的聲音不播
        assert not any("後半段" in l for l in state.voice_transcript)
        sent = rig.backend.audio_bytes
        await rig.push()
        assert rig.backend.audio_bytes == sent + len(BLOCK)  # 接著把新的問題送上雲端
        await rig.close()
    run(go())


def test_page_words_ignored_while_speaking(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend([LiveEvent("audio", data=b"A" * 4800)]))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        stops = rig.speaker.stops
        rig.detector.pending = ["下一頁"]
        await rig.push()
        assert state.page == 0 and rig.speaker.stops == stops and state.voice_status == "speaking"
        await rig.close()
    run(go())


def test_tool_call_roundtrip_and_transcript(state, run):
    from xiaoshen.persona import Persona, set_persona
    set_persona(Persona("小森"))
    async def go():
        script = [LiveEvent("input_text", text="小森，"), LiveEvent("input_text", text="下一頁"),
                  LiveEvent("tool_call", calls=[ToolCall("c1", "next_page", {})]),
                  LiveEvent("output_text", text="好的"), LiveEvent("turn_complete")]
        rig = Rig(state, FakeLiveBackend(script))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle(60)
        assert rig.tools.calls == ["next_page"] and state.page == 1
        assert rig.backend.tool_results[0][1] == {"status": "ok"}
        assert state.voice_transcript[-2:] == ["你：小森，下一頁", "小森：好的"]
        assert rig.tools.begins >= 2  # 開始時一次、回合結束一次
        await rig.close()
    run(go())


def test_next_page_reply_is_silent(state, run):
    # 翻頁成功時助理不要出聲（以免干擾講者）
    async def go():
        script = [LiveEvent("input_text", text="下一頁"),
                  LiveEvent("tool_call", calls=[ToolCall("c1", "next_page", {})]), "wait_results",
                  LiveEvent("audio", data=b"\x01\x00" * 100), LiveEvent("output_text", text="已翻到第 2 頁"),
                  LiveEvent("turn_complete")]
        rig = Rig(state, FakeLiveBackend(script))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await wait_until(lambda: rig.tools.begins >= 2)  # 這一輪已結束
        await settle()
        assert state.page == 1 and rig.speaker.played == 0
        assert not any(l.startswith("小森：") for l in state.voice_transcript)
        assert state.voice_status == "listening"  # 仍可接著追問
        await rig.close()
    run(go())


@pytest.mark.parametrize("name,args", [("prev_page", {}), ("goto_page", {"page": 3})])
def test_prev_and_goto_reply_is_silent(state, run, name, args):
    # 上一頁、跳頁成功也不出聲
    async def go():
        script = [LiveEvent("input_text", text="翻頁"), LiveEvent("tool_call", calls=[ToolCall("c1", name, args)]),
                  "wait_results", LiveEvent("audio", data=b"\x01\x00" * 100), LiveEvent("output_text", text="好"),
                  LiveEvent("turn_complete")]
        rig = Rig(state, FakeLiveBackend(script))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await wait_until(lambda: rig.tools.begins >= 2)  # 這一輪已結束
        await settle()
        assert rig.speaker.played == 0
        await rig.close()
    run(go())


def test_refused_repeat_page_turn_is_silent(state, run):
    # 助理自己重複呼叫翻頁（沒有新的話）：不翻、也不出聲
    async def go():
        script = [LiveEvent("input_text", text="下一頁"),
                  LiveEvent("tool_call", calls=[ToolCall("c1", "next_page", {})]), "wait_results",
                  LiveEvent("audio", data=b"AUDIO" * 40), LiveEvent("turn_complete")]
        rig = Rig(state, FakeLiveBackend(script))
        rig.tools.result = {"status": "already_done", "message": ""}
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await wait_until(lambda: rig.tools.begins >= 2)
        await settle()
        assert rig.speaker.played == 0
        await rig.close()
    run(go())


def test_reply_after_other_tool_in_same_turn_is_spoken(state, run):
    from xiaoshen.persona import Persona, set_persona
    set_persona(Persona("小森"))
    # 「下一頁，然後說明這頁」：翻頁後又查詢，回答照常出聲
    async def go():
        script = [LiveEvent("input_text", text="下一頁然後說明"),
                  LiveEvent("tool_call", calls=[ToolCall("c1", "next_page", {})]), "wait_results",
                  LiveEvent("tool_call", calls=[ToolCall("c2", "ask", {"question": "q"})]), "wait_results",
                  LiveEvent("audio", data=b"\x01\x00" * 100), LiveEvent("output_text", text="說明"),
                  LiveEvent("turn_complete")]
        rig = Rig(state, FakeLiveBackend(script))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await wait_until(lambda: rig.tools.begins >= 2)  # 這一輪已結束
        await settle()
        assert rig.speaker.played == 200 and state.voice_transcript[-1] == "小森：說明"
        await rig.close()
    run(go())


def test_input_text_feeds_phi_turn_text(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend([LiveEvent("input_text", text="病歷號 1234567")]))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert rig.tools.turn_text == "病歷號 1234567"
        await rig.close()
    run(go())


def test_user_speech_shown_in_traditional_chinese(state, run):
    # 實機：Gemini 把使用者的話轉成簡體，主畫面出現「播放简报」，所以加裝簡轉繁
    async def go():
        script = [LiveEvent("input_text", text="播放酸碱电解质平衡"), LiveEvent("input_text", text="简报")]
        rig = Rig(state, FakeLiveBackend(script))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert state.voice_transcript[-1] == "你：播放酸鹼電解質平衡簡報"
        assert rig.tools.turn_text == "播放酸鹼電解質平衡簡報"
        await rig.close()
    run(go())


def test_idle_returns_to_standby(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        assert state.voice_status == "listening"
        rig.clock.t += 9
        await rig.push()
        await settle()
        assert state.voice_status == "standby" and rig.detector.resets >= 1
        await rig.push()
        assert rig.backend.connects == 1  # 回待命後不再送音
        await rig.close()
    run(go())


def test_backend_error_returns_to_standby(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend(fail=ConnectionError("network down")))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert state.voice_status == "offline"
        rig.detector.pending = ["下一頁"]
        await rig.push()
        assert state.page == 1  # 離線時本機「下一頁」仍有效
        rig.clock.t += 31
        await rig.push()
        assert state.voice_status == "standby"
        await rig.close()
    run(go())


def test_external_wake_and_stop(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend([LiveEvent("audio", data=b"\x00" * 100)]))
        await rig.start()
        rig.session.wake()
        await rig.push()
        await settle()
        assert rig.backend.connects == 1
        rig.session.wake()  # 已啟動時忽略
        await rig.push()
        assert rig.backend.connects == 1
        rig.session.stop()
        await rig.push()
        await settle()
        assert state.voice_status == "standby" and rig.speaker.stops >= 1
        await rig.close()
    run(go())


def test_remote_wake_keeps_first_block(state, run):
    # 遙控器／手機叫醒沒有喚醒詞：按下後那一塊可能已經是問題的開頭，照送
    async def go():
        rig = Rig(state, FakeLiveBackend(connect_delay=0.05))
        await rig.start()
        rig.session.wake()
        await rig.push()
        await asyncio.sleep(0.1)
        await settle()
        assert rig.backend.audio_bytes == len(BLOCK)
        await rig.close()
    run(go())


def test_speech_during_connect_is_buffered_not_lost(state, run):
    """「小森，請收卷」一口氣說完：連線中說的話要先存起來，連上後補送（實測發現會漏掉）。"""
    async def go():
        rig = Rig(state, FakeLiveBackend(connect_delay=0.05))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()          # 偵測到喚醒詞的那一塊：不送雲端（實機雲端曾把喚醒詞聽成 ciao，回 Hello）
        await rig.push(3)         # 連線中繼續說話
        assert rig.backend.audio_bytes == 0
        await asyncio.sleep(0.1)  # 連上
        await settle()
        assert rig.backend.audio_bytes == 3 * len(BLOCK)
        await rig.push()
        assert rig.backend.audio_bytes == 4 * len(BLOCK)
        await rig.close()
    run(go())



# ---- 最終審查修正：一問一答＋追問、查詢中不送音、孤兒 session ----

def wake_rig(state, script, **kw):
    rig = Rig(state, FakeLiveBackend(script, **kw))
    return rig


async def woke(rig):
    await rig.start()
    rig.detector.pending = ["小森"]
    await rig.push()
    await settle()


def test_followup_window_ends_session_after_answer(state, run):
    async def go():
        rig = wake_rig(state, [LiveEvent("input_text", text="問題"), LiveEvent("turn_complete")])
        await woke(rig)
        assert state.voice_status == "listening"
        rig.clock.t += 5
        await rig.push()
        assert state.voice_status == "listening"  # 追問時間 6 秒內
        rig.clock.t += 2
        await rig.push()
        assert state.voice_status == "standby"
        await rig.close()
    run(go())


def test_followup_question_within_window_continues(state, run):
    async def go():
        rig = wake_rig(state, [LiveEvent("input_text", text="問題"), LiveEvent("turn_complete"), 0.01,
                               LiveEvent("input_text", text="追問")])
        await woke(rig)
        rig.clock.t += 3
        await asyncio.sleep(0.05)
        await settle()
        rig.clock.t += 10
        await rig.push()
        assert state.voice_status == "listening"  # 追問進行中，不因靜默結束
        await rig.close()
    run(go())


def test_background_talk_capped_by_max_listen(state, run):
    async def go():
        script = [LiveEvent("input_text", text="病例報告繼續講…")]
        rig = wake_rig(state, script)
        await woke(rig)
        for _ in range(590):
            await rig.mic.q.put(BLOCK)
        await settle(3000)
        assert state.voice_status == "listening"
        await rig.push(15)
        assert state.voice_status == "standby"
        assert rig.backend.audio_bytes <= 600 * len(BLOCK)
        await rig.close()
    run(go())


def test_session_end_reason_is_logged(state, run, tmp_path):
    import json

    from xiaoshen import eventlog

    async def go():
        rig = wake_rig(state, [LiveEvent("input_text", text="病例報告繼續講…")])
        await woke(rig)
        for _ in range(620):
            await rig.mic.q.put(BLOCK)
        await settle(3000)
        await rig.close()
    eventlog.configure(tmp_path)
    try:
        run(go())
    finally:
        eventlog.configure(None)
    rows = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    assert {"event": "session_end", "reason": "max_listen"}.items() <= [r for r in rows if r["event"] == "session_end"][0].items()


def test_idle_end_reason_is_logged(state, run, tmp_path):
    import json

    from xiaoshen import eventlog

    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        rig.clock.t += 9
        await rig.push()
        assert state.voice_status == "standby"
        await rig.close()
    eventlog.configure(tmp_path)
    try:
        run(go())
    finally:
        eventlog.configure(None)
    rows = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    assert [r["reason"] for r in rows if r["event"] == "session_end"] == ["idle"]


LOUD = (3000).to_bytes(2, "little", signed=True) * 1600  # 100 ms 有人說話（RMS 約 0.09）


def test_long_question_not_cut_off_before_transcript_arrives(state, run):
    # 實機：講不到 10 秒就回待命。量測：雲端的逐字稿要到講完（再停 2 秒）才傳回，
    # 叫醒後 8 秒內沒收到逐字稿就被當成沒人說話。改用本機音量判斷「已開始說」（使用者 2026-11-05 回報）
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        for _ in range(5):            # 開始說話（還沒有任何逐字稿）
            await rig.mic.q.put(LOUD)
            await settle()
        rig.clock.t += 9              # 超過 8 秒的等待時間，還在講
        await rig.mic.q.put(LOUD)
        await settle()
        assert state.voice_status == "listening"
        await rig.close()
    run(go())


def test_single_noise_blip_does_not_count_as_speech(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await rig.mic.q.put(LOUD)     # 一聲咳嗽、關門
        await settle()
        await rig.push(2)
        rig.clock.t += 9
        await rig.push()
        assert state.voice_status == "standby"
        await rig.close()
    run(go())


def test_no_audio_sent_while_thinking(state, run):
    async def go():
        import threading as th
        rig = wake_rig(state, [LiveEvent("tool_call", calls=[ToolCall("c1", "ask", {"question": "q"})])])
        rig.tools.gate = th.Event()
        await woke(rig)
        await asyncio.sleep(0.05)
        assert state.voice_status == "thinking"
        sent = rig.backend.audio_bytes
        await rig.push(5)
        assert rig.backend.audio_bytes == sent
        rig.tools.gate.set()
        await asyncio.sleep(0.05)
        await rig.close()
    run(go())


def test_phi_alert_from_transcript_without_tool(state, run):
    async def go():
        rig = wake_rig(state, [LiveEvent("input_text", text="病歷號 1234567 那位")])
        await woke(rig)
        assert state.voice_phi_alert is True
        await rig.close()
    run(go())


def test_tool_waits_for_late_transcript(state, run):
    async def go():
        rig = wake_rig(state, [LiveEvent("tool_call", calls=[ToolCall("c1", "ask", {"question": "怎麼用藥"})]),
                               LiveEvent("input_text", text="病歷號 1234567 的病人")])
        rig.session.settle_seconds = 0.1
        await woke(rig)
        await asyncio.sleep(0.3)
        await settle()
        assert "1234567" in rig.tools.seen_text  # 工具執行前已等到逐字稿
        await rig.close()
    run(go())


def test_send_failure_cancels_session(state, run):
    async def go():
        class Boom(FakeLiveBackend):
            pass
        rig = wake_rig(state, [5.0, LiveEvent("tool_call", calls=[ToolCall("c1", "next_page", {})])])

        async def bad_send(pcm):
            raise ConnectionError("gone")
        await woke(rig)
        rig.session._live.send_audio = bad_send
        await rig.push()
        assert state.voice_status == "offline"
        assert rig.session._task is None or rig.session._task.cancelled() or rig.session._task.done()
        await rig.close()
    run(go())


def test_session_system_prompt_includes_deck_vocabulary(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        rig.session.context = lambda: "\n七、今天簡報的專有名詞：Bartter、Gitelman。"
        await woke(rig)
        assert "Bartter、Gitelman" in rig.backend.systems[0] and "不得用自己的知識" in rig.backend.systems[0]
        await rig.close()
    run(go())


def test_broken_context_falls_back_to_base_prompt(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())

        def boom():
            raise RuntimeError("deck closed")
        rig.session.context = boom
        await woke(rig)
        assert rig.backend.connects == 1 and "不得用自己的知識" in rig.backend.systems[0]
        assert state.voice_status == "listening"
        await rig.close()
    run(go())


def test_cloud_failure_keeps_voice_down_until_reconnect(state, run):
    async def go():
        backend = FakeLiveBackend(fail=ConnectionError("network down"))
        rig = Rig(state, backend)
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        rig.clock.t += 31
        await rig.push()
        assert state.voice_status == "standby" and state.voice_down() is True
        backend.fail = None
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert state.voice_down() is False
        await rig.close()
    run(go())


def test_pending_switch_cleared_when_session_ends(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        state.pending_switch = "B"
        state.deck_candidates = []
        rig.session.stop()
        await rig.push()
        assert state.voice_status == "standby" and state.pending_switch is None
        await rig.close()
    run(go())


def test_pending_switch_cleared_when_offline(state, run):
    async def go():
        state.pending_switch = "B"
        rig = Rig(state, FakeLiveBackend(fail=ConnectionError("network down")))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert state.voice_status == "offline" and state.pending_switch is None
        await rig.close()
    run(go())


def test_speech_threshold_configurable(tmp_path):
    from xiaoshen.config import Config, load_config

    assert Config().voice_speech_rms == 0.008
    p = tmp_path / "config.toml"
    p.write_text("voice_speech_rms = 0.02\n", encoding="utf-8")
    assert load_config(p).voice_speech_rms == 0.02


def test_voice_uses_detector_label(state, run):
    async def go():
        rig = Rig(state, FakeLiveBackend())
        rig.detector.wake_label = "賈維斯"
        await rig.start()
        rig.detector.pending = ["小森"]  # 不是目前的喚醒詞：不應啟動
        await rig.push()
        assert rig.backend.connects == 0 and state.voice_status == "standby"
        rig.detector.pending = ["賈維斯"]
        await rig.push()
        assert rig.backend.connects == 1 and state.voice_status == "listening"
        await rig.close()
    run(go())


def test_own_name_in_reply_does_not_interrupt(state, run):
    # 實機 2026-11-19：介紹自己時唸到「我是賈維斯」，喇叭的聲音被本機喚醒詞聽到 → 自己打斷自己。
    # 這一輪回話裡有喚醒詞時，播完之前不因喚醒詞中斷；播完後恢復可插話。
    async def go():
        script = [LiveEvent("output_text", text="我是小"), LiveEvent("output_text", text="森，可以叫我"),
                  LiveEvent("audio", data=b"A" * 4800), 0.2, LiveEvent("audio", data=b"B" * 4800),
                  LiveEvent("turn_complete")]
        rig = Rig(state, FakeLiveBackend(script))
        await rig.start()
        rig.detector.pending = ["小森"]
        await rig.push()
        await settle()
        assert state.voice_status == "speaking"
        stops = rig.speaker.stops
        rig.detector.pending = ["小森"]  # 喇叭傳回來的自己的名字
        await rig.push()
        assert rig.speaker.stops == stops and state.voice_status == "speaking"  # 沒有中斷
        await asyncio.sleep(0.3)
        await settle()
        assert rig.speaker.played == 9600  # 整段播完
        rig.speaker.playing = False
        await rig.push()  # 播完：回到聆聽，防護解除
        assert state.voice_status == "listening" and rig.session._said_name is False
        await rig.close()
    run(go())

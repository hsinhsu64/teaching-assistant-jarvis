"""啟動助理：兩個 app、對外通道、自我檢查、投影與主持人視窗。

    uv run python -m xiaoshen      （或雙擊 start.bat）
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Callable

from .config import Config, load_config
from .platform_support import app_data_dir, kill_profile_processes, monitor_rects
from . import eventlog, persona
from .deck import list_decks, set_renderer
from .hub import Hub
from .pubmed import search_pubmed
from .quizbank import load_bank
from .render_proc import ProcessRenderer
from .runner import make_servers
from .jobs import JobRunner
from .library import Library
from .llm import GeminiLLM
from .oe_client import oe_auth_ok
from .oe_relay import relay_connected
from .oe_setup import ensure_relay, find_oe_mcp, resolve_oe_script
from .selfcheck import _default_http_get, openevidence_result, references_check, run_checks
from .slide_ocr import SlideReader, gemini_transcriber
from .first_run import ensure_data_folder
from .host_phone import create_gateway
from .phone_mic import PhoneMicMux
from .server import create_control_app, create_phone_app
from .converter import powerpoint_available, powerpoint_convert
from .state import AppState, check_phone_link, refresh_presentations
from .tunnel import TunnelError, TunnelHandle, find_cloudflared, start_quick_tunnel

Rect = tuple[int, int, int, int]
QUIT_DELAY = 0.5   # 按「結束程式」後等多久才關：讓主持人畫面先收到回應
SERVER_GRACE = 3.0 # 伺服器收尾最多等幾秒
APP_PROFILES = ("edge-display", "edge-host")  # 本程式自己開的投影、主持人視窗
PHONE_CHECK_SECONDS = 30  # 晨會中每隔多久確認一次手機作答網址
PRESENTATION_CHECK_SECONDS = 30  # 晨會中每隔多久檢查「簡報」裡有沒有新的或改過的 pptx


def pick_display_position(rects: list[Rect], index: int) -> tuple[tuple[int, int], str | None]:
    if 0 <= index < len(rects):
        left, top, _, _ = rects[index]
        return (left, top), None
    left, top = (rects[0][0], rects[0][1]) if rects else (0, 0)
    return (left, top), f"找不到第 {index + 1} 台螢幕（投影機），投影視窗改開在主螢幕"


WINDOW_MODE_WARNING = "請把投影視窗拖到投影機並全螢幕"


def display_args(browser: Path, profile: Path, x: int, y: int, url: str, kiosk: bool) -> list[str]:
    """投影視窗的瀏覽器參數。kiosk=True 沿用原本的全螢幕 kiosk（Edge 另加 --edge-kiosk-type）；
    kiosk=False（Mac 等不適合 kiosk 的環境）開一般 app 視窗，由主持人自己拖到投影機並全螢幕。"""
    head = [str(browser), f"--user-data-dir={profile}", f"--window-position={x},{y}"]
    if not kiosk:
        return head + [f"--app={url}", "--window-size=1280,720", "--no-first-run"]
    args = head + ["--kiosk", url]
    if "edge" in browser.name.lower():
        args.append("--edge-kiosk-type=fullscreen")
    return args + ["--no-first-run"]


def open_windows(config: Config, state: AppState) -> None:
    base = f"http://127.0.0.1:{config.control_port}"
    profile = app_data_dir()
    (x, y), warning = pick_display_position(monitor_rects(), config.display_monitor)
    if warning:
        state.warnings.append(warning)
    if config.browser is None or not config.browser.exists():
        state.warnings.append(f"找不到瀏覽器 {config.browser or ''}，改用預設瀏覽器開啟")
        webbrowser.open(f"{base}/display")
        webbrowser.open(f"{base}/host")
        return
    if not config.display_kiosk:
        state.warnings.append(WINDOW_MODE_WARNING)
    subprocess.Popen(display_args(config.browser, profile / 'edge-display', x, y, f"{base}/display",
                                  config.display_kiosk))
    subprocess.Popen([str(config.browser), f"--user-data-dir={profile / 'edge-host'}",
                      f"--app={base}/host", "--no-first-run"])


def close_app_windows(profile_root: Path, kill: Callable[[Path], None] | None = None) -> None:
    """結束時關掉本程式自己開的投影與主持人視窗（只動本程式專用的設定檔，不碰你平常的 Edge）。"""
    if kill is None:
        kill = kill_profile_processes
    for name in APP_PROFILES:
        kill(profile_root / name)


def make_quit_handler(loop: asyncio.AbstractEventLoop, quit_event: asyncio.Event,
                      delay: float = QUIT_DELAY) -> Callable[[], None]:
    """給 /api/quit 用：可在任何執行緒呼叫；delay 秒後才設定 quit_event（先讓回應送出去）。"""
    def handler() -> None:
        loop.call_soon_threadsafe(loop.call_later, delay, quit_event.set)
    return handler


async def serve_until_quit(server_tasks: list[asyncio.Task], other_tasks: list[asyncio.Task],
                           servers: list, quit_event: asyncio.Event, grace: float = SERVER_GRACE) -> None:
    """一直跑到主持人按「結束程式」（或 Ctrl+C）。伺服器或背景工作出錯照常丟出。
    結束時先讓伺服器正常收尾，再停掉背景工作。"""
    gathered = asyncio.gather(*server_tasks, *other_tasks)
    waiter = asyncio.ensure_future(quit_event.wait())
    try:
        await asyncio.wait([gathered, waiter], return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if gathered.done():
        gathered.result()
        return
    for s in servers:
        s.should_exit = True
    if server_tasks:
        await asyncio.wait(server_tasks, timeout=grace)
    gathered.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await gathered


def make_source_viewer(config: Config, state: AppState, library: Library):
    """顯示出處原文（§6.2a）；原文網頁開在投影螢幕上的獨立瀏覽器視窗。"""
    from .source_view import EdgeWindow, SourceViewer

    web = None
    if config.browser is not None and config.browser.exists():
        profile = app_data_dir() / "edge-source"
        (x, y), _ = pick_display_position(monitor_rects(), config.display_monitor)
        web = EdgeWindow(config.browser, profile, (x, y), kiosk=config.display_kiosk)
    return SourceViewer(state, library, web=web)


OCR_CACHE_DIR = app_data_dir() / "ocr"


def voice_devices(config: Config, devices: list[dict] | None = None) -> tuple[int | None, int | None]:
    """（麥克風, 喇叭）裝置編號；None = Windows 預設。麥克風名稱找不到 → RuntimeError。"""
    from .audio_io import find_device

    mic = find_device(config.audio_device, "input", devices)
    if mic is None and config.audio_device:
        raise RuntimeError(f"找不到名稱含「{config.audio_device}」的麥克風")
    speaker = find_device(config.speaker_device, "output", devices)
    if speaker is None and config.speaker_device:
        raise RuntimeError(f"找不到名稱含「{config.speaker_device}」的喇叭")
    return mic, speaker


def build_detector(config: Config):
    """依設定的喚醒詞建偵測器；詞不合法或含模型不認得的音就退回預設，回傳 (偵測器, 警告或 None)。

    建好後把實際生效的詞交給 persona，畫面上的「喊…」才會和偵測器一致。"""
    from .wakeword import DEFAULT_WAKE, UnknownTokenError, WakeWordDetector, validate_wake_word

    word = config.effective_wake_word
    reason = validate_wake_word(word)
    if reason is None:
        try:
            detector = WakeWordDetector(config.kws_model_dir, config.kws_threshold, config.kws_score, word)
            persona.set_wake_word(detector.wake_label)
            return detector, None
        except UnknownTokenError as e:
            reason = str(e)
    warning = f"喚醒詞「{word}」無法使用（{reason}），已改用預設喚醒詞「{DEFAULT_WAKE}」"
    detector = WakeWordDetector(config.kws_model_dir, config.kws_threshold, config.kws_score, DEFAULT_WAKE)
    persona.set_wake_word(detector.wake_label)
    return detector, warning


def voice_check_functions(config: Config) -> dict:
    """自我檢查用的語音檢查函式（在背景執行緒呼叫）。"""
    from .audio_io import probe_mic
    from .live_backend import GeminiLiveBackend
    def mic_check() -> float:
        return probe_mic(voice_devices(config)[0])

    def speaker_check() -> bool:
        try:
            voice_devices(config)
            return True
        except RuntimeError:
            return False

    def wake_check() -> bool:
        try:
            detector, warning = build_detector(config)
            return detector.wake_label, warning
        except Exception:
            return False

    def live_check() -> float:
        async def ping() -> float:
            t = time.monotonic()
            async with GeminiLiveBackend(config.live_model).connect("ping", []):
                return time.monotonic() - t

        return asyncio.run(ping())

    return {"mic_check": mic_check, "speaker_check": speaker_check, "wake_check": wake_check, "live_check": live_check}


async def supervise_voice(run, state: AppState) -> None:
    """語音的任何執行期錯誤（麥克風被拔、裝置被占用…）只停用語音，不能讓整個程式結束（翻頁、作答照常）。"""
    try:
        await run
    except asyncio.CancelledError:
        raise
    except Exception as e:
        state.voice_status = "off"
        state.wake_handler = None
        state.stop_handler = None
        state.warnings.append(f"語音停止：{e}（簡報與作答照常；重新啟動{persona.current().display_name}可恢復）")
        state._changed()


async def start_voice(config: Config, state: AppState, jobs: JobRunner, library: Library,
                      viewer=None) -> asyncio.Task:
    """建立麥克風、喇叭、喚醒詞與語音 session，回傳執行中的 task。"""
    from .audio_io import MicStream, Speaker
    from .live_backend import GeminiLiveBackend
    from .voice import VoiceSession
    from .vocabulary import deck_context
    from .voice_tools import VoiceTools

    detector, warning = await asyncio.to_thread(build_detector, config)
    if warning:
        state.warnings.append(warning)
    mic_index, speaker_index = voice_devices(config)
    speaker = Speaker(speaker_index)
    mic = PhoneMicMux(MicStream(mic_index), state.phone_mic, state)  # 手機按叫醒：那一輪改收手機（§6.3b）
    backend = GeminiLiveBackend(config.live_model, config.voice_end_silence_ms, config.live_voice)
    session = VoiceSession(state, detector, backend, VoiceTools(state, jobs, library, viewer),
                           mic, speaker, idle_seconds=config.voice_idle_seconds,
                           followup_seconds=config.voice_followup_seconds,
                           max_listen_seconds=config.voice_max_listen_seconds,
                           context=deck_context(state), speech_rms=config.voice_speech_rms)
    state.wake_handler = session.wake
    state.stop_handler = session.stop
    return asyncio.create_task(supervise_voice(session.run(), state))


async def _amain() -> None:
    config = load_config()
    persona.start_from_config(config)
    # 沒裝 Claude Code 的電腦：自己帶起 OpenEvidence 轉接程式、用 openevidence-mcp 附的 oe.py（2026-11-08）
    oe_mcp = find_oe_mcp(config)
    config.oe_script = resolve_oe_script(config, oe_mcp)
    relay_note = await asyncio.to_thread(ensure_relay, oe_mcp)
    eventlog.configure(app_data_dir() / "logs")
    renderer = ProcessRenderer()  # 轉圖放子行程，不卡語音
    renderer.warm()
    set_renderer(renderer)
    bank, bank_warnings = load_bank(config.folder("題庫") / "題庫.xlsx")
    state = AppState(config, bank)
    state.warnings.extend(f"題庫 {w}" for w in bank_warnings)
    if relay_note:
        state.warnings.append(relay_note)
    # 先把 pptx／ppt 轉成 PDF，「最新簡報」才會是剛改好的那份
    convert = powerpoint_convert if config.auto_convert_pptx and powerpoint_available() else None
    reported: set = set()
    await asyncio.to_thread(refresh_presentations, state, convert, reported)
    decks = list_decks(config.folder("簡報"))
    state.set_deck_paths(decks)
    if decks:
        state.open_deck(decks[0])  # 「今天的簡報」，開機不投影（§6.0 主畫面）

    # Phase 2：參考資料索引與 AI 背景工作
    library = Library(config.folder("參考資料"), convert)
    library_warnings = await asyncio.to_thread(library.refresh)
    llm = GeminiLLM(config.model_fallbacks, timeout=config.ai_timeout_seconds)
    # OE 之後再查 PubMed；都沒有才用 Gemini 一般知識（標示未經文獻驗證）
    jobs = JobRunner(state, llm, config, library, pubmed=search_pubmed, knowledge_fallback=True)

    hub = Hub()
    viewer = make_source_viewer(config, state, library)
    control, phone = create_control_app(state, hub, jobs, viewer), create_phone_app(state, jobs)
    servers = make_servers([(control, config.control_port), (create_gateway(phone, control, state), config.phone_port)])
    serving = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        if any(t.done() for t in serving):
            for t in serving:
                if t.done() and t.exception():
                    raise t.exception()
            raise SystemExit("伺服器啟動失敗（連接埠可能被占用）")
        await asyncio.sleep(0.1)

    tunnel: TunnelHandle | None = None
    try:
        tunnel = await asyncio.to_thread(start_quick_tunnel, config.phone_port, find_cloudflared(config.cloudflared))
        state.public_base = tunnel.url
    except TunnelError as e:
        state.warnings.append(str(e))

    open_windows(config, state)
    def llm_check() -> str:
        llm.generate_json(config.model_answer, "Reply in JSON.", "Reply with ok.",
                          {"type": "object", "properties": {"ok": {"type": "string"}}}, timeout=30)
        return llm.last_model

    voice_checks = voice_check_functions(config) if config.voice_enabled else {}
    checks = await asyncio.to_thread(
        run_checks, config, state, None, bank_warnings, 6, 3.0,
        llm_check=llm_check, oe_check=lambda: oe_auth_ok(config.oe_script), oe_tab_check=relay_connected,
        library=library, library_warnings=library_warnings, **voice_checks)
    state.checks = [c.to_dict() for c in checks]
    # 黃燈那一列可以「重新檢查」
    state.oe_recheck = lambda: openevidence_result(oe_auth_ok(config.oe_script), relay_connected())
    state._changed()
    if voice_checks:
        failed = {c.name for c in checks if c.level == "fail"}
        if failed & {"麥克風", "喚醒詞", "語音模型"}:
            state.warnings.append("語音停用（見自我檢查）；簡報與作答照常")
        else:
            try:
                voice_task = await start_voice(config, state, jobs, library, viewer)  # 不放進 serving：語音結束不影響伺服器
            except Exception as e:  # 語音任何問題都不影響其他功能
                state.voice_status = "off"
                state.warnings.append(f"語音無法啟動：{e}")
        state._changed()
    async def watch_phone_link():
        while True:
            await asyncio.sleep(PHONE_CHECK_SECONDS)
            await asyncio.to_thread(check_phone_link, state, _default_http_get)

    def refresh_library() -> None:
        before = {n: len(d.pages) for n, d in library.docs.items()}
        warns = library.refresh()
        detail = references_check(library, warns)
        state.set_check("參考資料", detail.level, detail.detail)
        if {n: len(d.pages) for n, d in library.docs.items()} != before:
            state._changed()

    async def watch_slide_images():
        """開了新簡報就在背景抄錄以圖片為主的投影片。翻頁不受影響。"""
        reader = SlideReader(state, gemini_transcriber(config.model_ocr), OCR_CACHE_DIR)
        last = None
        while True:
            if state.deck is not None and state.deck_version != last and state.summary_path is None:
                last = state.deck_version
                try:
                    await asyncio.to_thread(reader.run_once)
                except Exception as e:
                    state.warnings.append(f"投影片圖片辨識失敗：{e}")
            await asyncio.sleep(5)

    async def watch_presentations():
        while True:
            await asyncio.sleep(PRESENTATION_CHECK_SECONDS)
            await asyncio.to_thread(refresh_presentations, state, convert, reported)
            await asyncio.to_thread(refresh_library)
            decks = await asyncio.to_thread(list_decks, config.folder("簡報"))
            state.set_deck_paths(decks)
            if state.deck is None:
                if decks:
                    state.open_deck(decks[0])

    watchers = [asyncio.create_task(watch_phone_link()), asyncio.create_task(watch_presentations())]
    quit_event = asyncio.Event()
    state.quit_handler = make_quit_handler(asyncio.get_running_loop(), quit_event)
    ocr_task = asyncio.create_task(watch_slide_images())  # 不放進 serving：失敗不影響伺服器
    print(f"{persona.current().display_name}已啟動：主持人畫面 http://127.0.0.1:{config.control_port}/host　（按 Ctrl+C 結束）", flush=True)
    try:
        await serve_until_quit(serving, watchers, servers, quit_event)
    finally:
        state.quit_handler = None
        if tunnel:
            tunnel.stop()
        state.close_source_view()  # 結束時一併關掉投影上的原文網頁視窗
        state.wait_web_close()
        set_renderer(None)
        renderer.close()
        close_app_windows(app_data_dir())
    print(f"{persona.current().display_name}已結束。", flush=True)


def main() -> None:
    # 第一次啟動：還沒有資料夾就請使用者填設定並選位置（不要求 Google Drive，2026-11-08）
    if not ensure_data_folder():
        print("沒有選資料夾，程式沒有啟動。下次啟動會再問一次。", flush=True)
        raise SystemExit(1)
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass

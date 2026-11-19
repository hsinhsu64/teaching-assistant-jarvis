import asyncio

from xiaoshen.launcher import supervise_voice


def test_voice_crash_does_not_propagate(state):
    async def boom():
        await asyncio.sleep(0)
        raise OSError("麥克風被拔掉")

    state.voice_status = "listening"
    asyncio.run(supervise_voice(boom(), state))  # 不得拋出（否則整個程式連同翻頁一起結束）
    assert state.voice_status == "off"
    assert any("語音停止" in w and "麥克風被拔掉" in w for w in state.warnings)


def test_voice_devices_speaker_follows_windows_default():
    """實測：USBMIC 只有麥克風有接；喇叭要跟 Windows 預設輸出（使用者平常聽到的地方），不能跟麥克風綁同一個名稱。"""
    from xiaoshen.config import Config
    from xiaoshen.launcher import voice_devices

    devices = [
        {"name": "麥克風 (USBMIC1.)", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "SAMSUNG (NVIDIA High Definition", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "喇叭 (USBMIC1.)", "max_input_channels": 0, "max_output_channels": 2},
    ]
    assert voice_devices(Config(audio_device="USBMIC"), devices) == (0, None)  # None = Windows 預設輸出
    assert voice_devices(Config(audio_device="USBMIC", speaker_device="SAMSUNG"), devices) == (0, 1)


# ---- 主持人畫面「結束程式」----

class _FakeServer:
    """像 uvicorn.Server：should_exit 變 True 後 serve() 才結束。"""
    def __init__(self):
        self.should_exit = False
        self.stopped = False

    async def serve(self):
        while not self.should_exit:
            await asyncio.sleep(0.01)
        self.stopped = True


def test_serve_until_quit_stops_servers_gracefully_and_cancels_watchers():
    from xiaoshen.launcher import serve_until_quit

    async def scenario():
        servers = [_FakeServer(), _FakeServer()]
        server_tasks = [asyncio.create_task(s.serve()) for s in servers]
        watcher = asyncio.create_task(asyncio.sleep(3600))  # 像 watch_phone_link：永遠不結束
        quit_event = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, quit_event.set)
        await asyncio.wait_for(serve_until_quit(server_tasks, [watcher], servers, quit_event), 2)
        return servers, watcher

    servers, watcher = asyncio.run(scenario())
    assert all(s.should_exit and s.stopped for s in servers)  # 伺服器正常收尾（回應送得出去）
    assert watcher.cancelled()


def test_serve_until_quit_still_raises_server_errors():
    import pytest
    from xiaoshen.launcher import serve_until_quit

    async def scenario():
        async def broken():
            await asyncio.sleep(0)
            raise OSError("連接埠被占用")

        await asyncio.wait_for(serve_until_quit([asyncio.create_task(broken())], [], [], asyncio.Event()), 2)

    with pytest.raises(OSError, match="連接埠被占用"):
        asyncio.run(scenario())


def test_quit_handler_sets_event_from_another_thread():
    import threading
    from xiaoshen.launcher import make_quit_handler

    async def scenario():
        quit_event = asyncio.Event()
        handler = make_quit_handler(asyncio.get_running_loop(), quit_event, delay=0.01)
        threading.Thread(target=handler).start()  # FastAPI 的同步端點在執行緒裡呼叫
        await asyncio.wait_for(quit_event.wait(), 2)
        return quit_event.is_set()

    assert asyncio.run(scenario())


def test_quit_handler_accepts_longer_delay_for_voice():
    import asyncio

    from xiaoshen.launcher import make_quit_handler

    async def scenario():
        loop = asyncio.get_running_loop()
        quit_event = asyncio.Event()
        handler = make_quit_handler(loop, quit_event, delay=0.01)
        start = loop.time()
        handler(0.3)  # 語音結束：等助理把話說完才關
        await asyncio.wait_for(quit_event.wait(), 2)
        return loop.time() - start

    assert asyncio.run(scenario()) >= 0.25


def test_close_app_windows_only_targets_xiaoshen_profiles(tmp_path):
    from xiaoshen.launcher import close_app_windows

    killed = []
    close_app_windows(tmp_path, kill=killed.append)
    assert [p.name for p in killed] == ["edge-display", "edge-host"]


from pathlib import Path  # noqa: E402

from xiaoshen import launcher  # noqa: E402
from xiaoshen.launcher import display_args  # noqa: E402


def test_display_args_kiosk_edge():
    a = display_args(Path("msedge.exe"), Path("p"), 1920, 0, "http://x/display", True)
    assert a == [str(Path("msedge.exe")), f"--user-data-dir={Path('p')}", "--window-position=1920,0",
                 "--kiosk", "http://x/display", "--edge-kiosk-type=fullscreen", "--no-first-run"]


def test_display_args_kiosk_chrome_has_no_edge_flag():
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    a = display_args(chrome, Path("p"), 0, 0, "http://x/display", True)
    assert "--kiosk" in a and not any(x.startswith("--edge-kiosk-type") for x in a)


def test_display_args_window_mode():
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    a = display_args(chrome, Path("p"), 5, 6, "http://x/display", False)
    assert "--kiosk" not in a and "--app=http://x/display" in a
    assert "--window-position=5,6" in a and "--window-size=1280,720" in a


def _run_open_windows(state, monkeypatch, tmp_path, kiosk):
    browser = tmp_path / "msedge.exe"
    browser.write_text("")
    state.config.browser = browser
    state.config.display_kiosk = kiosk
    calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(launcher, "monitor_rects", lambda: [(0, 0, 1920, 1080)])
    launcher.open_windows(state.config, state)
    return calls


def test_open_windows_window_mode_warns_once(state, monkeypatch, tmp_path):
    calls = _run_open_windows(state, monkeypatch, tmp_path, False)
    assert "--kiosk" not in calls[0]
    assert state.warnings.count("請把投影視窗拖到投影機並全螢幕") == 1


def test_open_windows_kiosk_no_warning(state, monkeypatch, tmp_path):
    calls = _run_open_windows(state, monkeypatch, tmp_path, True)
    assert "--kiosk" in calls[0]
    assert "請把投影視窗拖到投影機並全螢幕" not in state.warnings

"""主持人手機的麥克風（spec §6.3b，2026-11-08）：手機按「叫醒」後，那一輪只收手機的聲音。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from xiaoshen.host_phone import create_gateway
from xiaoshen.hub import Hub
from xiaoshen.phone_mic import BLOCK_BYTES, NO_SESSION_BLOCKS, PhoneMic, PhoneMicMux
from xiaoshen.server import create_control_app, create_phone_app

BASE = "https://example.trycloudflare.com"


def blk(v: int) -> bytes:
    return bytes([v]) * BLOCK_BYTES


def test_feed_rebuffers_into_100ms_blocks():
    m = PhoneMic()
    gen = m.start()
    assert m.feed(gen, blk(1)[:1000]) and m.feed(gen, blk(1)[1000:] + blk(2)[:10])
    assert m.take() == blk(1) and m.take() is None


def test_feed_ignored_when_not_active_or_stale():
    m = PhoneMic()
    old = m.start()
    new = m.start()
    assert not m.feed(old, blk(1))  # 舊連線：通知它關閉
    assert m.feed(new, blk(2)) and m.take() == blk(2)
    m.stop(old)  # 舊連線關閉不影響新的
    assert m.active
    m.stop(new)
    assert not m.active and not m.feed(new, blk(3))


class FakeState:
    def __init__(self):
        self.voice_status = "standby"


async def laptop(n):
    for _ in range(n):
        yield blk(9)


def run_mux(mux, n):
    async def go():
        out = []
        async for b in mux:
            out.append(b)
            if len(out) == n:
                break
        return out
    return asyncio.run(go())


def test_mux_passes_laptop_mic_when_phone_idle():
    mux = PhoneMicMux(laptop(3), PhoneMic(), FakeState())
    assert run_mux(mux, 3) == [blk(9)] * 3


def test_mux_uses_only_phone_audio_while_phone_active():
    phone, st = PhoneMic(), FakeState()
    gen = phone.start()
    phone.feed(gen, blk(1))
    st.voice_status = "listening"
    out = run_mux(PhoneMicMux(laptop(3), phone, st), 3)
    assert out == [blk(1), bytes(BLOCK_BYTES), bytes(BLOCK_BYTES)]  # 沒收到就補靜音，筆電的聲音不用


def test_mux_catches_up_when_phone_audio_piles_up():
    phone, st = PhoneMic(), FakeState()
    gen = phone.start()
    st.voice_status = "listening"
    for v in (1, 2, 3, 4):
        phone.feed(gen, blk(v))
    out = run_mux(PhoneMicMux(laptop(2), phone, st), 4)
    assert out == [blk(1), blk(2), blk(3), blk(4)]  # 兩個時間單位就送完，不累積延遲


def test_mux_releases_phone_when_session_ends():
    phone, st = PhoneMic(), FakeState()
    gen = phone.start()
    mux = PhoneMicMux(laptop(5), phone, st)

    async def go():
        out = []
        async for b in mux:
            out.append(b)
            st.voice_status = "listening" if len(out) == 1 else "standby"
            if len(out) == 4:
                break
        return out
    out = asyncio.run(go())
    assert not phone.active and out[-1] == blk(9)  # 回到待命：改回筆電麥克風
    assert not phone.feed(gen, blk(1))


def test_mux_releases_phone_if_wake_never_happens():
    phone, st = PhoneMic(), FakeState()
    phone.start()
    run_mux(PhoneMicMux(laptop(NO_SESSION_BLOCKS + 2), phone, st), NO_SESSION_BLOCKS + 2)
    assert not phone.active


@pytest.fixture
def gate(state):
    state.public_base = BASE
    control = create_control_app(state, Hub())
    with TestClient(create_gateway(create_phone_app(state), control, state), base_url=BASE,
                    follow_redirects=False) as c:
        yield c


def test_mic_socket_needs_pairing(gate):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with gate.websocket_connect("/ws/mic"):
            pass


def test_paired_phone_streams_audio(gate, state):
    session = gate.get("/h", params={"c": state.host_pairing.new_code()}).cookies["xs_host"]
    with gate.websocket_connect("/ws/mic", headers={"cookie": f"xs_host={session}"}) as ws:
        ws.send_bytes(blk(5))
        ws.send_bytes(blk(6))
        ws.send_text("ping")  # 往返一次：確定前面的音訊已處理
        assert ws.receive_text() == "ok"
        assert state.phone_mic.active
        assert state.phone_mic.take() == blk(5)
    assert not state.phone_mic.active

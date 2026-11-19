"""主持人手機（spec §6.3b）：掃筆電 QR Code 配對後，手機有完整主持人功能，但拿不到投影片圖。"""

import pytest
from fastapi.testclient import TestClient

from xiaoshen.host_phone import CODE_TTL, HostPairing, create_gateway
from xiaoshen.hub import Hub
from xiaoshen.server import create_control_app, create_phone_app

BASE = "https://example.trycloudflare.com"  # cookie 是 Secure：測試也要走 https


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pairing(clock):
    return HostPairing(now=clock)


def test_code_works_once(pairing):
    code = pairing.new_code()
    session = pairing.redeem(code)
    assert session and pairing.valid(session)
    assert pairing.redeem(code) is None


def test_code_expires(pairing, clock):
    code = pairing.new_code()
    clock.now += CODE_TTL + 1
    assert pairing.redeem(code) is None


def test_new_code_replaces_old_code(pairing):
    old = pairing.new_code()
    new = pairing.new_code()
    assert pairing.redeem(old) is None and pairing.redeem(new)


def test_new_pairing_replaces_old_phone(pairing):
    first = pairing.redeem(pairing.new_code())
    second = pairing.redeem(pairing.new_code())
    assert pairing.valid(second) and not pairing.valid(first)


def test_revoke_ends_session(pairing):
    session = pairing.redeem(pairing.new_code())
    pairing.revoke()
    assert not pairing.valid(session) and not pairing.paired


def test_garbage_is_not_valid(pairing):
    assert not pairing.valid("") and not pairing.valid("x") and pairing.redeem("") is None


@pytest.fixture
def ctl(state):
    state.public_base = BASE
    with TestClient(create_control_app(state, Hub())) as c:
        yield c


@pytest.fixture
def gate(state, ctl):
    app = create_gateway(create_phone_app(state), ctl.app, state)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c


def pair(gate, state):
    r = gate.get("/h", params={"c": state.host_pairing.new_code()})
    assert r.status_code == 303 and r.headers["location"] == "/host"
    return r


def test_unpaired_phone_has_no_host_access(gate):
    for path in ["/host", "/api/state", "/slides/0.png", "/display"]:
        assert gate.get(path).status_code in (404, 405), path
    assert gate.post("/api/nav", json={"action": "next"}).status_code in (404, 405)


def test_resident_link_still_works_through_gateway(gate, state):
    assert gate.get("/q", params={"t": state.token}).status_code == 200
    assert gate.get("/healthz").json() == {"ok": True}


def test_cookie_is_locked_down(gate, state):
    cookie = pair(gate, state).headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_bad_code_refused(gate):
    r = gate.get("/h", params={"c": "wrong"})
    assert r.status_code == 403 and "重新" in r.text
    assert gate.get("/host").status_code == 404


def test_paired_phone_can_host(gate, state):
    pair(gate, state)
    assert gate.get("/host").status_code == 200
    assert gate.get("/api/state").json()["page"] == state.page
    before = state.page
    assert gate.post("/api/nav", json={"action": "next"}).status_code == 200
    assert state.page == before + 1


def test_paired_phone_cannot_get_slide_images(gate, state):
    pair(gate, state)
    for path in ["/slides/0.png", "/source/page.png", "/deck/links/0", "/display"]:
        assert gate.get(path).status_code == 403, path


def test_paired_phone_cannot_pair_or_revoke(gate, state):
    pair(gate, state)
    assert gate.post("/api/host-phone/pair").status_code == 403
    assert gate.post("/api/host-phone/revoke").status_code == 403
    assert state.host_pairing.paired


def test_phone_side_routes_stay_on_phone_app_when_paired(gate, state):
    pair(gate, state)
    assert gate.get("/q", params={"t": state.token}).status_code == 200
    assert gate.post("/api/phone/control-enabled", json={"enabled": True}).status_code == 200  # 主持人開關走控制端
    assert state.phone_control_enabled is True  # 預設關（2026-11-18），打開才看得出開關有作用


def test_revoked_phone_loses_access(gate, ctl, state):
    pair(gate, state)
    assert ctl.post("/api/host-phone/revoke").status_code == 200
    assert gate.get("/host").status_code == 404


def test_host_websocket_needs_pairing(gate, state):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with gate.websocket_connect("/ws?audience=host"):
            pass
    session = pair(gate, state).cookies["xs_host"]
    # TestClient 的 WebSocket 不走 https 網域，cookie 要自己帶
    with gate.websocket_connect("/ws?audience=host", headers={"cookie": f"xs_host={session}"}) as ws:
        assert "page" in ws.receive_json()


def test_laptop_pair_returns_qr_for_tunnel_url(ctl, state):
    r = ctl.post("/api/host-phone/pair").json()
    assert r["qr"].startswith("data:image/png;base64,") and r["expires_in"] == CODE_TTL


def test_pair_needs_tunnel(ctl, state):
    state.public_base = None
    assert ctl.post("/api/host-phone/pair").status_code == 409


def test_host_snapshot_shows_pairing_only_to_host(gate, state):
    assert state.snapshot("host")["host_phone"] == {"paired": False}
    pair(gate, state)
    assert state.snapshot("host")["host_phone"] == {"paired": True}
    assert "host_phone" not in state.snapshot("display")


def test_paired_host_phone_reaches_settings(gate, state):
    pair(gate, state)
    assert gate.get("/api/settings").status_code == 200

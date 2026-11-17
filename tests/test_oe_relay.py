"""OpenEvidence 經瀏覽器分頁的 relay 送出（參考 htlin222/openevidence-mcp）。

relay（127.0.0.1:8787）把請求交給使用者已登入的 OpenEvidence 分頁執行；沒有連線就照舊用 cookies。
"""

import json
import subprocess
import sys
import textwrap

from xiaoshen import oe_relay

FAKE_OE = textwrap.dedent("""
    import json, sys

    def _request(jar, method, path, body=None):
        return 200, json.dumps({"via": "cookies", "path": path}).encode()

    def main():
        status, body = _request(None, "GET", "/api/auth/me")
        print(json.dumps({"argv": sys.argv[1:], "result": json.loads(body)}))
""")


def test_relay_request_posts_method_path_body():
    sent = {}

    def post(url, payload, timeout):
        sent.update(url=url, payload=payload)
        return {"status": 200, "body": '{"ok": true}'}

    status, body = oe_relay.relay_request("POST", "/api/article", {"q": 1}, post=post)
    assert status == 200 and json.loads(body) == {"ok": True}
    assert sent["url"] == "http://127.0.0.1:8787/relay"
    assert sent["payload"]["method"] == "POST" and sent["payload"]["path"] == "/api/article"
    assert json.loads(sent["payload"]["body"]) == {"q": 1}


def test_relay_connected_reads_health():
    assert oe_relay.relay_connected(get=lambda url, timeout: {"ok": True, "connected": True}) is True
    assert oe_relay.relay_connected(get=lambda url, timeout: {"ok": True, "connected": False}) is False

    def down(url, timeout):
        raise OSError("refused")

    assert oe_relay.relay_connected(get=down) is False


def test_run_oe_uses_relay_when_connected(tmp_path, capsys):
    script = tmp_path / "oe.py"
    script.write_text(FAKE_OE, encoding="utf-8")
    calls = []
    oe_relay.run_oe(script, ["auth-status"], connected=lambda: True,
                    request=lambda method, path, body=None: calls.append((method, path)) or (200, b'{"via": "relay"}'))
    out = json.loads(capsys.readouterr().out)
    assert out == {"argv": ["auth-status"], "result": {"via": "relay"}} and calls == [("GET", "/api/auth/me")]


def test_run_oe_falls_back_to_cookies_when_relay_down(tmp_path, capsys):
    script = tmp_path / "oe.py"
    script.write_text(FAKE_OE, encoding="utf-8")
    oe_relay.run_oe(script, ["auth-status"], connected=lambda: False,
                    request=lambda *a, **k: (_ for _ in ()).throw(AssertionError("relay used")))
    assert json.loads(capsys.readouterr().out)["result"]["via"] == "cookies"


def test_runner_is_a_standalone_script(tmp_path):
    # 助理用子行程執行：python <oe_relay.py> <oe.py 的參數>，oe.py 路徑用環境變數傳
    script = tmp_path / "oe.py"
    script.write_text(FAKE_OE, encoding="utf-8")
    env = {"XIAOSHEN_OE_SCRIPT": str(script), "XIAOSHEN_OE_RELAY": "off", "PYTHONIOENCODING": "utf-8",
           "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    proc = subprocess.run([sys.executable, oe_relay.__file__, "ask", "q"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["argv"] == ["ask", "q"]



FAKE_OE_NEEDS_COOKIES = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path

    def resolve_cookies_path():
        p = os.environ.get("OE_COOKIES")
        if p and Path(p).is_file():
            return Path(p)
        raise SystemExit("cookies.json not found")

    def _request(jar, method, path, body=None):
        raise AssertionError("cookies used")

    def main():
        cookies = json.loads(resolve_cookies_path().read_text(encoding="utf-8"))
        status, body = _request(cookies, "GET", "/api/auth/me")
        print(json.dumps({"cookies": cookies, "result": json.loads(body)}))
""")


def test_relay_works_without_cookies_file(tmp_path, capsys, monkeypatch):
    # 只在瀏覽器分頁登入、沒有 cookies.json 的電腦（2026-11-08）：用空的 cookie 檔讓 oe.py 跑得下去
    monkeypatch.delenv("OE_COOKIES", raising=False)
    script = tmp_path / "oe.py"
    script.write_text(FAKE_OE_NEEDS_COOKIES, encoding="utf-8")
    oe_relay.run_oe(script, ["auth-status"], connected=lambda: True,
                    request=lambda method, path, body=None: (200, b'{"via": "relay"}'))
    out = json.loads(capsys.readouterr().out)
    assert out["result"] == {"via": "relay"}
    assert [c["domain"] for c in out["cookies"]] == ["invalid.example"]  # 假 cookie：真的 oe.py 不接受空清單，也不會送出

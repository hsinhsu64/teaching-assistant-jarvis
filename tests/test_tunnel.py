import pytest

from xiaoshen.tunnel import TunnelError, parse_tunnel_url, start_quick_tunnel


def test_parse_tunnel_url():
    line = "2026-11-01 INF |  https://brave-otter-12ab.trycloudflare.com  |"
    assert parse_tunnel_url(line) == "https://brave-otter-12ab.trycloudflare.com"
    assert parse_tunnel_url("INF Starting tunnel") is None
    assert parse_tunnel_url("see https://www.cloudflare.com/terms") is None


def test_missing_cloudflared_raises(tmp_path):
    with pytest.raises(TunnelError):
        start_quick_tunnel(8766, tmp_path / "nope.exe", timeout=1)


def test_find_cloudflared_falls_back_to_default_install(tmp_path, monkeypatch):
    import xiaoshen.tunnel as t

    exe = tmp_path / "cloudflared.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(t.shutil, "which", lambda name: None)
    monkeypatch.setattr(t, "DEFAULT_INSTALL_PATHS", [tmp_path / "missing.exe", exe])
    assert t.find_cloudflared(None) == exe


def test_find_cloudflared_uses_bundled(tmp_path, monkeypatch):
    import xiaoshen.tunnel as t

    exe = tmp_path / "cloudflared"
    exe.write_bytes(b"")
    monkeypatch.setattr(t.shutil, "which", lambda name: None)
    monkeypatch.setattr(t, "DEFAULT_INSTALL_PATHS", [tmp_path / "missing.exe"])
    monkeypatch.setattr(t, "bundled_tool", lambda name: exe if name == "cloudflared" else None)
    assert t.find_cloudflared(None) == exe

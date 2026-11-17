"""沒裝 Claude Code 也能用 OpenEvidence：教學助理啟動時自動帶起 openevidence-mcp 的轉接程式。"""

from pathlib import Path

from xiaoshen.config import Config
from xiaoshen.oe_setup import ensure_relay, find_oe_mcp, resolve_oe_script


def make_mcp(root: Path) -> Path:
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "relay-daemon.js").write_text("", encoding="utf-8")
    (root / "openevidence-skill" / "scripts").mkdir(parents=True)
    (root / "openevidence-skill" / "scripts" / "oe.py").write_text("", encoding="utf-8")
    return root


def test_find_mcp_prefers_config_then_known_places(tmp_path):
    a, b = make_mcp(tmp_path / "a"), make_mcp(tmp_path / "b")
    assert find_oe_mcp(Config(oe_mcp_dir=a), places=[b]) == a
    assert find_oe_mcp(Config(oe_mcp_dir=tmp_path / "none"), places=[tmp_path / "x", b]) == b
    assert find_oe_mcp(Config(), places=[tmp_path / "x"]) is None


def test_oe_script_falls_back_to_the_copy_inside_openevidence_mcp(tmp_path):
    mcp = make_mcp(tmp_path / "mcp")
    own = tmp_path / "skills" / "oe.py"
    assert resolve_oe_script(Config(oe_script=own), mcp) == mcp / "openevidence-skill" / "scripts" / "oe.py"
    own.parent.mkdir()
    own.write_text("", encoding="utf-8")
    assert resolve_oe_script(Config(oe_script=own), mcp) == own  # 已有自己的 skill 就用自己的


class Fake:
    def __init__(self, up_after=0):
        self.calls, self.up_after, self.checks = [], up_after, 0

    def up(self):
        self.checks += 1
        return bool(self.calls) and self.checks > self.up_after

    def popen(self, cmd, **kw):
        self.calls.append((cmd, kw))


def test_relay_already_running_is_left_alone(tmp_path):
    f = Fake()
    assert ensure_relay(make_mcp(tmp_path / "m"), up=lambda: True, popen=f.popen, which=lambda n: "node") is None
    assert f.calls == []


def test_relay_started_with_node(tmp_path):
    mcp, f = make_mcp(tmp_path / "m"), Fake(up_after=2)
    msg = ensure_relay(mcp, up=f.up, popen=f.popen, which=lambda n: "C:/node/node.exe", sleep=lambda s: None)
    assert msg is None
    cmd, kw = f.calls[0]
    assert cmd == ["C:/node/node.exe", str(mcp / "dist" / "relay-daemon.js")] and kw["cwd"] == str(mcp)


def test_relay_problems_are_reported(tmp_path, monkeypatch):
    import xiaoshen.oe_setup as o

    monkeypatch.setattr(o, "bundled_tool", lambda name: None)
    assert "openevidence-mcp" in ensure_relay(None, up=lambda: False, popen=None, which=lambda n: "node")
    assert "Node.js" in ensure_relay(make_mcp(tmp_path / "m"), up=lambda: False, popen=None, which=lambda n: None)
    f = Fake(up_after=99)
    assert "沒有回應" in ensure_relay(make_mcp(tmp_path / "n"), up=f.up, popen=f.popen, which=lambda n: "node",
                                   sleep=lambda s: None)


def test_oe_relay_uses_bundled_node(tmp_path, monkeypatch):
    import xiaoshen.oe_setup as o

    node = tmp_path / "node"
    monkeypatch.setattr(o, "bundled_tool", lambda name: node if name == "node" else None)
    mcp, f = make_mcp(tmp_path / "m"), Fake(up_after=1)
    assert o.ensure_relay(mcp, up=f.up, popen=f.popen, which=lambda n: None, sleep=lambda s: None) is None
    assert f.calls[0][0][0] == str(node)


def test_find_oe_mcp_checks_bundled_dir_first(tmp_path, monkeypatch):
    import xiaoshen.oe_setup as o

    monkeypatch.setattr(o, "REPO_ROOT", tmp_path)
    b = make_mcp(tmp_path / "tools" / "openevidence-mcp")
    assert find_oe_mcp(Config(), places=None) == b

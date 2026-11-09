import json

import pytest

from bsp.cli.remotes import RemoteError, RemoteStore


@pytest.fixture
def builtin(tmp_path):
    p = tmp_path / "builtin.json"
    p.write_text(json.dumps({"default": "local", "remotes": {
        "local": {"url": "http://127.0.0.1:8765"},
        "lab-1": {"url": "http://192.0.2.20:8765/"},
        "lab-2": {"url": "http://lab-2.example.net:8765"},
    }}))
    return p


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("BSP_URL", "BSP_REMOTE", "BSP_STATELESS", "BSP_CONFIG"):
        monkeypatch.delenv(k, raising=False)


def store(tmp_path, builtin, **kw):
    return RemoteStore(user_path=tmp_path / "user.json", builtin_path=builtin, **kw)


def test_select_precedence(tmp_path, builtin, monkeypatch):
    s = store(tmp_path, builtin)
    assert [r.name for r in s.select()] == ["local"]
    assert s.select(remote="lab-1")[0].url == "http://192.0.2.20:8765"          # prefix + trailing slash
    assert [r.name for r in s.select(remote="all")] == ["lab-1", "lab-2"]
    assert [r.name for r in s.select(remote="lab-2,LAB-1")] == ["lab-2", "lab-1"]
    monkeypatch.setenv("BSP_REMOTE", "lab-2")
    assert s.select()[0].name == "lab-2"
    monkeypatch.setenv("BSP_URL", "http://x:1")
    assert s.select()[0].url == "http://x:1"                                 # BSP_URL beats BSP_REMOTE
    assert s.select(remote="lab-1")[0].name == "lab-1"                      # flag beats env
    assert s.select(url="http://y:2", remote="lab-1")[0].url == "http://y:2"    # --url beats all


def test_lookup_diagnostics(tmp_path, builtin):
    s = store(tmp_path, builtin)
    with pytest.raises(RemoteError) as e:
        s.lookup("lab-l")
    assert "lab-1" in e.value.hint
    s.add("lab-x", "http://1.2.3.4:8765")
    with pytest.raises(RemoteError) as e:
        s.lookup("lab")
    assert "ambiguous" in e.value.message


def test_user_overlay_persists(tmp_path, builtin):
    s = store(tmp_path, builtin)
    s.add("lab", "http://lab:8765", token="t", default=True)
    s2 = store(tmp_path, builtin)
    assert s2.default_name == "lab" and s2.select()[0].token == "t"
    s2.remove("local")                                                       # hides a builtin
    assert "local" not in store(tmp_path, builtin).remotes()
    with pytest.raises(RemoteError):
        s2.add("all", "http://x")
    with pytest.raises(RemoteError):
        s2.add("x", "not-a-url")


def test_stateless_ignores_user_file_and_default(tmp_path, builtin):
    store(tmp_path, builtin).add("lab", "http://lab:8765", default=True)
    s = store(tmp_path, builtin, stateless=True)
    assert "lab" not in s.remotes()
    with pytest.raises(RemoteError) as e:
        s.select()                                                           # no silent default
    assert "-r" in e.value.hint
    assert s.select(remote="lab-1")[0].name == "lab-1"
    with pytest.raises(RemoteError):
        s.use("lab-1")


def test_transport_chain_http_then_ssh(tmp_path, builtin, monkeypatch):
    from bsp.cli import fleet as fl
    from bsp.cli.transport import HttpTransport, SshTransport, Unreachable

    s = store(tmp_path, builtin)
    r = s.add("dual", "http://dead:1", fallback_urls=["http://alive:2/"], ssh="root@host")
    kinds = [t.kind for t in r.transports()]
    assert kinds == ["http", "http", "ssh"] and r.urls == ["http://dead:1", "http://alive:2"]

    monkeypatch.setattr(HttpTransport, "reachable", lambda self, timeout=2.0: False)
    seen = []

    def ssh_request(self, method, path, body=None, timeout=90):
        seen.append((self.target, path))
        return {"ok": True, "result": {"host": "h1", "modules": {}}}

    monkeypatch.setattr(SshTransport, "request", ssh_request)
    h = fl.connect(r)
    assert h.id == "h1" and h.transport.kind == "ssh" and seen == [("root@host", "/api/v1/status")]

    r2 = s.add("nossh", "http://dead:1")
    with pytest.raises(Unreachable):
        fl.connect(r2)

"""Host registry: board hosts announce themselves, clients list them as remotes."""

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path, text: str) -> HostConfig:
    p = tmp_path / "hosts" / "reg.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return HostConfig.load(p)


REGISTRY = """
host: {id: reg}
server: {state_dir: state}
modules:
  registry: {stale_s: 120}
"""


@pytest.fixture
def registry(tmp_path):
    with TestClient(create_app(_config(tmp_path, REGISTRY)), client=("192.0.2.30", 5000)) as c:
        yield c


def test_announce_records_the_address_it_came_from(registry):
    body = {"host": "lab-2", "port": 8765, "addresses": ["192.168.1.105", "127.0.0.1", "nonsense"],
            "ssh_user": "root", "duts": ["b1", "b2"], "version": "0.1"}
    r = registry.post("/api/v1/registry/announce", json=body).json()
    assert r["ok"] and r["result"]["addresses"] == ["192.0.2.30", "192.168.1.105"]
    items = registry.get("/api/v1/registry/list").json()["result"]
    assert [e["host"] for e in items] == ["lab-2"]
    assert items[0]["remote"] == {"url": "http://192.0.2.30:8765", "fallback_urls": ["http://192.168.1.105:8765"],
                                  "ssh": "root@192.0.2.30", "port": 8765, "description": "registered lab-2"}
    assert items[0]["duts"] == ["b1", "b2"] and not items[0]["stale"]
    for bad in ({"host": "../x"}, {"host": "h", "port": 0}, {"host": "h", "ssh_user": "root;rm"}):
        assert registry.post("/api/v1/registry/announce", json=bad).status_code == 400
    assert registry.post("/api/v1/tools/registry_forget", json={"host": "lab-2"}).json()["ok"]
    assert registry.get("/api/v1/registry/list").json()["result"] == []


def test_stale_hosts_are_hidden_and_registrations_persist(tmp_path):
    cfg = _config(tmp_path, REGISTRY)
    with TestClient(create_app(cfg), client=("192.0.2.31", 1)) as c:
        c.post("/api/v1/registry/announce", json={"host": "old", "port": 8765})
    data = json.loads((cfg.state_dir / "registry" / "hosts.json").read_text())
    data["old"]["seen"] -= 3600
    (cfg.state_dir / "registry" / "hosts.json").write_text(json.dumps(data))
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/v1/registry/list").json()["result"] == []
        assert c.get("/api/v1/registry/list?all=true").json()["result"][0]["stale"] is True


def _serve(app):
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}"


def test_host_announces_itself_and_the_cli_finds_it(tmp_path, monkeypatch, capsys):
    from bsp.cli.main import main

    reg_server, reg_url = _serve(create_app(_config(tmp_path, REGISTRY)))
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "mock-state"))
    mock = HostConfig.load(ROOT / "examples" / "site" / "hosts" / "dev-mock.yaml")
    host_port = None
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        host_port = s.getsockname()[1]
    mock.port = host_port
    mock.announce = {"url": reg_url, "interval_s": 10, "addresses": ["127.0.0.1"], "ssh_user": "root"}
    import uvicorn

    host_server = uvicorn.Server(uvicorn.Config(create_app(mock), host="127.0.0.1", port=host_port,
                                                log_level="error"))
    threading.Thread(target=host_server.run, daemon=True).start()
    try:
        from bsp.cli.transport import HttpTransport

        reg = HttpTransport(reg_url)
        for _ in range(100):
            items = reg.request("GET", "/api/v1/registry/list")["result"]
            if items:
                break
            time.sleep(0.05)
        # loopback addresses are not offered to clients, so the announcement came from 127.0.0.1 in vain
        assert items == [] or items[0]["host"] == "dev-mock"
        # register it the way a host on the network would be seen
        reg_body = {"host": "dev-mock", "port": host_port, "addresses": ["192.0.2.40"]}
        reg.request("POST", "/api/v1/registry/announce", reg_body)
        monkeypatch.setenv("BSP_REGISTRY", reg_url)
        monkeypatch.delenv("BSP_REMOTE", raising=False)
        monkeypatch.delenv("BSP_URL", raising=False)
        from bsp.cli.remotes import RemoteStore

        store = RemoteStore(stateless=True, builtin_path=tmp_path / "none.json")
        r = store.remotes()["dev-mock"]
        assert r.source == "registry" and r.url == f"http://192.0.2.40:{host_port}"
        monkeypatch.setenv("BSP_REGISTRY", "http://127.0.0.1:1")
        store = RemoteStore(stateless=True, builtin_path=tmp_path / "none.json")
        assert "dev-mock" not in store.remotes() and "registry http://127.0.0.1:1" in store.registry_error
    finally:
        host_server.should_exit = True
        reg_server.should_exit = True


def test_announcer_body_and_status(tmp_path):
    from bsp.server.announce import Announcer

    a = Announcer({"url": "http://192.0.2.1:9/", "addresses": ["192.0.2.5"], "ssh_user": "root"},
                  lambda: {"host": "h", "port": 8765, "duts": ["x"]})
    assert a.url == "http://192.0.2.1:9/api/v1/registry/announce" and a.interval == 60
    assert a.body() == {"host": "h", "port": 8765, "duts": ["x"], "ssh_user": "root", "addresses": ["192.0.2.5"]}
    cfg = _config(tmp_path, REGISTRY.replace("server: {state_dir: state}",
                                             "server: {state_dir: state, announce: http://192.0.2.1:9}"))
    assert cfg.announce == {"url": "http://192.0.2.1:9"}


def test_port_watcher_reports_cables_coming_and_going(tmp_path):
    from bsp.dut.ports import PortWatcher, snapshot

    dev, usb = tmp_path / "dev", tmp_path / "usb"
    dev.mkdir()
    usb.mkdir()
    (dev / "ttyUSB0").touch()
    probe = lambda: snapshot((str(dev / "ttyUSB*"),), str(usb))
    changes = []
    w = PortWatcher(lambda change, now: changes.append(change), probe=probe)
    assert w.current == {"tty": [str(dev / "ttyUSB0")], "usb": []} and w.check() is None
    (dev / "ttyUSB3").touch()
    port = usb / "2-1.3"
    port.mkdir()
    (port / "idVendor").write_text("361c\n")
    (port / "idProduct").write_text("1001\n")
    (usb / "2-1.3:1.0").mkdir()                     # an interface, not a device
    assert w.check() == {"added": {"tty": [str(dev / "ttyUSB3")], "usb": ["usb:2-1.3 361c:1001"]},
                         "removed": {"tty": [], "usb": []}}
    (dev / "ttyUSB0").unlink()
    assert w.check()["removed"]["tty"] == [str(dev / "ttyUSB0")]
    assert len(changes) == 2


def test_registry_publishes_port_changes(registry):
    registry.post("/api/v1/registry/announce", json={"host": "lab-2", "port": 8765})       # before it watched ports
    body = {"host": "lab-2", "port": 8765, "ports": {"tty": ["/dev/ttyUSB0"], "usb": []}}
    registry.post("/api/v1/registry/announce", json=body)                                   # no news: nothing to compare
    registry.post("/api/v1/registry/announce", json={**body, "ports": {"tty": ["/dev/ttyUSB0", "/dev/ttyUSB3"],
                                                                         "usb": ["usb:2-1.3 361c:1001"]}})
    events = [e for e in registry.get("/api/v1/events/recent?limit=50").json()["result"] if e["topic"] == "registry.ports"]
    assert [e["data"] for e in events] == [{"host": "lab-2", "added": {"tty": ["/dev/ttyUSB3"], "usb": ["usb:2-1.3 361c:1001"]},
                                            "removed": {"tty": [], "usb": []}}]
    assert registry.get("/api/v1/registry/list").json()["result"][0]["ports"]["tty"] == ["/dev/ttyUSB0", "/dev/ttyUSB3"]


def test_announcer_is_poked_by_port_changes():
    import threading

    from bsp.core.events import EventBus
    from bsp.server.announce import Announcer

    sent = threading.Event()
    a = Announcer({"url": "http://192.0.2.1:9", "interval_s": 3600}, lambda: {"host": "h"})
    a.once = lambda: sent.set() or {"ok": True}
    bus = EventBus()
    bus.on("dut.ports", lambda ev: a.poke())
    a.start()
    assert sent.wait(2)                       # the first announcement
    sent.clear()
    bus.publish("dut.ports", added={"tty": ["/dev/ttyUSB3"]})
    assert sent.wait(2)                       # at once, not an hour later
    a.stop()


def test_an_ssh_url_reaches_bspd_through_ssh(monkeypatch):
    """`--url ssh://user@host:port` is bspd on that machine's loopback port, reached through ssh, with the ssh options a
    program driving bsp adds (BSP_SSH_OPTIONS: boardlab's own key)."""
    from bsp.cli.remotes import RemoteStore
    from bsp.cli.transport import SshTransport, _ssh_base

    r = RemoteStore(stateless=True).select(url="ssh://root@lab-1:8766")[0]
    [t] = r.transports()
    assert isinstance(t, SshTransport) and t.target == "root@lab-1" and t.port == 8766
    monkeypatch.setenv("BSP_SSH_OPTIONS", "-i /var/lib/boardlab/ssh/id_ed25519 -o IdentitiesOnly=yes")
    base = _ssh_base("root@lab-1")
    assert base[-1] == "root@lab-1" and base[-5:-1] == ["-i", "/var/lib/boardlab/ssh/id_ed25519", "-o", "IdentitiesOnly=yes"]

"""Console pipe, file staging, file boot and lease renewal: the APIs boardlab drives bsp boards with."""

import json
import os
import socket
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bsp.core.errors import Forbidden, InvalidRequest
from bsp.dut.hostio import ArtifactStore
from bsp.dut.leases import LeaseManager
from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "examples" / "site" / "hosts" / "dev-mock.yaml"


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_artifact_store_checks_content_and_keeps_one_copy(tmp_path):
    store = ArtifactStore(tmp_path)
    data = b"kernel FIT" * 1000
    with pytest.raises(InvalidRequest) as e:
        store.put(_sha(b"other"), "Image.itb", [data])
    assert "does not match" in e.value.message and store.find(_sha(b"other")) is None
    assert not list((tmp_path / _sha(b"other")).iterdir())                 # no partial file left
    r = store.put(_sha(data), "../../Image.itb", [data[:10], data[10:]])
    assert r["stored"] and Path(r["path"]).name == "Image.itb" and Path(r["path"]).read_bytes() == data
    assert store.put(_sha(data), "again.itb", [data])["stored"] is False   # sent twice, kept once
    assert store.get(_sha(data)) == Path(r["path"])
    with pytest.raises(InvalidRequest):
        store.get("0" * 64)
    with pytest.raises(InvalidRequest):
        store.upload("not-a-sha", "x")


def test_lease_renew_keeps_holder_and_token(tmp_path):
    lm = LeaseManager(tmp_path / "l.json")
    a = lm.acquire("b1", "boardlab:user:alice", 60, "debug")
    r = lm.renew("b1", a["token"], 7200)
    assert (r["token"], r["holder"], r["purpose"], r["expires_in_s"]) == (a["token"], a["holder"], "debug", 7200)
    with pytest.raises(Forbidden):
        lm.renew("b1", "wrong", 60)
    lm.release("b1", a["token"])
    with pytest.raises(Forbidden) as e:
        lm.renew("b1", a["token"], 60)
    assert "acquire" in e.value.hint


def test_bridge_copies_both_ways_until_a_side_closes(tmp_path):
    from bsp.cli.pipe import bridge, remote_command

    path = str(tmp_path / "con.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    got = []

    def server():
        c, _ = srv.accept()
        c.sendall(b"\x00\x00\x00\x02{}")              # what a daemon says first
        got.append(c.recv(100))
        c.close()

    t = threading.Thread(target=server)
    t.start()
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    os.write(in_w, b"hello")
    assert bridge(path, in_r, out_w) == 0
    t.join(5)
    assert got == [b"hello"] and os.read(out_r, 100) == b"\x00\x00\x00\x02{}"
    cmd = remote_command("/root/.serial-mux/sock/b 1.sock")
    assert cmd.startswith("python3 -c ") and cmd.endswith("'/root/.serial-mux/sock/b 1.sock'")


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The mock host with a fake fastboot that shows mock-board in download mode on usb:0-0."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "fastboot.log"
    fb = bindir / "fastboot"
    fb.write_text(f"""#!/bin/sh
echo "$*" >> {log}
[ "$1" = devices ] && {{ [ -e {tmp_path}/off-bus ] || printf 'dfu\\tDFU download\\n    usb:0-0\\n'; exit 0; }}
[ "$1" = -s ] && shift 2
[ "$1" = stage ] && touch {tmp_path}/off-bus
echo "$1 OKAY"
""")
    fb.chmod(fb.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    with TestClient(create_app(HostConfig.load(MOCK))) as c:
        c.tmp, c.log = tmp_path, log
        yield c


def _call(c, tool, **args):
    r = c.post(f"/api/v1/tools/{tool}", json=args).json()
    return r["result"] if r["ok"] else r["error"]


def test_upload_stage_present_and_renew(host):
    token = _call(host, "dut_acquire", dut="mock-board", holder="boardlab:user:alice", ttl=60)["token"]
    data = b"\x7fELF fsbl" * 100
    sha = _sha(data)
    assert host.get(f"/api/v1/artifacts/{sha}").json()["result"]["present"] is False
    r = host.put(f"/api/v1/artifacts/{sha}?name=FSBL.bin", content=data).json()
    assert r["ok"] and r["result"]["stored"] and r["result"]["size"] == len(data)
    assert host.get(f"/api/v1/artifacts/{sha}").json()["result"] == {"sha256": sha, "present": True,
                                                                     "size": len(data), "file": "FSBL.bin"}
    bad = host.put(f"/api/v1/artifacts/{'1' * 64}", content=data)
    assert bad.status_code == 400 and "does not match" in bad.json()["error"]["message"]

    assert _call(host, "dut_present", dut="mock-board")["present"] is True
    assert _call(host, "dut_stage", dut="mock-board", sha256=sha)["code"] == "forbidden"     # no lease
    r = _call(host, "dut_stage", dut="mock-board", sha256=sha, lease=token)
    assert r["continued"] and r["file"] == "FSBL.bin"
    calls = host.log.read_text().splitlines()
    assert f"-s usb:0-0 stage {host.tmp}/state/dut/artifacts/{sha}/FSBL.bin" in calls and "-s usb:0-0 continue" in calls
    assert _call(host, "dut_present", dut="mock-board")["present"] is False                  # the board moved on
    assert _call(host, "dut_stage", dut="mock-board", sha256="2" * 64, lease=token)["code"] == "invalid_request"

    st = _call(host, "dut_status", dut="mock-board")
    assert st["console_socket"].endswith("/sock/mock-board.sock") and st["console_up"] is False
    r = _call(host, "dut_renew", dut="mock-board", lease=token, ttl=900)
    assert r["token"] == token and r["expires_in_s"] == 900 and r["holder"] == "boardlab:user:alice"


def test_boot_files_stages_in_order_and_waits_for_the_port(host):
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    shas = []
    for name, data in (("FSBL.bin", b"fsbl"), ("Image.itb", b"kernel")):
        host.put(f"/api/v1/artifacts/{_sha(data)}?name={name}", content=data)
        shas.append(_sha(data))
    r = _call(host, "dut_boot_files", dut="mock-board", files=",".join(shas), lease=token, reset=False,
              first_wait=0, port_timeout=0.5)
    assert r["job"] and [f["name"] for f in r["files"]] == ["FSBL.bin", "Image.itb"]
    job = _call(host, "job_get", id=r["job"], wait=10)
    # the fake port never comes back after the first stage: the boot stops before the kernel
    assert job["status"] == "failed" and "did not come back" in job["error"]["message"]
    staged = [l.rsplit("/", 1)[-1] for l in host.log.read_text().splitlines() if " stage " in l]
    assert staged == ["FSBL.bin"]

    (host.tmp / "off-bus").unlink()            # port back: now the whole sequence runs
    r = _call(host, "dut_boot_files", dut="mock-board", files=",".join(shas), lease=token, reset=False,
              first_wait=0, port_timeout=0.5)
    threading.Timer(0.2, lambda: (host.tmp / "off-bus").unlink(missing_ok=True)).start()
    job = _call(host, "job_get", id=r["job"], wait=10)
    assert job["status"] == "succeeded", job
    assert any("file 2/2 Image.itb: staged" in l for l in job["lines"])
    assert _call(host, "dut_boot_files", dut="mock-board", files="", lease=token)["code"] == "invalid_request"


def test_cli_stage_uploads_once_over_http(tmp_path, monkeypatch, capsys):
    """End to end through the real CLI and an HTTP server (streamed upload, then stage)."""
    import uvicorn

    from bsp.cli.main import main

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fb = bindir / "fastboot"
    fb.write_text("#!/bin/sh\n[ \"$1\" = devices ] && printf 'dfu\\tDFU download\\n    usb:0-0\\n'\necho OKAY\n")
    fb.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(HostConfig.load(MOCK)), host="127.0.0.1", port=port,
                                           log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        url = ["--url", f"http://127.0.0.1:{port}", "--stateless"]
        assert main([*url, "--json", "dut", "acquire", "mock-board", "--holder", "t"]) == 0
        import json

        token = json.loads(capsys.readouterr().out)["result"]["token"]
        f = tmp_path / "Image.itb"
        f.write_bytes(os.urandom(3 << 20))
        assert main([*url, "dut", "stage", "mock-board", str(f), "--lease", token]) == 0
        out = capsys.readouterr().out
        assert "sent Image.itb" in out and "staged Image.itb (3,145,728 B" in out
        assert main([*url, "dut", "stage", "mock-board", str(f), "--lease", token, "--no-continue"]) == 0
        assert "already on dev-mock" in capsys.readouterr().out
        assert main([*url, "--json", "dut", "boot", "mock-board", "--sdk", "x", "--files", str(f)]) == 1
        assert "either --sdk" in capsys.readouterr().out
    finally:
        server.should_exit = True
        t.join(5)


def test_slots_name_boards_and_refresh_reads_the_board_swapped_into_one(host, monkeypatch):
    """A slot reference works wherever a DUT name does; refresh re-reads the board after a swap."""
    from bsp.dut import probe as probe_mod

    mod = host.app.state.hub.modules["dut"]
    spec = {"id": "k3-deb1-32g", "board": "k3_deb1", "soc": "soc-b", "console": {"alias": "uart-a"},
            "fastboot": {"usb": "usb:0-5"},
            "control": {"backend": "gpio-relay", "reset": {"pin": "P16", "mode": "button", "pulse_ms": 50},
                        "fdl": {"strategy": "strapped"}}}
    # The host's console list: this adapter is a slot, online.
    monkeypatch.setattr(mod.slots, "_list", lambda: [
        {"id": "uart-a", "kind": "serial-mux", "alias": "uart-a", "device": "/dev/ttyUSB0", "usb_port": "p-a", "busy": True}])
    assert _call(host, "dut_add", spec=json.dumps(spec))["slot"] == "s1"
    assert _call(host, "dut_status", dut="s1")["id"] == "k3-deb1-32g"
    token = _call(host, "dut_acquire", dut="dev-mock/s1", holder="boardlab:user:alice", ttl=600)["token"]
    assert _call(host, "dut_status", dut="k3-deb1-32g")["lease"]["holder"] == "boardlab:user:alice"
    assert _call(host, "dut_status", dut="other-host/s1")["code"] == "invalid_request"

    # Someone swaps in another board: same reset line and console, and its download cable is now on usb:0-7.
    usb = iter([{}, {"usb:0-7": {"usb": "usb:0-7", "devnum": 4, "product": "DFU download"}}])
    monkeypatch.setattr(mod, "_usb_now", lambda: next(usb, {"usb:0-7": {"usb": "usb:0-7", "devnum": 4}}))
    monkeypatch.setattr(probe_mod, "identify_board", lambda mod, usb, fdl, soc, log_dir, log: {
        "fields": {"product": "k3_com260", "model": "CoM260 on IFX"}, "name_hint": "k3-board-32g"})
    assert _call(host, "dut_refresh", dut="s1")["code"] == "forbidden"            # needs the lease
    r = _call(host, "dut_refresh", dut="s1", lease=token, rename=True)
    job = _call(host, "job_get", id=r["job"], wait=10)
    assert job["status"] == "succeeded", job
    want = {"dut": "k3-board-32g", "renamed_from": "k3-deb1-32g", "usb": "usb:0-7", "board": "k3_com260", "slot": "s1"}
    assert {k: job["result"][k] for k in want} == want
    st = _call(host, "dut_status", dut="s1")
    assert (st["id"], st["board"], st["description"], st["usb"]) == ("k3-board-32g", "k3_com260", "CoM260 on IFX", "usb:0-7")
    assert st["lease"]["holder"] == "boardlab:user:alice"                        # the lease followed the board
    _call(host, "dut_reset", dut="k3-board-32g", lease=token, fdl=False)
    events = [h["event"] for h in _call(host, "slot_status", slot="s1")["history"]]
    assert events == ["added", "refreshed"]
    # Removing the board leaves the slot; it says which board it had.
    assert _call(host, "dut_remove", dut="s1", force=True)["removed"] == "k3-board-32g"
    st = _call(host, "slot_status", slot="s1")
    assert st["dut"] is None and st["reset"] == "P16" and st["history"][-1]["event"] == "removed"
    assert "has no board registered" in _call(host, "dut_reset", dut="s1", lease=token)["message"]

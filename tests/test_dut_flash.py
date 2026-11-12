"""`dut flash`: packages fetched or uploaded, unpacked safely, written by a flash hook with progress."""

import hashlib
import http.server
import io
import json
import os
import socket
import stat
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bsp.core.errors import InvalidRequest
from bsp.dut import flashpkg
from bsp.dut.hostio import ArtifactStore
from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "examples" / "site" / "hosts" / "dev-mock.yaml"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tgz(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        for name, target in (links or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type, ti.linkname = tarfile.SYMTYPE, target
            t.addfile(ti)
    return buf.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def web(tmp_path):
    """A local HTTP server for files in tmp_path/www."""
    root = tmp_path / "www"
    root.mkdir()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.root, srv.url = root, f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


# ---------------------------------------------------------------- package --
def test_urls_other_than_http_are_refused():
    for bad in ("file:///etc/passwd", "ftp://h/p.tgz", "http:///p.tgz", "http://h/a b.tgz", "http://h/\n",
                "javascript:alert(1)", "/local/path"):
        with pytest.raises(InvalidRequest):
            flashpkg.check_url(bad)
    assert flashpkg.check_url("https://h.example/p.tgz?x=1") == "https://h.example/p.tgz?x=1"


def test_fetch_downloads_once_and_checks_the_hash(tmp_path, web):
    store, lines = ArtifactStore(tmp_path / "art"), []
    data = _tgz({"pkg/fastboot.yaml": b"actions: []\n"})
    (web.root / "pkg.tgz").write_bytes(data)
    with pytest.raises(InvalidRequest) as e:
        flashpkg.fetch(store, f"{web.url}/pkg.tgz", "0" * 64, lines.append)
    assert "hashes to" in e.value.message and store.find(_sha(data)) is None
    assert not list((tmp_path / "art" / ".incoming").iterdir())          # nothing half-written left
    r = flashpkg.fetch(store, f"{web.url}/pkg.tgz", _sha(data), lines.append)
    assert r["fetched"] and Path(r["path"]).read_bytes() == data and Path(r["path"]).name == "pkg.tgz"
    assert flashpkg.fetch(store, f"{web.url}/gone.tgz", _sha(data), lines.append)["fetched"] is False  # cached
    with pytest.raises(Exception) as e:
        flashpkg.fetch(store, f"{web.url}/missing.tgz", None, lines.append)
    assert "404" in str(e.value)
    with pytest.raises(InvalidRequest):
        flashpkg.fetch(store, f"{web.url}/pkg.tgz", None, lines.append, max_bytes=10)


@pytest.mark.parametrize("bad", [
    _tgz({"../evil": b"x"}),
    _tgz({"/abs/evil": b"x"}),
    _tgz({"pkg/a": b"x"}, links={"pkg/link": "/etc/passwd"}),
    _zip({"../../evil": b"x"}),
    _zip({"/abs": b"x"}),
])
def test_unpack_refuses_entries_outside_the_package(tmp_path, bad):
    p = tmp_path / "bad.pkg"
    p.write_bytes(bad)
    with pytest.raises(InvalidRequest):
        flashpkg.unpack(p, tmp_path / "out")
    assert not (tmp_path / "evil").exists()


def test_unpack_descends_into_one_top_level_directory(tmp_path):
    for i, data in enumerate((_tgz({"pkg/fastboot.yaml": b"a", "pkg/img/u-boot.itb": b"b"}),
                              _zip({"pkg/fastboot.yaml": b"a", "pkg/img/u-boot.itb": b"b"}))):
        p = tmp_path / f"p{i}"
        p.write_bytes(data)
        d = flashpkg.unpack(p, tmp_path / f"out{i}")
        assert d.name == "pkg" and (d / "fastboot.yaml").read_bytes() == b"a" and (d / "img" / "u-boot.itb").exists()
    p = tmp_path / "flat"
    p.write_bytes(_tgz({"fastboot.yaml": b"a", "u-boot.itb": b"b"}))
    assert flashpkg.unpack(p, tmp_path / "flat-out") == tmp_path / "flat-out"
    p.write_bytes(b"not an archive")
    with pytest.raises(InvalidRequest):
        flashpkg.unpack(p, tmp_path / "x")


# ---------------------------------------------------------------- dut.flash --
FAKE_FLASHER = """
import os, time
pkg = req['package_dir']
with open(os.environ['FLASH_LOG'], 'w') as f:
    json.dump({**req, 'files': sorted(os.listdir(pkg))}, f)
print('flashing ' + req['usb'], flush=True)
for n in (10, 55, 100):
    print(json.dumps({'progress': n, 'msg': 'part %d' % n}), flush=True)
    time.sleep(float(os.environ.get('FLASH_SLEEP', '0')))
if os.environ.get('FLASH_FAIL'):
    print(json.dumps({'flashed': ['bootfs'], 'error': 'partition rootfs did not take', 'hint': 'check the cable'}))
    sys.exit(2)
print(json.dumps({'flashed': ['bootfs', 'rootfs']}))
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The mock host (mock-board in download mode on usb:0-0) with a fake flash hook `fake`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fb = bindir / "fastboot"
    fb.write_text("#!/bin/sh\n[ \"$1\" = devices ] && printf 'dfu\\tDFU download\\n    usb:0-0\\n'\necho OKAY\n")
    fb.chmod(0o755)
    hooks = tmp_path / "hooks" / "flash"
    hooks.mkdir(parents=True)
    h = hooks / "fake"
    h.write_text("#!/usr/bin/env python3\nimport json,sys\nreq=json.load(sys.stdin)\n" + FAKE_FLASHER)
    h.chmod(h.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_HOOKS_PATH", str(tmp_path / "hooks"))
    monkeypatch.setenv("FLASH_LOG", str(tmp_path / "flash.json"))
    with TestClient(create_app(HostConfig.load(MOCK))) as c:
        c.tmp = tmp_path
        yield c


def _call(c, tool, **args):
    r = c.post(f"/api/v1/tools/{tool}", json=args).json()
    return r["result"] if r["ok"] else r["error"]


def test_flasher_choice_order(host):
    mod = host.app.state.hub.modules["dut"]
    b = mod.board("mock-board")
    assert mod.flasher_for(b) == ("spacemit-pkg", "the default")
    mod.socs.profiles.setdefault("soc-b", {})["flash"] = {"flasher": "soc-one"}
    assert mod.flasher_for(b)[0] == "soc-one"
    b.spec.raw["flash"] = {"flasher": "board-one"}
    assert mod.flasher_for(b)[0] == "board-one"
    assert mod.flasher_for(b, "asked") == ("asked", "asked for")


def test_flash_uploaded_package_runs_the_hook_with_progress(host):
    # Host env, network and a product directory are in the hook's request; args expand from the env.
    mod = host.app.state.hub.modules["dut"]
    mod.network_cfg = {"board_subnet": "10.0.0.0/24", "host_address": "10.0.0.2",
                       "export_address": "203.0.113.5"}
    mod._network = None
    mod.ctx.env.update({"BSP_TFTP_SERVER": "10.0.0.9", "BSP_SDK_OUTPUT": "/srv/sdk"})
    mod.flash_cfg["args"] = {"tftp": "${BSP_TFTP_SERVER}/pkg", "fallback": "${NOPE:-none}"}
    data = _tgz({"pkg/fastboot.yaml": b"actions: []\n", "pkg/partition_universal.json": b"{}",
                 "pkg/rootfs.ext4": b"r" * 1000})
    sha = _sha(data)
    assert host.put(f"/api/v1/artifacts/{sha}?name=pkg.tgz", content=data).json()["ok"]
    assert _call(host, "dut_flash", dut="mock-board", package=sha, flasher="fake")["code"] == "forbidden"
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    e = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="nope", lease=token)
    assert e["code"] == "not_found" and "fake" in e["hint"]
    assert _call(host, "dut_flash", dut="mock-board", package="ftp://h/p.tgz", lease=token)["code"] == "invalid_request"
    assert _call(host, "dut_flash", dut="mock-board", package="1" * 64, lease=token)["code"] == "invalid_request"

    r = _call(host, "dut_flash", dut="mock-board", package=sha, flasher="fake", lease=token, reset=False)
    assert r["flasher"] == "fake" and r["package"] == "pkg.tgz"
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "succeeded", job
    assert job["kind"] == "flash" and job["progress"] == 100
    assert job["result"]["flashed"] == ["bootfs", "rootfs"] and job["result"]["flasher"] == "fake"
    assert any("55%" in l for l in job["lines"]) and any("flashing usb:0-0" in l for l in job["lines"])
    req = json.loads((host.tmp / "flash.json").read_text())
    assert req["files"] == ["fastboot.yaml", "partition_universal.json", "rootfs.ext4"]
    assert req["timeout"] == 3600
    assert req["usb"] == "usb:0-0" and req["dut"] == "mock-board" and req["artifacts"]["package"].endswith("pkg.tgz")
    assert req["product_dir"] == req["package_dir"]              # the unpacked package is the product
    assert req["env"] == {"BSP_TFTP_SERVER": "10.0.0.9", "BSP_SDK_OUTPUT": "/srv/sdk"}
    assert req["network"] == {"board_subnet": "10.0.0.0/24", "host_address": "10.0.0.2",
                              "export_address": "203.0.113.5", "console": "mock-board"}
    assert req["args"] == {"tftp": "10.0.0.9/pkg", "fallback": "none"}
    assert not Path(req["package_dir"]).exists()                  # the unpacked copy is gone afterwards
    assert any(j["id"] == r["job"] and j["progress"] == 100 for j in _call(host, "job_list"))


def test_flash_downloads_a_url_and_reports_the_hooks_error(host, web, monkeypatch):
    data = _zip({"fastboot.yaml": b"actions: []\n", "u-boot.itb": b"u"})
    (web.root / "p.zip").write_bytes(data)
    token = _call(host, "dut_acquire", dut="mock-board", holder="t", ttl=60)["token"]
    r = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", sha256="0" * 64, flasher="fake",
              lease=token, reset=False)
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "failed" and "hashes to" in job["error"]["message"]

    monkeypatch.setenv("FLASH_FAIL", "1")
    r = _call(host, "dut_flash", dut="mock-board", package=f"{web.url}/p.zip", sha256=_sha(data), flasher="fake",
              lease=token, reset=False)
    job = _call(host, "job_get", id=r["job"], wait=20)
    assert job["status"] == "failed" and "rootfs did not take" in job["error"]["message"]
    assert job["error"]["hint"] == "check the cable"
    req = json.loads((host.tmp / "flash.json").read_text())
    assert req["files"] == ["fastboot.yaml", "u-boot.itb"] and req["artifacts"]["flash-zip"].endswith("p.zip")


def test_cli_flash_sends_a_directory_and_prints_progress(tmp_path, monkeypatch, capsys):
    import uvicorn

    from bsp.cli.main import main

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fb = bindir / "fastboot"
    fb.write_text("#!/bin/sh\n[ \"$1\" = devices ] && printf 'dfu\\tDFU download\\n    usb:0-0\\n'\necho OKAY\n")
    fb.chmod(0o755)
    hooks = tmp_path / "hooks" / "flash"
    hooks.mkdir(parents=True)
    (hooks / "fake").write_text("#!/usr/bin/env python3\nimport json,sys\nreq=json.load(sys.stdin)\n" + FAKE_FLASHER)
    (hooks / "fake").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BSP_HOOKS_PATH", str(tmp_path / "hooks"))
    monkeypatch.setenv("FLASH_LOG", str(tmp_path / "flash.json"))
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
        token = json.loads(capsys.readouterr().out)["result"]["token"]
        pkg = tmp_path / "my-image"
        (pkg / "img").mkdir(parents=True)
        (pkg / "fastboot.yaml").write_text("actions: []\n")
        (pkg / "img" / "rootfs.ext4").write_bytes(b"r" * 4096)
        assert main([*url, "--json", "dut", "flash", "mock-board", "--package", str(pkg), "--flasher", "fake",
                     "--lease", token, "--no-reset"]) == 0
        cap = capsys.readouterr()
        assert "sent my-image.tar" in cap.err and "55%" in cap.err
        res = json.loads(cap.out)["result"]
        assert res["flasher"] == "fake" and res["flashed"] == ["bootfs", "rootfs"] and res["job"]
        assert any("100%" in l for l in res["lines"]) and isinstance(res["elapsed_s"], (int, float))
        req = json.loads((tmp_path / "flash.json").read_text())
        assert req["files"] == ["fastboot.yaml", "img"]
        assert main([*url, "dut", "flash", "mock-board", "--package", str(pkg), "--flasher", "fake",
                     "--lease", token, "--no-reset"]) == 0
        assert "my-image.tar already on dev-mock" in capsys.readouterr().out     # same tree, same tar
        assert main([*url, "dut", "flash", "mock-board", "--package", "file:///etc/x.tgz", "--lease", token]) == 1
        assert "not allowed" in capsys.readouterr().err
    finally:
        server.should_exit = True
        t.join(5)


def test_a_running_job_can_be_cancelled(host):
    """`dut cancel` asks the running job to stop: it ends before its next step, with status cancelled."""
    import time

    from bsp.core.jobs import JobManager
    from bsp.core.events import EventBus

    bus = EventBus()
    jobs = JobManager(bus)
    step = {"n": 0}

    def body(log):
        for _ in range(100):
            log.check()
            step["n"] += 1
            time.sleep(0.02)

    job = jobs.start("boot", "d1: boot", body, dut="d1")
    for _ in range(100):
        if step["n"] >= 2:
            break
        time.sleep(0.02)
    job.cancel()
    jobs.wait(job.id, 5)
    assert job.status == "cancelled" and step["n"] < 100

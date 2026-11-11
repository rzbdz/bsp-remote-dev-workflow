"""Flash packages: downloaded into the artifact cache, unpacked without reaching outside."""

import hashlib
import http.server
import io
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from bsp.core.errors import InvalidRequest
from bsp.dut import flashpkg
from bsp.dut.hostio import ArtifactStore

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

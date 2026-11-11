"""`site.reload`: a host reads its site again after a tool that manages it shipped a new copy."""

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]


def _sdks(c) -> list[str]:
    return [s["id"] for s in c.post("/api/v1/tools/sdk_list", json={}).json()["result"]]


def test_site_reload_takes_a_site_shipped_after_start(tmp_path, monkeypatch):
    """A host started with an empty site picks up the SDK catalog and SoC profiles shipped later; a catalog that does
    not load is refused and the running one kept."""
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path / "state"))
    site = tmp_path / "site"
    p = site / "hosts" / "h.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("host: {id: h}\nserver: {state_dir: ../state}\nmodules:\n  sdk: {}\n"
                 "  dut: {probe: {pulse_ms: 200}, slots: {auto_console: false}}\n")
    with TestClient(create_app(HostConfig.load(p))) as c:
        assert _sdks(c) == []
        shutil.copytree(ROOT / "examples" / "site" / "sdk", site / "sdk")
        shutil.copytree(ROOT / "examples" / "site" / "socs", site / "socs")
        r = c.post("/api/v1/tools/site_reload", json={}).json()
        assert r["ok"] and "bsp-a" in r["result"]["sdks"] and {"soc-a", "soc-b"} <= set(r["result"]["socs"])
        assert "bsp-a" in _sdks(c)
        (site / "sdk" / "instances" / "broken.yaml").write_text("id: broken\nfamily: make-bsp\n")
        r = c.post("/api/v1/tools/site_reload", json={})
        assert r.status_code == 400 and "boot" in r.json()["error"]["message"]
        assert "bsp-a" in _sdks(c) and "broken" not in _sdks(c)

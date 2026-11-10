"""Reading a console's log over a time range, with a search: what export and search in front ends are built on."""
from pathlib import Path

from bsp.dut.hostio import SerialConsole


def console(tmp_path: Path) -> SerialConsole:
    logs = tmp_path / "logs" / "s1"
    logs.mkdir(parents=True)
    (logs / "2026-11-09.log").write_text(
        "[2026-11-09 23:59:58] U-Boot SPL 2022.10\n"
        "[2026-11-09 23:59:59] DDR: 8 GiB\n")
    (logs / "2026-11-10.log").write_text(
        "[2026-11-10 00:00:01] Starting kernel ...\n"
        "a line printed without a stamp of its own\n"
        "[2026-11-10 00:00:05] ERROR: mmc0 timeout\n"
        "[2026-11-10 09:00:00] root@k1:~# \n")
    return SerialConsole("s1", log_dir=str(tmp_path / "logs"))


def test_a_range_spans_day_files_and_keeps_unstamped_lines_with_the_one_before(tmp_path):
    c = console(tmp_path)
    r = c.range("2026-11-09 23:59:59", "2026-11-10 00:00:05")
    assert r["lines"] == ["[2026-11-09 23:59:59] DDR: 8 GiB", "[2026-11-10 00:00:01] Starting kernel ...",
                          "a line printed without a stamp of its own", "[2026-11-10 00:00:05] ERROR: mmc0 timeout"]
    assert r["truncated"] is False and r["matched"] == 4


def test_a_search_matches_text_case_blind_or_a_regex(tmp_path):
    c = console(tmp_path)
    assert c.range(None, None, grep="error")["lines"] == ["[2026-11-10 00:00:05] ERROR: mmc0 timeout"]
    assert c.range(None, None, grep=r"^\[.*\] (U-Boot|DDR)", regex=True)["matched"] == 2


def test_a_range_is_capped_and_says_so(tmp_path):
    c = console(tmp_path)
    r = c.range(None, None, limit=2)
    assert r["lines"] == ["[2026-11-10 00:00:05] ERROR: mmc0 timeout", "[2026-11-10 09:00:00] root@k1:~# "]
    assert r["truncated"] is True and r["matched"] == 6
    assert c.range(None, None, limit=2, first=True)["lines"][0] == "[2026-11-09 23:59:58] U-Boot SPL 2022.10"


def test_hosts_serve_a_dut_s_log_range(tmp_path, monkeypatch):
    """`dut.logs` over the host API: the DUT's console log, ranged and searched."""
    import json

    from fastapi.testclient import TestClient

    from bsp.server.app import create_app
    from bsp.server.config import HostConfig

    root = Path(__file__).resolve().parents[1]
    hosts = tmp_path / "site" / "hosts"
    hosts.mkdir(parents=True)
    for d in ("sdk", "socs", "boards", "duts"):
        (tmp_path / "site" / d).symlink_to(root / "examples" / "site" / d)
    (hosts / "h.yaml").write_text("host: {id: h}\nserver: {state_dir: state}\n"
                                  "modules: {gpio_relay: {board: example-40pin, backend: mock, pinmux: false}, dut: {serial_log_dir: %s}}\n" % (tmp_path / "logs"))
    console(tmp_path)
    with TestClient(create_app(HostConfig.load(hosts / "h.yaml"))) as c:
        mod = c.app.state.hub.modules["dut"]
        spec = {"id": "k1-a", "board": "k1", "soc": "soc-a", "console": {"alias": "s1"}, "fastboot": {"usb": "usb:0-5"},
                "control": {"backend": "human", "fdl": {"strategy": "strapped"}}}
        monkeypatch.setattr(mod.slots, "_list", lambda: [])
        assert c.post("/api/v1/tools/dut_add", json={"spec": json.dumps(spec)}).json()["ok"]
        r = c.post("/api/v1/tools/dut_logs", json={"dut": "k1-a", "grep": "mmc0"}).json()["result"]
        assert r["lines"] == ["[2026-11-10 00:00:05] ERROR: mmc0 timeout"] and r["truncated"] is False
        bad = c.post("/api/v1/tools/dut_logs", json={"dut": "k1-a", "grep": "(", "regex": True}).json()
        assert bad["error"]["code"] == "invalid_request"

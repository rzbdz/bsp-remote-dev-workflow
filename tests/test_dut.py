import json
import sys
import os
import stat
from pathlib import Path

import time
import pytest

from bsp.core.errors import Conflict, Forbidden, HardwareError, HardwareTimeout, InvalidRequest, NotFound
from bsp.core.hooks import HookRunner
from bsp.dut.boot import BootRunner
from bsp.dut.catalog import DutRegistry
from bsp.dut.leases import LeaseManager
from bsp.sdk.catalog import SdkCatalog

ROOT = Path(__file__).resolve().parents[1]
SPEC = {"id": "b1", "board": "board-x", "soc": "soc-b", "console": {"alias": "board1"},
        "fastboot": {"usb": "usb:2-1.2"}, "control": {"backend": "gpio-relay", "reset": {"pin": "P33"},
                                                     "fdl": {"strategy": "strapped"}}, "sdks": ["upstream-a"]}


def test_leases(tmp_path):
    lm = LeaseManager(tmp_path / "l.json")
    a = lm.acquire("b1", "alice", 600, "test")
    with pytest.raises(Conflict) as e:
        lm.acquire("b1", "bob", 600)
    assert "alice" in e.value.message and "another" in e.value.hint
    assert lm.acquire("b1", "alice", 900, token=a["token"])["token"] == a["token"]   # renew
    with pytest.raises(Forbidden):
        lm.check("b1", "wrong")
    lm.check("b1", a["token"])
    assert LeaseManager(tmp_path / "l.json").view("b1")["holder"] == "alice"           # persisted
    with pytest.raises(Forbidden):
        lm.release("b1", "wrong")
    assert lm.release("b1", a["token"])["released"]
    with pytest.raises(Forbidden) as e:
        lm.check("b1", None)
    assert "acquire" in e.value.hint


def test_registry_add_conflicts_and_persists(tmp_path):
    reg = DutRegistry("hostA", None, tmp_path / "duts.json")
    reg.add(SPEC)
    with pytest.raises(Conflict):
        reg.add({**SPEC, "id": "b2"})                       # same usb/console
    reg.add({**SPEC, "id": "b2", "fastboot": {"usb": "usb:3-1"}, "console": {"alias": "board2"}})
    again = DutRegistry("hostA", None, tmp_path / "duts.json")
    assert sorted(again.all()) == ["b1", "b2"] and again.all()["b1"].host == "hostA"
    again.remove("b1")
    with pytest.raises(InvalidRequest):
        again.add({"id": "x"})


# ------------------------------------------------------------ boot runner --
class FakeFastboot:
    usb, binary = "usb:2-1.2", "fastboot"

    def __init__(self, console):
        self.console, self.product, self.calls = console, "DFU download", []

    def state(self):
        return {"product": self.product, "usb": self.usb} if self.product else None

    def cmd(self, *args, timeout=0):
        self.calls.append(args)
        if args[0] == "continue":                           # board moves to the next stage
            nxt = {"DFU download": ("Android Fastboot", "U-Boot SPL 2022.10\n"),
                   "Android Fastboot": ("Android Fastboot", "U-Boot 2022.10 (Oct 29 2026)\n")}
            if self.staged == "kernel":
                self.product = None
                self.console.emit("Run /init as init process\n~ # ")
            else:
                self.product, banner = nxt[self.product]
                self.console.emit(banner)
        if args[0] == "stage":
            self.staged = "kernel" if args[1].endswith("Image.itb") else "fw"
        return "OKAY"


class FakeConsole:
    alias = "board1"

    def __init__(self, tmp):
        self.path = tmp / "console.log"
        self.path.write_text("")

    def emit(self, text):
        with open(self.path, "a") as f:
            f.write(text)

    def mark(self):
        return self.path, self.path.stat().st_size

    def read_since(self, mark):
        return self.path.read_text()[mark[1]:]

    def tail(self, n=10, grep=None):
        return self.path.read_text().splitlines()[-n:]

    def wait(self, mark, pattern, fail, timeout, stall=None, nudge=None):
        import re

        text = self.read_since(mark)
        if fail and re.search(fail, text):
            raise HardwareError("console reported failure")
        if pattern and not re.search(pattern, text):
            raise HardwareError(f"/{pattern}/ not seen: {text!r}")
        return text


class FakeControl:
    def __init__(self, console):
        self.console, self.resets = console, 0

    def enter_fdl(self, log, ready=None):
        self.resets += 1
        self.console.emit("ROM: usb download handler\n")


class FakeStore:
    def pull(self, ssh, files, log):
        return {r: {"path": p, "remote": p, "sha256": "x", "size": 1, "age_s": 0} for r, p in files.items()}


def test_boot_runner_executes_load_recipe(tmp_path):
    sdk = SdkCatalog(ROOT / "examples" / "site" / "sdk").get("bsp-a")
    con = FakeConsole(tmp_path)
    fb = FakeFastboot(con)
    ctl = FakeControl(con)
    lines = []
    res = BootRunner("b1", ctl, fb, con, FakeStore()).run(sdk, "load", None, lines.append)
    assert ctl.resets == 1 and res["sdk"] == "bsp-a"
    staged = [c[1].rsplit("/", 1)[-1] for c in fb.calls if c[0] == "stage"]
    assert staged == ["FSBL.bin", "u-boot-opensbi.itb", "Image.itb"]
    assert any("~ # " in l or "step 10/10" in l for l in lines)


def test_boot_runner_uboot_only_variant(tmp_path):
    sdk = SdkCatalog(ROOT / "examples" / "site" / "sdk").get("bsp-a")
    con = FakeConsole(tmp_path)
    fb = FakeFastboot(con)
    BootRunner("b1", FakeControl(con), fb, con, FakeStore()).run(sdk, "load", "uboot-only", lambda m: None)
    assert [c[1].rsplit("/", 1)[-1] for c in fb.calls if c[0] == "stage"] == ["FSBL.bin", "u-boot-opensbi.itb"]


# ------------------------------------------------------------------ hooks --
def _hook(dir_: Path, kind: str, name: str, body: str):
    p = dir_ / kind / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env python3\nimport json,sys\nreq=json.load(sys.stdin)\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def test_hook_contract_and_override_order(tmp_path, monkeypatch):
    site, user = tmp_path / "site", tmp_path / "user"
    _hook(site, "probe", "x", "print('working'); print(json.dumps({'who': 'site', 'echo': req['a']}))")
    _hook(user, "probe", "x", "print(json.dumps({'who': 'user'}))")
    _hook(site, "probe", "none", "print(json.dumps({'found': []})); sys.exit(2)")
    _hook(site, "probe", "bad", "print(json.dumps({'error': 'no fastboot', 'hint': 'install it'})); sys.exit(1)")
    monkeypatch.setenv("BSP_HOOKS_PATH", f"{user}:{site}")
    hr = HookRunner()
    assert hr.run("probe", "x", {"a": 1}).data["who"] == "user"            # earlier dir wins
    monkeypatch.setenv("BSP_HOOKS_PATH", str(site))
    hr = HookRunner()
    lines = []
    r = hr.run("probe", "x", {"a": 7}, log=lines.append)
    assert r.data == {"who": "site", "echo": 7} and lines == ["probe/x: working"]
    assert not hr.run("probe", "none", {}).applicable
    with pytest.raises(HardwareError) as e:
        hr.run("probe", "bad", {})
    assert "no fastboot" in e.value.message and e.value.hint == "install it"
    assert {"consoles", "usb-devices", "identify"} <= set(HookRunner().names("probe"))   # repo hooks


def test_registry_update_and_rename(tmp_path):
    reg = DutRegistry("hostA", None, tmp_path / "duts.json")
    reg.add(SPEC)
    reg.add({**SPEC, "id": "b2", "fastboot": {"usb": "usb:3-1"}, "console": {"alias": "board2"}})
    reg.update("b1", {**SPEC, "id": "board-a", "soc": "soc-a"})
    assert sorted(reg.all()) == ["b2", "board-a"] and reg.all()["board-a"].soc == "soc-a"
    with pytest.raises(Conflict):
        reg.update("board-a", {**SPEC, "id": "b2"})                     # name taken
    with pytest.raises(Conflict):
        reg.update("b2", {**SPEC, "id": "b2"})                          # would steal board-a's usb


def test_soc_profiles_detect_and_layer_merge(tmp_path):
    from bsp.dut.socs import SocProfiles

    site = ROOT / "examples" / "site" / "socs"
    p = SocProfiles([site])
    assert p.detect("ROMB: hello\nROMB: usb2 init")[0] == "soc-b"
    assert p.detect("ROMA: boot start\nROMA: usb init")[0] == "soc-a"
    assert p.detect("random noise")[0] is None
    assert p.in_download_mode("xx download handler", "") and not p.in_download_mode("", "Android Fastboot")
    # a higher layer overrides fields and supplies the image files
    top = tmp_path / "socs" / "soc-a"
    top.mkdir(parents=True)
    (top / "soc.yaml").write_text("images: {fsbl: {sha256: abc}}\ndownload_mode: {fastboot: OTHER}\n")
    (top / "fsbl.bin").write_bytes(b"x")
    m = SocProfiles([site, tmp_path / "socs"]).get("soc-a")
    assert m["download_mode"] == {"console": "download handler", "fastboot": "OTHER"}
    assert m["images"]["fsbl"] == {"file": "fsbl.bin", "sha256": "abc", "path": str(top / "fsbl.bin")}
    assert "path" not in m["images"]["uboot"]                        # file not present in any layer


def test_console_wait_nudges_a_quiet_prompt(tmp_path, monkeypatch):
    """A prompt without a newline only reaches the line-based log after Enter."""
    from bsp.dut import hostio

    con = hostio.SerialConsole("b1", log_dir=str(tmp_path))
    (tmp_path / "b1").mkdir()
    log = con._log_path()
    log.write_text("run init ...\n")
    mark = con.mark()
    sent = []

    def fake_run(argv, timeout):
        sent.append(argv)
        with open(log, "a") as f:
            f.write("~ # \n")

    monkeypatch.setattr(hostio, "_run", fake_run)
    assert "~ # " in con.wait(mark, r"~ # ", None, timeout=5, nudge=0.3)
    assert sent and sent[0][-2:] == ["--send", " "]


def _human(cfg=None):
    import threading

    from bsp.dut.catalog import spec_from_dict as parse_spec
    from bsp.dut.control import Attention, make_control

    att = Attention()
    spec = parse_spec({"id": "slot-1", "host": "h", "board": "b", "console": {"alias": "slot-1"},
                              "fastboot": {"usb": "usb:1-1"}, "control": {"backend": "human", **(cfg or {})}})
    return make_control(spec, lambda name: att), att, threading


def test_human_backend_waits_for_confirm():
    ctl, att, threading = _human()
    assert ctl.needs_human
    lines = []
    t = threading.Thread(target=ctl.enter_fdl, args=(lines.append,))
    t.start()
    for _ in range(50):
        if att.view("slot-1"):
            break
        time.sleep(0.02)
    assert att.view("slot-1")["action"] == "enter download mode"
    assert att.confirm("slot-1", "pressed")["confirmed"] == "enter download mode"
    t.join(5)
    assert not t.is_alive() and att.view("slot-1") is None
    assert any("ACTION NEEDED" in l for l in lines) and lines[-1].endswith("confirmed by a person")


def test_human_backend_detects_the_board_itself_and_times_out():
    ctl, att, _ = _human({"human": {"timeout_s": 0.3}})
    lines = []
    ctl.reset(lines.append, ready=lambda: "the boot ROM on the console")
    assert lines[-1] == "reset: detected the boot ROM on the console"
    with pytest.raises(HardwareTimeout) as e:
        ctl.reset(lines.append, ready=lambda: None)
    assert "bsp dut confirm slot-1" in e.value.hint
    with pytest.raises(NotFound):
        att.confirm("slot-1")                        # nothing pending any more


def test_human_probe_pairs_the_console_that_speaks_with_the_usb_port_that_appears(tmp_path, monkeypatch):
    """No GPIO: a person resets boards one at a time; each round pairs a console and a USB port."""
    import types

    from bsp.core.hooks import HookRunner as _H  # noqa: F401 - the real hooks are replaced below
    from bsp.dut import probe as probe_mod
    from bsp.dut.socs import SocProfiles

    logs = {a: tmp_path / a for a in ("con-a", "con-b")}
    for d in logs.values():
        d.mkdir()
    usb = {"usb:1-1": {"usb": "usb:1-1", "devnum": 3, "product": "hub"}}
    script = [("con-b", "usb:2-1.4"), ("con-a", "usb:2-1.2")]

    class Hooks:
        def run(self, kind, name, req, timeout=0, log=None):
            if name == "consoles":
                return types.SimpleNamespace(data={"consoles": [
                    {"id": a, "kind": "serial-mux", "alias": a, "device": f"/dev/{a}", "log": str(d)} for a, d in logs.items()]})
            return types.SimpleNamespace(data={"devices": list(usb.values())})

    events = []
    mod = types.SimpleNamespace(
        probe_cfg={}, socs=SocProfiles([ROOT / "examples" / "site" / "socs"]), hooks=Hooks(), boards={},
        serial_log_dir=str(tmp_path / "logs"), fastboot_bin="fastboot",
        ctx=types.SimpleNamespace(host_id="lab-9", service=lambda name: (_ for _ in ()).throw(NotFound("no gpio"))),
        _event=lambda topic, **d: events.append(d))
    lines = []
    sleeps = {"n": 0}

    def fake_sleep(s):
        # a person reacts during the first poll of each round
        sleeps["n"] += 1
        if script and sleeps["n"] % 10 == 1:
            con, port = script.pop(0)
            from datetime import date

            (logs[con] / f"{date.today().isoformat()}.log").open("a").write("ROMB: hello\nROMB: usb2 init\nxx download handler\n")
            usb[port] = {"usb": port, "devnum": 9, "product": "DFU download"}

    clock = {"t": 0.0}
    monkeypatch.setattr(probe_mod.time, "sleep", lambda s: (fake_sleep(s), clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(probe_mod.time, "monotonic", lambda: clock["t"])
    p = probe_mod.Prober(mod, None, None, identify=False, default_sdks=None, human=True, rounds=5, wait_s=10)
    res = p.run(lines.append)
    specs = [x["spec"] for x in res["proposals"]]
    assert [(s["console"]["alias"], s["fastboot"]["usb"], s["control"]["backend"]) for s in specs] == [
        ("con-b", "usb:2-1.4", "human"), ("con-a", "usb:2-1.2", "human")]
    assert all(s["soc"] == "soc-b" and s["probed"]["human"] for s in specs)
    assert res["unmatched_consoles"] == []
    assert sum("ACTION NEEDED" in l for l in lines) == 2 and events[0]["dut"] == "probe@lab-9"


def test_enter_fdl_accepts_a_board_already_in_download_mode(tmp_path):
    """A board already in download mode prints no new boot-ROM banner; its USB download device is enough."""
    con = FakeConsole(tmp_path)
    fb = FakeFastboot(con)                       # on the bus as "DFU download" already

    class Quiet:
        def enter_fdl(self, log, ready=None):
            assert ready() == "download-mode device on usb:2-1.2 (DFU download)"

    lines = []
    BootRunner("b1", Quiet(), fb, con, FakeStore(), download_mode={"console": "download handler", "fastboot": "DFU"}) \
        .enter_fdl(lines.append, timeout=2)
    assert lines[-1] == "board in download mode on usb:2-1.2 (DFU download)"
    class Nothing:
        def enter_fdl(self, log, ready=None):
            pass

    fb.product = None                            # neither sign: the error names both
    with pytest.raises(HardwareTimeout) as e:
        BootRunner("b1", Nothing(), fb, con, FakeStore(), download_mode={"console": "download handler"}) \
            .enter_fdl(lines.append, timeout=0.5)
    assert "neither the boot ROM's banner" in e.value.message and "usb:2-1.2" in e.value.message


def test_flash_hook_skips_a_medium_the_board_does_not_have(tmp_path):
    """U-Boot reports mtd-size NULL on a UFS-only board: the NOR layout is skipped, not flashed and failed."""
    import subprocess
    import zipfile

    pkg = tmp_path / "flash.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("partition_64M.json", json.dumps({"format": "mtd", "partitions": [{"name": "fsbl", "image": "nor.bin"}]}))
        z.writestr("partition_universal.json", json.dumps({"format": "gpt", "partitions": [{"name": "bootfs", "image": "bootfs.vfat"}]}))
        z.writestr("nor.bin", "n")
        z.writestr("bootfs.vfat", "b")
    log = tmp_path / "fastboot.log"
    fb = tmp_path / "fastboot"
    fb.write_text(f"""#!/bin/sh
echo "$*" >> {log}
case "$3 $4" in
  "getvar mtd-size") echo "mtd-size: NULL" >&2 ;;
  "getvar blk-size") echo "blk-size: universal" >&2 ;;
  "flash mtd") echo "FAILED (remote: 'invalid partition or device')" >&2; exit 1 ;;
  *) echo OKAY >&2 ;;
esac
""")
    fb.chmod(0o755)
    hook = ROOT / "packs" / "spacemit" / "hooks" / "flash" / "spacemit-pkg"
    req = {"usb": "usb:2-1.1", "fastboot": str(fb), "artifacts": {"flash-zip": str(pkg)}}
    r = subprocess.run([sys.executable, str(hook)], input=json.dumps(req), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout
    result = json.loads(r.stdout.strip().splitlines()[-1])
    assert result["layouts"] == ["partition_universal.json"] and not result["failed"]
    assert "device has no mtd medium (mtd-size=NULL); skipping mtd" in r.stdout
    assert "flash mtd" not in log.read_text() and "flash gpt" in log.read_text()

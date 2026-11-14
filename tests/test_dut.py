import json
import sys
import os
import stat
import subprocess
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
        self.sent = []

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

    def send(self, text, pattern=None, fail=None, timeout=60, stall=None, nudge=None):
        self.sent.append(text)
        self.emit(f"{text}\n")
        if pattern:
            self.emit("READY\n")
        return {"sent": text, "output": "READY" if pattern else ""}


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


def test_boot_runner_send_step_types_one_line_and_waits(tmp_path):
    con = FakeConsole(tmp_path)
    lines = []
    BootRunner("b1", FakeControl(con), FakeFastboot(con), con, FakeStore()).run_steps(
        [{"do": "send", "text": "reboot", "serial": "READY", "timeout": 5}], lines.append)
    assert con.sent == ["reboot"]
    assert any("sent 'reboot'" in line and "/READY/ seen" in line for line in lines)


# ------------------------------------------------------ serial send (echo) --
def _console(tmp_path):
    from bsp.dut import hostio

    (tmp_path / "b1").mkdir(exist_ok=True)
    return hostio.SerialConsole("b1", log_dir=str(tmp_path))


def _fake_smtty(monkeypatch, log, *chunks):
    from bsp.dut import hostio

    def fake_run(argv, timeout):
        with open(log, "a") as f:
            for chunk in chunks:
                f.write(chunk)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(hostio, "_run", fake_run)


def test_console_send_does_not_accept_the_echoed_line(tmp_path, monkeypatch):
    """`text` itself contains the wait pattern; only what the board prints after the echo counts."""
    con = _console(tmp_path)
    _fake_smtty(monkeypatch, con._log_path(), "[2026-11-12 10:00:00] echo READY\nREADY\n")
    out = con.send("echo READY", "READY", timeout=2)
    assert out["sent"] == "echo READY" and "READY" in out["output"]


def test_console_send_times_out_on_the_echo_alone(tmp_path, monkeypatch):
    con = _console(tmp_path)
    _fake_smtty(monkeypatch, con._log_path(), "[2026-11-12 10:00:00] reboot now\n")
    with pytest.raises(HardwareTimeout):
        con.send("reboot now", "reboot now", timeout=0.2)


def test_console_send_fail_pattern_in_the_echo_is_not_a_failure(tmp_path, monkeypatch):
    con = _console(tmp_path)
    _fake_smtty(monkeypatch, con._log_path(), "[2026-11-12 10:00:00] watch PANIC\n")
    with pytest.raises(HardwareTimeout):
        con.send("watch PANIC", "released", fail="PANIC", timeout=0.2)


def test_console_send_without_an_echo_matches_the_output(tmp_path, monkeypatch):
    con = _console(tmp_path)
    _fake_smtty(monkeypatch, con._log_path(), "READY\n")
    out = con.send("status", "READY", timeout=1)
    assert out["output"].startswith("READY")


def test_a_send_step_refuses_more_than_one_line(tmp_path):
    con = _console(tmp_path)
    with pytest.raises(InvalidRequest):
        con.send("one\ntwo", timeout=1)
    with pytest.raises(InvalidRequest):
        con.send("   ", timeout=1)


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


def test_a_site_overlay_adds_media_boot_and_images_to_a_pack_profile(tmp_path):
    """A site is an overlay: it writes only what it adds, the public pack supplies everything else."""
    from bsp.dut import images
    from bsp.dut.socs import SocProfiles

    site = tmp_path / "socs" / "k3"
    site.mkdir(parents=True)
    (site / "soc.yaml").write_text(
        "images:\n"
        "  handoff: {file: media-handoff.bin, sha256: 'abababababababababababababababababababababababababababababababab', source: {url: 'https://example.org/x.bin'}}\n"
        "media_boot:\n"
        "  name: media-handoff\n"
        "  steps: [{do: check, serial: 'sys: 0x'}, {do: stage, image: handoff}, {do: continue}]\n")
    p = SocProfiles([ROOT / "packs" / "spacemit" / "socs", tmp_path / "socs"])
    prof = p.get("k3")
    # the pack's facts survive
    assert prof["download_mode"]["fastboot"] == "DFU download"
    assert prof["identify"]["fields"]["model"]
    assert prof["images"]["fsbl"]["file"] == "FSBL.bin"
    # and the site's additions are merged on top
    assert prof["media_boot"]["name"] == "media-handoff"
    assert prof["images"]["handoff"]["source"]["url"] == "https://example.org/x.bin"
    plan = {row["image"]: row["state"] for row in images.plan(p, tmp_path)}
    assert plan["handoff"] == "fetchable" and plan["fsbl"] == "missing"


def test_common_boot_banners_count_as_a_boot_and_shell_chatter_does_not():
    from bsp.dut.socs import SocProfiles

    p = SocProfiles(None)
    for banner in ("NOTICE:  BL2: v2.7(release):v2.7-dirty", "NOTICE:  BL31: Built : 10:15:02, Jan 10 2026",
                   "INFO:    BL1: RAM 0x4022000 - 0x4023000", "DDR Version 1.16 20230821", "DDR V1.08 20221011",
                   "U-Boot TPL 2023.07 (Jan 01 2026 - 00:00:00)", "U-Boot SPL board init",
                   "Trying to boot from MMC1", "Hit any key to stop autoboot:  2", "OpenSBI v1.3",
                   "U-Boot 2024.01 (Jan 01 2026)", "Starting kernel ..."):
        assert p.looks_like_boot(banner), banner
    for chatter in ("~ # lsusb\nBus 001 Device 001: ID 1d6b:0002\n~ # ", "root@board:~# ls /boot\nImage  dtb\n",
                    "$ echo NOTICE\nNOTICE\n", "root@board:~# cat notes.txt\nddr is fine\n"):
        assert not p.looks_like_boot(chatter), chatter


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
    # The error says a person was needed, so an agent asks one instead of rewiring the host.
    assert "reset by a person (no relay)" in e.value.message and "nobody did 'reset'" in e.value.message
    assert "ask a person" in e.value.hint and "rebuild" not in e.value.hint
    with pytest.raises(NotFound):
        att.confirm("slot-1")                        # nothing pending any more


def test_human_probe_pairs_the_console_that_speaks_with_the_usb_port_that_appears(tmp_path, monkeypatch):
    """No GPIO: each board first boots normally, then a person resets it into download mode; the console and the USB
    port that appear together are paired."""
    import types

    from bsp.core.hooks import HookRunner as _H  # noqa: F401 - the real hooks are replaced below
    from bsp.dut import probe as probe_mod
    from bsp.dut.socs import SocProfiles

    logs = {a: tmp_path / a for a in ("con-a", "con-b")}
    for d in logs.values():
        d.mkdir()
    usb = {"usb:1-1": {"usb": "usb:1-1", "devnum": 3, "product": "hub"}}
    order = ["con-b", "con-a"]                     # the boards a person works through, in turn
    ports = {"con-b": "usb:2-1.4", "con-a": "usb:2-1.2"}

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
    clock = {"t": 0.0}
    acted = set()                                   # (board index, "boot" | "fdl")

    def fake_sleep(s):
        clock["t"] += s
        if not lines:
            return
        from datetime import date

        asked = lines[-1]
        import re as _re
        found = _re.search(r"\(board (\d+)\)", asked)
        if not found:
            return
        k = int(found.group(1)) - 1                # the board the instruction is for
        if asked.startswith("ACTION NEEDED (board") and "start normally" in asked and (k, "boot") not in acted:
            acted.add((k, "boot"))
            (logs[order[k]] / f"{date.today().isoformat()}.log").open("a").write(
                "U-Boot SPL 2022.10\nStarting kernel ...\nlogin: ")
        elif "hold its BOOT" in asked and (k, "fdl") not in acted:
            acted.add((k, "fdl"))
            con, port = order[k], ports[order[k]]
            (logs[con] / f"{date.today().isoformat()}.log").open("a").write(
                "ROMB: hello\nROMB: usb2 init\nxx download handler\n")
            usb[port] = {"usb": port, "devnum": 9, "product": "DFU download"}

    monkeypatch.setattr(probe_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(probe_mod.time, "monotonic", lambda: clock["t"])
    p = probe_mod.Prober(mod, None, None, identify=False, default_sdks=None, human=True, rounds=5, wait_s=10)
    res = p.run(lines.append)
    specs = [x["spec"] for x in res["proposals"]]
    assert [(s["console"]["alias"], s["fastboot"]["usb"], s["control"]["backend"]) for s in specs] == [
        ("con-b", "usb:2-1.4", "human"), ("con-a", "usb:2-1.2", "human")]
    assert all(s["soc"] == "soc-b" and s["probed"]["human"] for s in specs)
    assert all(s["control"]["fdl"]["strategy"] == "button" for s in specs)   # BOOT+RESET, not a strapped reset
    assert res["unmatched_consoles"] == []
    # Two steps per board, and each asks the page to notify a person.
    assert sum("ACTION NEEDED" in line for line in lines) == 4
    assert [e["action"] for e in events[:4]] == ["boot normally", "enter download mode", "boot normally", "enter download mode"]
    assert all(e["dut"] == "probe@lab-9" for e in events)


def test_human_probe_starts_from_a_running_board_not_one_already_in_download_mode(tmp_path, monkeypatch):
    """A board left in download mode would match at once; the first step asks for a normal boot instead."""
    import types

    from bsp.dut import probe as probe_mod
    from bsp.dut.socs import SocProfiles

    logdir = tmp_path / "logs" / "con-a"
    logdir.mkdir(parents=True)
    usb = {"usb:1-1": {"usb": "usb:1-1", "devnum": 3, "product": "hub"}}

    class Hooks:
        def run(self, kind, name, req, timeout=0, log=None):
            if name == "consoles":
                return types.SimpleNamespace(data={"consoles": [{"id": "con-a", "kind": "serial-mux", "alias": "con-a",
                                                                 "device": "/dev/con-a", "log": str(logdir)}]})
            return types.SimpleNamespace(data={"devices": list(usb.values())})

    mod = types.SimpleNamespace(
        probe_cfg={}, socs=SocProfiles([ROOT / "examples" / "site" / "socs"]), hooks=Hooks(), boards={},
        serial_log_dir=str(tmp_path / "logs"), fastboot_bin="fastboot",
        ctx=types.SimpleNamespace(host_id="lab-9", service=lambda name: (_ for _ in ()).throw(NotFound("no gpio"))),
        _event=lambda topic, **d: None)
    lines, clock, state = [], {"t": 0.0}, {"step": 0}

    def write(text):
        from datetime import date

        (logdir / f"{date.today().isoformat()}.log").open("a").write(text)

    def fake_sleep(s):
        clock["t"] += s
        asked = lines[-1] if lines else ""
        if "start normally" in asked and state["step"] == 0:
            state["step"] = 1
            write("xx download handler\n")          # already in download mode: does not count as a normal boot
        elif "start normally" in asked and state["step"] == 1 and clock["t"] > 5:
            state["step"] = 2
            write("U-Boot SPL 2022.10\nStarting kernel ...\n")   # the person reset it normally
        elif "hold its BOOT" in asked and state["step"] == 2:
            state["step"] = 3
            write("ROMB: hello\nxx download handler\n")
            usb["usb:2-1.2"] = {"usb": "usb:2-1.2", "devnum": 9, "product": "DFU download"}

    monkeypatch.setattr(probe_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(probe_mod.time, "monotonic", lambda: clock["t"])
    res = probe_mod.Prober(mod, None, None, identify=False, default_sdks=None, human=True, rounds=1, wait_s=10).run(lines.append)
    assert [x["spec"]["console"]["alias"] for x in res["proposals"]] == ["con-a"]
    assert any("started in download mode and booted normally" in line for line in lines)
    assert sum("ACTION NEEDED" in line for line in lines) == 2


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


def test_a_person_gets_the_whole_window_to_put_a_board_in_download_mode(tmp_path, monkeypatch):
    """A person confirms, then presses a moment later: the board is waited for past a relay's 30 seconds, within the
    person's window; if it never shows, the error says a person confirmed and the board did not follow."""
    import bsp.dut.boot as boot_mod

    con = FakeConsole(tmp_path)
    fb = FakeFastboot(con)
    fb.product = None
    clock = [1000.0]
    monkeypatch.setattr(boot_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(boot_mod.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    class Person:
        needs_human = True
        def human_timeout(self):
            return 300.0
        def enter_fdl(self, log, ready=None):
            clock[0] += 10                         # confirmed after 10 s, before the board is there

    lines = []
    runner = BootRunner("b1", Person(), fb, con, FakeStore(), download_mode={"console": "download handler", "fastboot": "DFU"})
    real_seen = runner.download_evidence

    def late(mark):
        check = real_seen(mark)
        def seen():
            if clock[0] >= 1100 and fb.product is None:     # pressed 100 s in: a relay's 30 s would have failed
                fb.product = "DFU download"
            return check()
        return seen
    runner.download_evidence = late
    runner.enter_fdl(lines.append, timeout=30)
    assert lines[-1].startswith("board in download mode")

    fb.product = None
    runner.download_evidence = real_seen                    # never shows: the person's window runs out
    with pytest.raises(HardwareTimeout) as e:
        runner.enter_fdl(lines.append, timeout=30)
    assert "a person confirmed b1 is in download mode" in e.value.message
    assert "reset by a person (no relay)" in e.value.hint and "rebuild" not in e.value.hint


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


def test_spacemit_pkg_flashes_a_package_directory_from_boot_rom_download_mode(tmp_path):
    """`bsp dut flash` hands over an unpacked directory with the board in the boot ROM: the package's
    fastboot.yaml stage/continue actions bring U-Boot up, then the layouts are flashed with progress."""
    import subprocess
    import sys

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "fastboot.yaml").write_text("version: 1\nactions:\n- stage: {file: FSBL.bin}\n- continue: {}\n"
                                       "- stage: {file: u-boot.itb}\n- continue: {}\n- getvar: {args: blk-size}\n"
                                       "- multi_flash: {relate_partition: [gpt]}\n")
    for name, data in (("FSBL.bin", "f"), ("u-boot.itb", "u"), ("bootfs.vfat", "b" * 300), ("rootfs.ext4", "r" * 700)):
        (pkg / name).write_text(data)
    (pkg / "partition_universal.json").write_text(json.dumps({"format": "gpt", "partitions": [
        {"name": "bootfs", "image": "bootfs.vfat"}, {"name": "rootfs", "image": "rootfs.ext4"}]}))
    log, state = tmp_path / "fastboot.log", tmp_path / "rom"
    state.write_text("rom")
    fb = tmp_path / "fastboot"
    # in the ROM the port says "DFU download"; each continue drops it off the bus for a moment
    fb.write_text(f"""#!/bin/sh
echo "$*" >> {log}
if [ "$1" = devices ]; then
  [ -e {tmp_path}/away ] && {{ rm {tmp_path}/away; exit 0; }}
  grep -q rom {state} && printf 'dfu\\tDFU download usb:2-1.1\\n' || printf 'x\\tfastboot usb:2-1.1\\n'
  exit 0
fi
case "$3 $4" in
  "getvar mtd-size") echo "mtd-size: NULL" >&2 ;;
  "getvar blk-size") echo "blk-size: universal" >&2 ;;
  "continue ") touch {tmp_path}/away; echo uboot > {state}; echo OKAY >&2 ;;
  *) echo OKAY >&2 ;;
esac
""")
    fb.chmod(0o755)
    hook = ROOT / "packs" / "spacemit" / "hooks" / "flash" / "spacemit-pkg"
    req = {"usb": "usb:2-1.1", "fastboot": str(fb), "package_dir": str(pkg), "artifacts": {}, "args": {"port_timeout": 5}}
    r = subprocess.run([sys.executable, str(hook)], input=json.dumps(req), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.strip().splitlines()
    result = json.loads(lines[-1])
    assert [f["partition"] for f in result["flashed"]] == ["gpt", "bootfs", "rootfs"]
    calls = [l for l in log.read_text().splitlines() if not l.startswith("devices")]
    assert calls[:4] == [f"-s usb:2-1.1 stage {pkg}/FSBL.bin", "-s usb:2-1.1 continue",
                         f"-s usb:2-1.1 stage {pkg}/u-boot.itb", "-s usb:2-1.1 continue"]
    pcts = [json.loads(l)["progress"] for l in lines[:-1] if l.startswith('{"progress"')]
    assert pcts == sorted(pcts) and pcts[-1] == 100 and len(pcts) == 3
    assert pkg.is_dir() and (pkg / "rootfs.ext4").exists()              # the caller's directory is left alone


def test_a_board_whose_fdl_is_not_known_says_what_to_set():
    import pytest

    from bsp.core.errors import InvalidRequest
    from bsp.dut.catalog import spec_from_dict as parse_spec
    from bsp.dut.control import make_control

    spec = parse_spec({"id": "k3-s1", "host": "h", "board": "b", "console": {"alias": "s1"}, "fastboot": {"usb": "usb:1-1"},
                       "control": {"backend": "gpio-relay", "reset": {"pin": "P33"}, "fdl": {"strategy": "unset"}}})
    ctl = make_control(spec, lambda name: (_ for _ in ()).throw(AssertionError("no line is touched")))
    with pytest.raises(InvalidRequest) as e:
        ctl.enter_fdl(lambda line: None)
    assert "not set" in e.value.message and "--fdl strapped" in e.value.hint


def test_hook_lines_stream_as_they_come_and_progress_lines_set_a_percent(tmp_path, monkeypatch):
    """A long hook shows what it does while it runs; its progress lines set the job's percent."""
    site = tmp_path / "site"
    _hook(site, "flash", "slow", "import time\nprint('erasing', flush=True)\ntime.sleep(0.6)\n"
                                 "print(json.dumps({'progress': 40}))\nprint(json.dumps({'type': 'progress', 'value': 40.4}))\n"
                                 "print(json.dumps({'note': 'not progress'}))\n"
                                 "print(json.dumps({'type': 'progress', 'value': 100, 'msg': 'rootfs'}))\n"
                                 "print(json.dumps({'flashed': ['rootfs']}))")
    monkeypatch.setenv("BSP_HOOKS_PATH", str(site))
    seen, pcts = [], []
    t0 = time.monotonic()
    r = HookRunner().run("flash", "slow", {}, log=lambda l: seen.append((round(time.monotonic() - t0, 1), l)),
                         progress=pcts.append)
    assert r.data == {"flashed": ["rootfs"]}
    assert seen[0][1] == "flash/slow: erasing" and seen[0][0] < 0.5          # before the hook is done
    assert [l for _, l in seen[1:]] == ["flash/slow: 40%", 'flash/slow: {"note": "not progress"}',
                                        "flash/slow: 100% rootfs"]
    assert pcts == [40.0, 40.4, 100.0]


def test_a_hook_that_runs_too_long_gets_sigterm_first(tmp_path, monkeypatch):
    site = tmp_path / "site"
    mark = tmp_path / "terminated"
    _hook(site, "flash", "stuck", f"import signal,time\n"
                                  f"signal.signal(signal.SIGTERM, lambda *a: (open({str(mark)!r}, 'w').write('x'), sys.exit(143)))\n"
                                  f"print('working', flush=True)\ntime.sleep(30)")
    monkeypatch.setenv("BSP_HOOKS_PATH", str(site))
    with pytest.raises(HardwareTimeout) as e:
        HookRunner().run("flash", "stuck", {}, timeout=1)
    assert "timed out after 1s" in e.value.message and "more time" in e.value.hint
    assert mark.exists() and e.value.details["stdout"] == ["working"]


def test_an_sdk_recipes_hook_step_gets_the_boards_soc_images(tmp_path):
    """A flasher shipped as a SoC image (fetched by `bsp images sync`) is handed to the recipe's hook step."""
    import types

    sdk = types.SimpleNamespace(id="pkg-sdk", server=types.SimpleNamespace(ssh="u@h"), artifacts=lambda: {"flash-zip": "/out/p.zip"},
                                boot_plan=lambda method, variant, brk=None: {"steps": [{"do": "hook", "kind": "flash", "name": "tool-a",
                                                                               "artifacts": ["flash-zip"]}]})
    seen = {}

    class Hooks:
        def run(self, kind, name, req, timeout=0, log=None, progress=None):
            seen.update(req)
            return types.SimpleNamespace(data={"flashed": []}, lines=[], code=0)

    con = FakeConsole(tmp_path)
    media = types.SimpleNamespace(profile={"images": {"tool-a-amd64": {"file": "tool", "path": "/site/.blobs/x/tool"},
                                                      "not-fetched": {"file": "other"}}})
    BootRunner("b1", FakeControl(con), FakeFastboot(con), con, FakeStore(), hooks=Hooks(), media=media) \
        .run(sdk, "download", None, lambda m: None)
    assert seen["images"] == {"tool-a-amd64": "/site/.blobs/x/tool"}
    assert seen["artifacts"] == {"flash-zip": "/out/p.zip"}


def test_preferred_gpio_numbers_are_read_however_they_are_typed():
    """`--prefer` takes what people actually write: spaces, Chinese commas, 、, ;, GPIO12, gpio_13, P15, 引脚16."""
    from bsp.dut.probe import pin_numbers

    for typed in ("12, 13", "12，13", "12、13", "12;13", "12 13", "GPIO12 gpio_13 P15 pin 16 引脚17",
                  "GPIO 18、GPIO_19\n20"):
        numbers = pin_numbers(typed)
        assert numbers == sorted(set(numbers)), (typed, numbers)
    assert pin_numbers("12, 13") == [12, 13]
    assert pin_numbers("GPIO 12、gpio_13; P14，引脚15") == [12, 13, 14, 15]
    assert pin_numbers("gpio_12 gpio_12") == [12]
    assert pin_numbers("") == [] and pin_numbers(None) == []


def test_confirm_names_the_request_and_a_repeat_is_idempotent():
    """A person clicks 「我做好了」 after bsp already saw the board arrive: the request id resolves to what it
    answered, so the click is not an error and the page can say it was already confirmed."""
    import threading

    from bsp.dut.catalog import spec_from_dict as parse_spec
    from bsp.dut.control import Attention, make_control

    att = Attention()
    spec = parse_spec({"id": "slot-1", "host": "h", "board": "b", "console": {"alias": "slot-1"},
                       "fastboot": {"usb": "usb:1-1"}, "control": {"backend": "human"}})
    ctl = make_control(spec, lambda name: att)
    lines = []
    seen = {"now": None}
    t = threading.Thread(target=ctl.reset, args=(lines.append, lambda: seen["now"]))
    t.start()
    for _ in range(50):
        if att.view("slot-1"):
            break
        time.sleep(0.02)
    req = att.view("slot-1")["id"]
    seen["now"] = "the boot ROM on the console"          # the board arrives before anyone clicks
    t.join(5)
    assert not t.is_alive()

    again = att.confirm("slot-1", "clicked", request=req)
    assert again["repeat"] and again["how"] == "detected" and again["request"] == req
    # A second click on the same page button: still the same answer, never NotFound.
    assert att.confirm("slot-1", "", request=req)["repeat"]

    # An unknown request id is the one thing that is still an error, and it says what is waiting.
    with pytest.raises(NotFound) as e:
        att.confirm("slot-1", request="nope")
    assert "not waiting" in e.value.message and "bsp dut status" in e.value.hint


def test_confirm_without_a_request_answers_what_is_waiting_now():
    import threading

    from bsp.dut.catalog import spec_from_dict as parse_spec
    from bsp.dut.control import Attention, make_control

    att = Attention()
    spec = parse_spec({"id": "slot-1", "host": "h", "board": "b", "console": {"alias": "slot-1"},
                       "fastboot": {"usb": "usb:1-1"}, "control": {"backend": "human"}})
    ctl = make_control(spec, lambda name: att)
    t = threading.Thread(target=ctl.enter_fdl, args=(lambda line: None,))
    t.start()
    for _ in range(50):
        if att.view("slot-1"):
            break
        time.sleep(0.02)
    done = att.confirm("slot-1", "pressed")
    assert done["confirmed"] == "enter download mode" and not done["repeat"] and done["request"]
    t.join(5)
    assert not t.is_alive()


def test_a_repeated_ask_supersedes_the_older_one_and_a_click_on_it_is_answered():
    """bsp asks again before a person answered (a second reset): a click on the first page button resolves to
    'superseded', not to an error, and the newer request carries its own id."""
    from bsp.dut.control import Attention

    att = Attention()
    first = att.request("slot-1", "reset", "press RESET")
    second = att.request("slot-1", "reset", "press RESET")
    assert first["id"] != second["id"]
    assert att.view("slot-1")["id"] == second["id"]
    old = att.confirm("slot-1", "clicked", request=first["id"])
    assert old["repeat"] and old["how"] == "superseded" and old["request"] == first["id"]
    att.confirm("slot-1", "clicked", request=second["id"])
    after = att.confirm("slot-1", "", request=second["id"])
    assert after["repeat"] and after["how"] == "by a person"

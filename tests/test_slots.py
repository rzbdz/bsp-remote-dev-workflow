"""Slots: every console on a host is a slot; reset lines, download ports and DUTs are attached to it."""
import json
import types
from datetime import date
from pathlib import Path

import pytest

from bsp.core.errors import Conflict, InvalidRequest, NotFound
from bsp.dut import probe as probe_mod
from bsp.dut.slots import SlotTable, is_slot_ref

ROOT = Path(__file__).resolve().parents[1]


def table(tmp_path, consoles, started=None, **cfg):
    started = [] if started is None else started
    return SlotTable(tmp_path / "slots.json", lambda: consoles, lambda alias, dev: started.append((alias, dev)), cfg)


def test_every_console_is_a_slot_and_slots_outlive_their_adapters(tmp_path):
    consoles = [
        # a daemon whose adapter is gone (re-cabled): an offline slot, kept
        {"id": "board1", "kind": "serial-mux", "alias": "board1", "device": None, "usb_port": "usb-1.3:1.0", "busy": True},
        {"id": "ttyUSB0", "kind": "tty", "device": "/dev/ttyUSB0", "usb_port": "2-1.1"},
        {"id": "ttyUSB9", "kind": "tty", "device": "/dev/ttyUSB9", "usb_port": "2-1.9"},
    ]
    started = []
    t = table(tmp_path, consoles, started, exclude=["/dev/ttyUSB9"])
    change = t.refresh()
    assert change["added"] == ["s1", "s2"] and started == [("s2", "/dev/ttyUSB0")]
    views = {s["id"]: s for s in t.all()}
    assert views["s1"]["alias"] == "board1" and not views["s1"]["online"]
    assert views["s2"]["online"] and views["s2"]["alias"] == "s2"
    # The daemon now owns ttyUSB0: the same port is the same slot, nothing new.
    consoles[1] = {"id": "s2", "kind": "serial-mux", "alias": "s2", "device": "/dev/ttyUSB0", "usb_port": "2-1.1", "busy": True}
    assert t.refresh()["added"] == []
    # Slots and their history survive a restart.
    t.note("s2", "added", dut="k3-a", reset="P33", control="gpio-relay", usb="usb:2-1.2")
    t.note("s2", "removed", dut="k3-a")
    again = table(tmp_path, consoles)
    again.refresh()
    assert again.get("s2")["reset"] == "P33" and again.last_dut("s2") == "k3-a"
    assert again.history_pins() == ["P33"]
    # Only an offline slot nobody uses can be forgotten.
    with pytest.raises(InvalidRequest):
        again.forget("s2", lambda alias: None)
    with pytest.raises(Conflict):
        again.forget("s1", lambda alias: "old-board")
    assert again.forget("dev/s1", lambda alias: None)["forgotten"] == "s1"
    with pytest.raises(NotFound):
        again.get("s1")
    assert is_slot_ref("s3") and is_slot_ref("host-1/s3") and not is_slot_ref("k3-a") and not is_slot_ref("a/b/s3")


class FakeGpio:
    """Header pins; pulsing one makes the consoles wired to it print into their serial-mux logs."""

    def __init__(self, logs, wiring, history_modes=()):
        self.logs, self.wiring, self.pulsed = logs, wiring, []
        self.pins = [{"key": f"P{n}", "target": f"P{n}", "label": f"GPIO{n}", "number": n,
                      "channel": {"mode": "relay" if f"P{n}" in history_modes else "gpio"}} for n in (3, 5, 7, 33)]

    def list_pins(self, gpio_only=True):
        return self.pins

    def resolve(self, ref):
        return types.SimpleNamespace(key=ref)

    def get(self, ref):
        return next(p for p in self.pins if p["target"] == ref)

    def pulse(self, target, ms, level, wait, force):
        self.pulsed.append(target)
        for alias in self.wiring.get(target, []):
            text = "ROMB: hello\nROMB: usb2 init\nxx download handler\n" if isinstance(alias, str) else alias[1]
            name = alias if isinstance(alias, str) else alias[0]
            (self.logs[name] / f"{date.today().isoformat()}.log").open("a").write(text)
        noise = self.wiring.get(f"{target}:noise")
        if noise:
            self.wiring.pop(f"{target}:noise")
            for name in noise:
                (self.logs[name] / f"{date.today().isoformat()}.log").open("a").write("garbage\n")


def fake_host(tmp_path, monkeypatch, wiring, aliases=("s1", "s2"), **probe_cfg):
    from bsp.dut.socs import SocProfiles

    logs = {a: tmp_path / "logs" / a for a in aliases}
    for d in logs.values():
        d.mkdir(parents=True)
    consoles = [{"id": a, "kind": "serial-mux", "alias": a, "device": f"/dev/{a}", "usb_port": f"port-{a}", "busy": True}
                for a in aliases]
    slots = table(tmp_path, consoles)
    slots.refresh()
    gpio = FakeGpio(logs, wiring, history_modes=probe_cfg.pop("relays", ()))
    added = []
    mod = types.SimpleNamespace(
        probe_cfg={"watch_s": 0, **probe_cfg}, socs=SocProfiles([ROOT / "examples" / "site" / "socs"]),
        boards={}, slots=slots, serial_log_dir=str(tmp_path / "logs"), fastboot_bin="fastboot",
        hooks=types.SimpleNamespace(run=lambda kind, name, req, timeout=0, log=None: types.SimpleNamespace(data={"devices": []})),
        ctx=types.SimpleNamespace(host_id="lab-9", service=lambda name: gpio), _event=lambda topic, **d: None)
    mod.refresh_slots = lambda: slots.refresh()

    def slot_list():
        return [{**s, "dut": next((d for d, b in mod.boards.items() if b == s["alias"]), None), "console_up": True}
                for s in slots.all()]

    def add(spec, replace):
        mod.boards[spec["id"]] = spec["console"]["alias"]
        added.append((spec["id"], len(gpio.pulsed)))
        slots.note(spec["console"]["alias"], "added", dut=spec["id"], reset=(spec["control"].get("reset") or {}).get("pin"))
        return {"slot": slots.by_alias(spec["console"]["alias"])["id"]}

    mod.slot_list, mod.add = slot_list, add
    monkeypatch.setattr(probe_mod.time, "sleep", lambda s: None)
    return mod, gpio, added, slots


def test_probe_registers_each_board_as_found_and_tries_historic_lines_first(tmp_path, monkeypatch):
    mod, gpio, added, slots = fake_host(tmp_path, monkeypatch, {"P7": ["s1"], "P33": ["s2"]})
    # s2 held a board on P33 before it was removed; s1 never had one.
    slots.note("s2", "added", dut="k3-deb1-32g", reset="P33")
    slots.note("s2", "removed", dut="k3-deb1-32g")
    lines = []
    res = probe_mod.Prober(mod, None, None, identify=False, default_sdks=None, register=True).run(lines.append)
    assert gpio.pulsed[0] == "P33"                       # the line s2 had comes first
    # Each board was registered right after its line was found, not at the end; s2 got its old name back.
    assert added == [("k3-deb1-32g", 1), ("soc-b-s1", 4)]
    assert [r["slot"] for r in res["registered"]] == ["s2", "s1"]
    assert any(l.startswith("registered k3-deb1-32g in slot s2") for l in lines)
    assert slots.get("s1")["reset"] == "P7"


def test_probe_presses_an_ambiguous_line_again_and_says_why_nothing_was_found(tmp_path, monkeypatch):
    # P33 resets the board on s1, but the first time s2's adapter also printed (it had just re-attached).
    mod, gpio, added, slots = fake_host(tmp_path, monkeypatch, {"P33": ["s1"], "P33:noise": ["s2"]})
    res = probe_mod.Prober(mod, None, None, identify=False, default_sdks=None, register=False).run(lambda l: None)
    assert gpio.pulsed.count("P33") == 2
    assert [p["spec"]["console"]["alias"] for p in res["proposals"]] == ["s1"]
    assert res["reasons"] == ["no reset line made console(s) s2 speak"]
    # --known tries only lines slots had before; there are none yet, so nothing is pressed.
    before = len(gpio.pulsed)
    res = probe_mod.Prober(mod, None, "s2", identify=False, default_sdks=None, known=True).run(lambda l: None)
    assert len(gpio.pulsed) == before and res["proposals"] == []


def test_probe_skips_offline_slots_and_probes_only_the_slots_asked_for(tmp_path, monkeypatch):
    mod, gpio, added, slots = fake_host(tmp_path, monkeypatch, {"P5": ["s2"]}, aliases=("s1", "s2", "s3"))
    slots._live["s3"]["online"] = False
    mod.refresh_slots = lambda: None
    lines = []
    res = probe_mod.Prober(mod, None, "lab-9/s2", identify=False, default_sdks=None).run(lines.append)
    assert [p["spec"]["console"]["alias"] for p in res["proposals"]] == ["s2"]
    assert res["unmatched_consoles"] == [] and res["skipped_consoles"] == []
    res = probe_mod.Prober(mod, None, None, identify=False, default_sdks=None).run(lines.append)
    assert res["skipped_consoles"] == ["s3"] and "skipped offline console(s) s3" in res["reasons"]


def test_identify_says_why_it_was_skipped():
    mod = types.SimpleNamespace(socs=types.SimpleNamespace(get=lambda soc: {"identify": {"sequence": []}}))
    lines = []
    assert probe_mod.identify_board(mod, None, True, "k3", None, lines.append) == {}
    assert lines == ["identify skipped: no download-mode USB port seen"]

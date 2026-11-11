"""Board discovery on one host (`bsp probe`).

Every console on the host is a slot (see slots.py); probing finds the board on each free slot:

  candidates   consoles: the online slots without a DUT (or those named with --slots); a slot whose adapter
                         is gone is skipped. Without slots: hooks/probe/consoles (serial-mux aliases + raw ttys)
               pins   : free header GPIOs from the gpio_relay service, ordered by `probe.pin_order`:
                         `history` (lines a slot had before: re-cabled or re-registered boards come back first),
                         `relay` (lines already set up as relays or buttons), then `number`
               usb     : hooks/probe/usb-devices (download-mode vendor ids)
  correlate    for each pin: pulse it like a reset button, then watch every
               console for new output and every USB port for (re)enumeration.
               A pin that makes exactly one console speak is a reset line; the
               USB port that re-enumerated at the same time is its fastboot port.
  identify     hooks/probe/identify, driven by the detected SoC profile: stages the
               profile's probe images and extracts identity fields -> name hint
  propose      one DUT spec per board; with `register` each is registered the moment it is found (named
               after what identify read, else after the board its slot had last, else `<soc>-<slot>`),
               otherwise the CLI lets a human edit names and registers them with `dut.add`.

A line that made several consoles speak at once is pressed again once every other line was tried
(`probe.retry_ambiguous`, default 1): a console that spoke because its adapter just re-attached, or
a board that happened to print, no longer costs the board its reset line.

With `human`, boards without reset wiring are found instead: no pin is pulsed.
Each round asks a person to put one unregistered board into download mode
(press RESET; hold BOOT too where download mode is not strapped). The console
that speaks and the USB port that enumerates in that round belong together and
become a slot with the `human` control backend, like `bsp dut bind`. A round in
which nothing happens ends the probe.

Without `register`, nothing is registered by the probe job itself.
"""

from __future__ import annotations

import json
import os
import re
import termios
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..core.errors import InvalidRequest, BspError

Log = Callable[[str], None]


class _RawTtyWatcher:
    """Reads a raw tty (not owned by serial-mux) during the observation window."""

    def __init__(self, device: str, baud: int = 115200):
        self.device, self.data, self._stop = device, bytearray(), threading.Event()
        self.fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        speed = getattr(termios, f"B{baud}", termios.B115200)
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = os.read(self.fd, 4096)
                if chunk:
                    self.data += chunk
                    continue
            except BlockingIOError:
                pass
            except OSError:
                break
            time.sleep(0.05)

    def take(self) -> bytes:
        out, self.data = bytes(self.data), bytearray()
        return out

    def close(self) -> None:
        self._stop.set()
        self.thread.join(1)
        os.close(self.fd)


class Prober:
    def __init__(self, mod, pins: str | None, consoles: str | None, identify: bool,
                 default_sdks: list[str] | None, human: bool = False, rounds: int = 8, wait_s: float = 120,
                 register: bool = False, known: bool = False):
        self.mod = mod
        self.cfg = mod.probe_cfg
        self.want_pins = [p.strip() for p in (pins or "").split(",") if p.strip()]
        self.want_consoles = [c.strip().rsplit("/", 1)[-1] for c in (consoles or "").split(",") if c.strip()]
        self.register, self.known = register, known
        self.pin_order = list(self.cfg.get("pin_order") or ["history", "relay", "number"])
        self.retry_ambiguous = int(self.cfg.get("retry_ambiguous", 1))
        self.registered: list[dict[str, Any]] = []
        self.skipped: list[str] = []
        self.identify = identify
        self.default_sdks = default_sdks
        self.human, self.rounds, self.wait_s = human, max(1, int(rounds)), float(wait_s)
        self.watch_s = float(self.cfg.get("watch_s", 3.0))
        self.pulse_ms = int(self.cfg.get("pulse_ms", 1000))
        self.socs = mod.socs
        # USB devices to watch: configured, else every SoC profile's ids, else all devices
        self.vids = self.cfg.get("usb_vids") or self.socs.usb_vids()

    # ------------------------------------------------------- candidates --
    def _gpio(self):
        return self.mod.ctx.service("gpio_relay")

    def _used(self) -> dict[str, set]:
        used: dict[str, set] = {"pins": set(), "consoles": set(), "usb": set()}
        try:
            gpio = self._gpio()
        except BspError:          # a host without GPIO wiring: only human probes make sense there
            gpio = None
        for b in self.mod.boards.values():
            used["usb"].add(b.spec.usb)
            used["consoles"].add(b.spec.console_alias)
            for role in ("reset", "fdl"):
                pin = (b.spec.control.get(role) or {}).get("pin")
                if pin and gpio:
                    used["pins"].add(gpio.resolve(pin).key)
        return used

    def _history(self, gpio) -> list[str]:
        """Pin keys of the reset lines slots had before, most recent first."""
        slots = getattr(self.mod, "slots", None)
        out = []
        for ref in slots.history_pins() if slots else []:
            try:
                out.append(gpio.resolve(ref).key)
            except BspError:
                continue
        return out

    def candidate_pins(self) -> list[dict[str, Any]]:
        gpio, used = self._gpio(), self._used()
        if self.want_pins:
            return [gpio.get(p) for p in self.want_pins]
        history = self._history(gpio)
        out = []
        for p in gpio.list_pins(gpio_only=True):
            mux = p.get("pinmux") or {}
            if p["key"] in used["pins"] or p.get("protected") or mux.get("conflict") or p.get("busy"):
                continue
            if self.known and p["key"] not in history:
                continue
            out.append(p)

        def rank(p: dict[str, Any]) -> tuple:
            keys = []
            for k in self.pin_order:
                if k == "history":
                    keys.append(history.index(p["key"]) if p["key"] in history else len(history))
                elif k == "relay":
                    # pins already set up as relay/button lines are the likeliest resets
                    keys.append(p["channel"]["mode"] == "gpio")
                elif k == "number":
                    keys.append(p["number"])
            return tuple(keys)
        out.sort(key=rank)
        return out

    def candidate_consoles(self, log: Log) -> list[dict[str, Any]]:
        slots = getattr(self.mod, "slots", None)
        if slots is not None:
            return self._slot_consoles(log)
        used = self._used()
        res = self.mod.hooks.run("probe", "consoles", {
            "serial_mux_run": str(Path(self.mod.serial_log_dir).parent / "run")}, timeout=30, log=log)
        cons = []
        for c in res.data.get("consoles", []):
            if c.get("alias") in used["consoles"]:
                continue
            if c.get("kind") == "serial-mux" and not c.get("device"):
                self.skipped.append(c["id"])        # its adapter is gone: it cannot speak
                continue
            cons.append(c)
        if self.skipped:
            log(f"skipped consoles without a device: {', '.join(self.skipped)}")
        if self.want_consoles:
            cons = [c for c in cons if c["id"] in self.want_consoles or c.get("device") in self.want_consoles]
        return cons

    def _slot_consoles(self, log: Log) -> list[dict[str, Any]]:
        """The host's free online slots (or the slots asked for) as consoles."""
        self.mod.refresh_slots()
        out = []
        for s in self.mod.slot_list():
            if self.want_consoles and s["id"] not in self.want_consoles and s.get("alias") not in self.want_consoles:
                continue
            if s.get("dut"):
                if self.want_consoles:
                    log(f"slot {s['id']} holds {s['dut']}: refresh it instead (`bsp dut refresh {s['dut']}`)")
                continue
            if not s["online"] or not s["console_up"]:
                self.skipped.append(s["id"])
                continue
            out.append({"id": s["id"], "slot": s["id"], "kind": "serial-mux", "alias": s["alias"], "device": s["device"],
                        "usb_port": s.get("port"), "log": str(Path(self.mod.serial_log_dir) / s["alias"])})
        if self.skipped:
            log(f"skipped offline slots (adapter gone or console down): {', '.join(self.skipped)}")
        return out

    def usb_snapshot(self) -> dict[str, dict[str, Any]]:
        res = self.mod.hooks.run("probe", "usb-devices", {"vids": self.vids}, timeout=30)
        return {d["usb"]: d for d in res.data.get("devices", [])}

    def plan(self) -> dict[str, Any]:
        lines: list[str] = []
        return {"host": self.mod.ctx.host_id, "human": self.human,
                "pins": [] if self.human else [p["target"] for p in self.candidate_pins()],
                "consoles": self.candidate_consoles(lines.append),
                "usb": list(self.usb_snapshot().values()),
                "existing_duts": sorted(self.mod.boards),
                "identify": self.identify}

    # ------------------------------------------------------------- run --
    def run(self, log: Log) -> dict[str, Any]:
        if self.human:
            return self.run_human(log)
        pins = self.candidate_pins()
        consoles = self.candidate_consoles(log)
        if not consoles:
            raise InvalidRequest("no free serial console on this host",
                                 hint="plug the board's USB-UART in (or start serial-mux for it) and rerun")
        log(f"candidates: {len(pins)} pin(s), {len(consoles)} console(s) "
            f"[{', '.join(c['id'] for c in consoles)}], usb {sorted(self.usb_snapshot())}")
        found: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        pending = {c["id"]: c for c in consoles}
        raw = {c["id"]: _RawTtyWatcher(c["device"]) for c in consoles if c["kind"] == "tty"}
        ambiguous: list[dict[str, Any]] = []

        def found_one(hit: dict[str, Any]) -> None:
            found.append(hit)
            pending.pop(hit["console"]["id"], None)
            proposals.append(self._settle(self._propose(hit, len(found) - 1, log), log))

        try:
            for pin in pins:
                if not pending:
                    break
                hit = self._try_pin(pin, pending, raw, log)
                if hit and hit.get("ambiguous"):
                    ambiguous.append(pin)
                elif hit:
                    found_one(hit)
            for n in range(self.retry_ambiguous):
                again, ambiguous = ambiguous, []
                for pin in again:
                    if not pending:
                        break
                    log(f"{pin['target']}: trying again (several consoles spoke the first time)")
                    hit = self._try_pin(pin, pending, raw, log)
                    if hit and hit.get("ambiguous"):
                        ambiguous.append(pin)
                    elif hit:
                        found_one(hit)
        finally:
            for w in raw.values():
                w.close()
        log(f"correlated {len(found)} board(s); unmatched consoles: {sorted(pending) or 'none'}")
        return self._result(proposals, pending, [p["target"] for p in ambiguous])

    def _result(self, proposals: list[dict[str, Any]], pending: dict, ambiguous: list[str]) -> dict[str, Any]:
        if not self.register:
            self._uniquify(proposals)
        reasons = []
        if ambiguous:
            reasons.append(f"line(s) {', '.join(ambiguous)} made several consoles speak each time")
        if pending:
            reasons.append(f"no reset line made console(s) {', '.join(sorted(pending))} speak")
        if self.skipped:
            reasons.append(f"skipped offline console(s) {', '.join(self.skipped)}")
        return {"host": self.mod.ctx.host_id, "proposals": proposals, "registered": self.registered,
                "unmatched_consoles": list(pending.values()), "ambiguous_pins": ambiguous,
                "skipped_consoles": self.skipped, "reasons": reasons,
                "next": ("registered as found" if self.register else
                         "review names, then `bsp dut add` each proposal (the CLI does this interactively)")}

    def _settle(self, proposal: dict[str, Any], log: Log) -> dict[str, Any]:
        """Register a proposal at once when asked to: the board is usable while the probe goes on."""
        if not self.register:
            return proposal
        spec = proposal["spec"]
        spec["id"] = _unique(spec["id"], set(self.mod.boards))
        try:
            status = self.mod.add(json.loads(json.dumps(spec)), False)
            entry = {"dut": spec["id"], "slot": status.get("slot"), "usb": spec["fastboot"]["usb"]}
            self.registered.append(entry)
            proposal["registered"] = entry
            log(f"registered {spec['id']}" + (f" in slot {entry['slot']}" if entry["slot"] else ""))
        except BspError as e:
            proposal["warnings"].append(f"not registered: {e.message}")
            log(f"{spec['id']} not registered: {e.message}")
        return proposal

    def _try_pin(self, pin: dict[str, Any], pending: dict, raw: dict, log: Log) -> dict[str, Any] | None:
        gpio = self._gpio()
        marks = {cid: self._mark(c) for cid, c in pending.items()}
        for w in raw.values():
            w.take()
        before = self.usb_snapshot()
        level = "low" if pin["channel"]["mode"] == "gpio" else None
        try:
            gpio.pulse(pin["target"], self.pulse_ms, level, True, False)
        except BspError as e:
            log(f"{pin['target']}: skip ({e.message})")
            return None
        time.sleep(self.watch_s)
        spoke, chatter = {}, []
        for cid, c in pending.items():
            text = raw[cid].take().decode("utf-8", "replace") if cid in raw else self._read(c, marks[cid])
            # A console that printed something but not a boot (a shell someone is typing in) was not reset by it.
            if text.strip() and self.socs.looks_like_boot(text):
                spoke[cid] = text
            elif text.strip():
                chatter.append(cid)
        if chatter:
            log(f"{pin['target']}: console(s) {', '.join(chatter)} printed, but not a boot (someone typing?): not counted")
        after = self.usb_snapshot()
        changed = [u for u, d in after.items() if u not in before or before[u].get("devnum") != d.get("devnum")]
        if len(spoke) != 1:
            log(f"{pin['target']} ({pin['label']}): {'no console reacted' if not spoke else 'ambiguous: ' + ', '.join(spoke)}")
            return {"ambiguous": True} if spoke else None
        cid, text = next(iter(spoke.items()))
        fdl = self.socs.in_download_mode(text, " ".join(after[u].get("product", "") for u in changed))
        log(f"{pin['target']} ({pin['label']}) resets console {cid}; usb {changed or 'none'}; "
            f"{'lands in FDL' if fdl else 'no FDL signature'}")
        soc, score = self.socs.detect(text)
        if soc:
            log(f"  boot-ROM fingerprint -> {soc} ({score} marker(s) matched)")
        return {"pin": pin, "console": pending[cid], "usb": changed[0] if len(changed) == 1 else None,
                "usb_candidates": changed, "fdl": fdl, "console_text": text[-400:], "soc": soc}

    # ------------------------------------------------------ human probe --
    def run_human(self, log: Log) -> dict[str, Any]:
        consoles = self.candidate_consoles(log)
        if not consoles:
            raise InvalidRequest("no free serial console on this host",
                                 hint="plug the board's USB-UART in (or start serial-mux for it) and rerun")
        registered = self._used()["usb"]
        log(f"human probe: {len(consoles)} free console(s) [{', '.join(c['id'] for c in consoles)}], "
            f"up to {self.rounds} board(s), {self.wait_s:g}s per board")
        found: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        pending = {c["id"]: c for c in consoles}
        raw = {c["id"]: _RawTtyWatcher(c["device"]) for c in consoles if c["kind"] == "tty"}
        try:
            for n in range(1, self.rounds + 1):
                if not pending:
                    break
                hit, anything = self._human_round(n, pending, raw, registered, log)
                if hit:
                    found.append(hit)
                    pending.pop(hit["console"]["id"], None)
                    proposals.append(self._settle(self._propose(hit, len(found) - 1, log), log))
                    if hit["usb"]:
                        registered.add(hit["usb"])
                elif not anything:
                    log(f"round {n}: nothing happened within {self.wait_s:g}s; done")
                    break
        finally:
            for w in raw.values():
                w.close()
        log(f"paired {len(found)} board(s); consoles left: {sorted(pending) or 'none'}")
        return self._result(proposals, pending, [])

    def _human_round(self, n: int, pending: dict, raw: dict, registered: set, log: Log):
        marks = {cid: self._mark(c) for cid, c in pending.items()}
        for w in raw.values():
            w.take()
        texts = {cid: "" for cid in pending}
        before = self.usb_snapshot()
        log(f"ACTION NEEDED (board {n}): put ONE unregistered board on {self.mod.ctx.host_id} into download mode: "
            f"press its RESET (hold its BOOT/download button too unless download mode is strapped), then wait")
        self.mod._event("attention", dut=f"probe@{self.mod.ctx.host_id}", action="enter download mode",
                        message=f"probe: put one unregistered board into download mode (board {n})")
        deadline, settle_until = time.monotonic() + self.wait_s, None
        while True:
            time.sleep(0.5)
            for cid, c in pending.items():
                texts[cid] += raw[cid].take().decode("utf-8", "replace") if cid in raw else ""
            spoke = {cid: (texts[cid] if cid in raw else self._read(c, marks[cid])) for cid, c in pending.items()}
            spoke = {cid: t for cid, t in spoke.items() if t.strip() and self.socs.looks_like_boot(t)}
            after = self.usb_snapshot()
            changed = [u for u, d in after.items() if u not in registered
                       and (u not in before or before[u].get("devnum") != d.get("devnum"))]
            now = time.monotonic()
            if spoke and changed and settle_until is None:
                settle_until = now + 3.0          # let the rest of the banner and enumeration arrive
            if (settle_until and now >= settle_until) or now > deadline:
                break
        if not spoke and not changed:
            return None, False
        if len(spoke) != 1:
            log(f"round {n}: {'no console reacted' if not spoke else 'several consoles reacted: ' + ', '.join(spoke)}"
                f"; usb {changed or 'none'} — try again with one board at a time")
            return None, True
        cid, text = next(iter(spoke.items()))
        fdl = self.socs.in_download_mode(text, " ".join(after[u].get("product", "") for u in changed))
        soc, _ = self.socs.detect(text)
        log(f"round {n}: console {cid} reacted; usb {changed or 'none'}; {'download mode' if fdl else 'no download-mode signature'}"
            + (f"; SoC {soc}" if soc else ""))
        return ({"pin": None, "console": pending[cid], "usb": changed[0] if len(changed) == 1 else None,
                 "usb_candidates": changed, "fdl": fdl, "console_text": text[-400:], "soc": soc}, True)

    def _mark(self, c: dict[str, Any]):
        if c["kind"] != "serial-mux":
            return None
        from datetime import date

        p = Path(c["log"]) / f"{date.today().isoformat()}.log"
        return p, (p.stat().st_size if p.exists() else 0)

    def _read(self, c: dict[str, Any], mark) -> str:
        if not mark:
            return ""
        p, off = mark
        return p.read_bytes()[off:].decode("utf-8", "replace") if p.exists() else ""

    # --------------------------------------------------------- propose --
    def _propose(self, hit: dict[str, Any], index: int, log: Log) -> dict[str, Any]:
        soc = hit.get("soc") or self.cfg.get("default_soc")
        ident = identify_board(self.mod, hit["usb"], hit["fdl"], soc, hit["console"].get("log"), log) if self.identify else {}
        con = hit["console"]
        alias = con.get("alias")
        sid = con.get("slot")
        # A board found on a slot that had one before most likely is that board again (re-cabled, or re-registered).
        pin = (hit.get("pin") or {}).get("target")
        name = ident.get("name_hint") or (self._name_on(pin) if pin else None) or \
            (self._last_name(sid) if sid else None) or f"{soc or 'unknown'}-{sid or 'board'}"
        # empty = every SDK target of this SoC (resolved by the host at use time)
        sdks = self.default_sdks or (self.cfg.get("default_sdks") or {}).get(soc, [])
        spec = {
            "id": name,
            "board": (ident.get("fields") or {}).get("product") or "unknown",
            "soc": soc,
            "description": (ident.get("fields") or {}).get("model") or "probed board",
            "console": {"alias": alias or name, **({"device": con["device"], "start": True} if not alias else {}),
                        "usb_port": con.get("usb_port")},
            "fastboot": {"usb": hit["usb"] or "usb:UNKNOWN"},
            "control": ({"backend": "gpio-relay",
                         "reset": {"pin": hit["pin"]["target"], "mode": "button", "pulse_ms": self.pulse_ms},
                         # A reset that did not land in download mode: the board needs an FDL line nobody has found
                         # yet, or its strap is the other way; a person says which (`bsp dut set --fdl`).
                         "fdl": {"strategy": "strapped" if hit["fdl"] else "unset"}} if hit["pin"] else
                        {"backend": "human", "fdl": {"strategy": "strapped"}}),
            "sdks": sdks,
            "identity": ident,
            "probed": {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "gpio": hit["pin"]["label"] if hit["pin"] else None,
                       "human": not hit["pin"],
                       "usb_candidates": hit["usb_candidates"]},
        }
        warnings = []
        if not hit["usb"]:
            warnings.append("no unique USB port re-enumerated: set fastboot.usb by hand (`fastboot devices -l`)")
        if not hit["fdl"] and hit["pin"]:
            warnings.append("reset did not land in download mode: say how it gets there "
                            "(`bsp dut set <dut> --fdl strapped`, or `--fdl button --fdl-pin <pin>`)")
        if not hit["fdl"] and not hit["pin"]:
            warnings.append("no download-mode signature seen: check with `bsp dut status` after the first reset")
        return {"spec": spec, "warnings": warnings, "evidence": hit["console_text"]}

    def _name_on(self, pin: str) -> str | None:
        """The board this reset line belonged to before (after re-cabling, the same board is likely still on it)."""
        slots = getattr(self.mod, "slots", None)
        return slots.name_on(pin) if slots is not None else None

    def _last_name(self, sid: str) -> str | None:
        try:
            return self.mod.slots.last_dut(sid)
        except BspError:
            return None

    @staticmethod
    def _uniquify(proposals: list[dict[str, Any]]) -> None:
        seen: dict[str, int] = {}
        for p in proposals:
            base = p["spec"]["id"]
            seen[base] = seen.get(base, 0) + 1
        counter: dict[str, int] = {}
        for p in proposals:
            base = p["spec"]["id"]
            if seen[base] > 1:
                counter[base] = counter.get(base, 0) + 1
                p["spec"]["id"] = f"{base}-{counter[base]}"


def _unique(name: str, taken: set) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def identify_board(mod, usb: str | None, in_download: bool, soc: str | None, console_log: str | None, log: Log) -> dict[str, Any]:
    """Run the SoC profile's identify sequence on a board in download mode; {} when it cannot run (says why)."""
    prof = mod.socs.get(soc)
    why = (None if usb and "UNKNOWN" not in usb else "no download-mode USB port seen") or \
          (None if in_download else "the board is not in download mode") or \
          (None if prof.get("identify") else f"no SoC profile with an identify section for {soc or 'this board'}")
    if why:
        log(f"identify skipped: {why}")
        return {}
    req = {"usb": usb, "fastboot": mod.fastboot_bin, "soc": soc, "console_log": console_log, "profile": prof, "timeout": 60}
    try:
        res = mod.hooks.run("probe", "identify", req, timeout=180, log=log)
        return res.data if res.applicable else {}
    except BspError as e:
        log(f"identify failed: {e.message} (keeping a generic name)")
        return {}

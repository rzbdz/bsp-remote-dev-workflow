"""dut module, the DUT manager of a board host: the DUTs attached to it — registry, leases,
reset/FDL, fastboot boot (load / download), serial console, and auto-discovery (probe).

Each DUT (`Board`) is the three services of the host bound to one board:
  relay controller   its control backend (`control.py`): gpio_relay lines, or a person
  download path      fastboot on its USB port and the artifact cache (`hostio.py`, `boot.py`)
  serial console     its serial-mux daemon (`consoles.py`, `hostio.SerialConsole`)
Commands (`commands.py`) are the atomic layer under `bsp dut ...` / `bsp probe`."""

from __future__ import annotations

import logging
import os
import re

from typing import Any

from ..core.commands import Command
from ..core.errors import Conflict, HardwareError, InvalidRequest, BspError
from ..core.hooks import HookRunner
from ..core.module import MODULES, Module, ModuleContext
from ..sdk.catalog import SdkCatalog
from .boot import BootRunner
from .catalog import DutRegistry, DutSpec
from .commands import build_commands
from .control import make_control
from .hostio import ArtifactStore, Fastboot, SerialConsole
from .mediaboot import MediaBoot
from .leases import LeaseManager

class Board:
    """Runtime handles for one DUT (built from its spec)."""

    def __init__(self, spec: DutSpec, mod: "DutModule"):
        self.spec = spec
        self.control = make_control(spec, mod.ctx.service)
        self.fastboot = Fastboot(spec.usb, mod.fastboot_bin)
        self.console = SerialConsole(spec.console_alias, mod.smtty, mod.serial_log_dir)

    def runner(self, mod: "DutModule") -> BootRunner:
        expect = (self.spec.control.get("fdl") or {}).get("expect") or mod.socs.download_mode(self.spec.soc)
        return BootRunner(self.spec.id, self.control, self.fastboot, self.console, mod.store, mod.hooks, expect,
                          media=self.media(mod))

    def media(self, mod: "DutModule") -> MediaBoot:
        """Booting its media although its reset lands in download mode (see mediaboot.py)."""
        return MediaBoot(self.spec, mod.socs.get(self.spec.soc))


log = logging.getLogger(__name__)


@MODULES.register("dut")
class DutModule(Module):
    name = "dut"
    title = "DUTs"

    def __init__(self, ctx: ModuleContext, config: dict[str, Any]):
        super().__init__(ctx, config)
        from ..core.site import layered

        base = ctx.config_dir
        site = (base / config.get("site", "..")).resolve()    # <site>/hosts/<id>.yaml -> <site>
        state = ctx.module_state_dir("dut")
        self.smtty = config.get("smtty", "smtty")
        self.serial_mux = config.get("serial_mux", "serial-mux")
        self.serial_log_dir = config.get("serial_log_dir", os.path.expanduser("~/.serial-mux/logs"))
        self.fastboot_bin = config.get("fastboot", "fastboot")
        self.probe_cfg = config.get("probe") or {}
        # The site's catalog, under the registry's copy when this host follows one (see server/catalog_follow.py).
        self._sdk_roots = layered("sdk", site)
        self._catalog_layer = ctx.state_dir / "catalog"
        # SoC profiles and hooks: packs, the site, then the registry's shared files above them (catalog_follow.py)
        self._site = site
        self._files_layer = ctx.state_dir / "catalog-files"
        self.sdks = self._load_catalog()
        ctx.bus.on("catalog.updated", lambda ev: self.reload_catalog())
        self._hook_dirs = tuple(str((base / d).resolve()) for d in config.get("hook_dirs", []))
        self._load_socs()
        self.registry = DutRegistry(ctx.host_id, site / "duts",
                                    state / "duts.json")
        self.leases = LeaseManager(state / "leases.json")
        self.store = ArtifactStore(state / "artifacts")
        from .consoles import ConsoleManager

        self.consoles = ConsoleManager(self.serial_mux, self.serial_log_dir)
        from .slots import SlotTable

        self.slots = SlotTable(state / "slots.json", self._list_consoles,
                               lambda alias, device: self.consoles.ensure(alias, device), config.get("slots"))
        self.boards: dict[str, Board] = {}
        self._busy: dict[str, str] = {}
        self.port_watcher = None
        from .control import Attention
        from .sshlogin import ConsoleLogins

        self.attention = Attention(lambda topic, **d: self._event(topic, **d))
        ctx.services["dut_attention"] = self.attention

        self.logins = ConsoleLogins(config.get("ssh_console"), ctx.host_id, ctx.port, self.smtty)
        self._rebuild()

    def _rebuild(self) -> None:
        self.boards = {i: Board(s, self) for i, s in sorted(self.registry.all().items())}
        self._sync_logins()

    def _sync_logins(self) -> None:
        """Keep `ssh <prefix><dut>@host` console logins in step with the registry."""
        if not self.logins.enabled and not self.logins.last and not self._had_logins():
            return
        try:
            res = self.logins.sync({i: b.console.alias for i, b in self.boards.items()})
            if res["added"] or res["removed"] or res["renamed"]:
                self._event("logins", **{k: res[k] for k in ("added", "removed", "renamed")})
        except Exception as e:  # noqa: BLE001 - logins are a convenience; never block the registry
            self.logins.last = {"error": str(e)}
            log.warning("console logins: %s", e)

    def _load_socs(self) -> None:
        from ..core.site import layered
        from .socs import SocProfiles

        files = [self._files_layer / k for k in ("socs", "hooks")]
        self.socs = SocProfiles(layered("socs", self._site) + ([files[0]] if files[0].is_dir() else []))
        hooks = layered("hooks", self._site) + ([files[1]] if files[1].is_dir() else [])
        self.hooks = HookRunner(self._hook_dirs + tuple(str(d) for d in reversed(hooks)))

    def _load_catalog(self) -> SdkCatalog:
        return SdkCatalog(self._sdk_roots + ([self._catalog_layer] if self._catalog_layer.is_dir() else []))

    def reload_catalog(self) -> None:
        """Take the registry's new catalog; a catalog that does not load leaves the old one in place."""
        try:
            self.sdks = self._load_catalog()
            self._load_socs()
            self._event("catalog", sdks=len(self.sdks.instances.names()), socs=sorted(self.socs.profiles))
        except BspError as e:
            self._event("error", error=e.to_dict(), what="catalog reload")
            log.warning("catalog reload failed, keeping the previous one: %s", e.message)

    def catalog_rev(self) -> int:
        from ..registry.catalog import layer_rev

        return layer_rev(self._catalog_layer)

    def login_list(self) -> dict[str, Any]:
        rows = [{"login": u, "dut": d} for u, d in sorted(self.logins._ours().items())]
        return {"enabled": self.logins.enabled, "logins": rows, "error": self.logins.last.get("error")}

    def _had_logins(self) -> bool:
        try:
            return bool(self.logins._ours())
        except Exception:  # noqa: BLE001
            return False

    # -------------------------------------------------------------- slots --
    def _list_consoles(self) -> list[dict[str, Any]]:
        from pathlib import Path

        res = self.hooks.run("probe", "consoles", {"serial_mux_run": str(Path(self.serial_log_dir).parent / "run")}, timeout=30)
        return res.data.get("consoles", [])

    def refresh_slots(self) -> dict[str, Any]:
        """Match the host's consoles to slots now (new adapters get a slot and a console of their own)."""
        try:
            change = self.slots.refresh(lambda alias: next((i for i, b in self.boards.items() if b.spec.console_alias == alias), None))
        except BspError as e:
            self._event("error", error=e.to_dict(), what="slot refresh")
            return {"error": e.message}
        for slot in change.get("gone", []):
            if slot.get("alias"):
                self.consoles.stop(slot["alias"])
        if change["added"] or change["failed"] or change.get("gone"):
            self._event("slots", **change)
        return change

    def dut_in(self, slot: dict[str, Any]) -> str | None:
        """The DUT registered on a slot's console."""
        return next((i for i, b in self.boards.items() if slot.get("alias") and b.spec.console_alias == slot["alias"]), None)

    def slot_of(self, dut: str) -> str | None:
        b = self.boards.get(dut)
        slot = self.slots.by_alias(b.spec.console_alias) if b else None
        return slot["id"] if slot else None

    def resolve(self, ref: str) -> str:
        """A DUT by its name, or by its slot (`s3`, `<host>/s3`)."""
        from .slots import is_slot_ref

        if ref in self.boards or not is_slot_ref(ref):
            return ref
        host, _, sid = ref.rpartition("/")
        if host and host != self.ctx.host_id:
            raise InvalidRequest(f"slot {ref} is on {host}, not on {self.ctx.host_id}")
        dut = self.dut_in(self.slots.get(sid))
        if not dut:
            raise InvalidRequest(f"slot {sid} on {self.ctx.host_id} has no board registered",
                                 hint=f"probe it (`bsp probe -r {self.ctx.host_id} --slots {sid}`), or open its console (`bsp slot console {self.ctx.host_id}/{sid}`)")
        return dut

    def slot_list(self) -> list[dict[str, Any]]:
        return [self.slot_status(s["id"]) for s in self.slots.all()]

    def slot_status(self, slot: str) -> dict[str, Any]:
        s = self.slots.view(self.slots.get(slot))
        dut = self.dut_in(s)
        return {**s, "host": self.ctx.host_id, "ref": f"{self.ctx.host_id}/{s['id']}", "dut": dut,
                "lease": self.leases.view(dut) if dut else None,
                "console_socket": str(self.consoles.socket(s["alias"])) if s.get("alias") else None,
                "console_up": bool(s.get("alias")) and self.consoles.up(s["alias"]),
                "console_attach": f"{self.smtty} {s['alias']}" if s.get("alias") else None}

    def slot_forget(self, slot: str) -> dict[str, Any]:
        out = self.slots.forget(slot, lambda alias: next((i for i, b in self.boards.items() if b.spec.console_alias == alias), None))
        if out.get("alias"):
            self.consoles.stop(out["alias"])
        self._event("slots", forgotten=out["forgotten"])
        return out

    # ------------------------------------------------------------ helpers --
    def board(self, dut: str) -> Board:
        if dut in self.boards:
            return self.boards[dut]
        import difflib

        close = difflib.get_close_matches(dut, list(self.boards), n=3, cutoff=0.4)
        from ..core.errors import NotFound

        raise NotFound(f"no DUT {dut!r} on host {self.ctx.host_id}",
                       hint=(f"did you mean: {', '.join(close)}? " if close else "")
                       + f"DUTs here: {', '.join(self.boards) or '(none — run `bsp probe`)'}")

    def _event(self, topic: str, **data: Any) -> None:
        self.ctx.bus.publish(f"dut.{topic}", **data)

    def _guard_busy(self, dut: str) -> None:
        if dut in self._busy:
            raise Conflict(f"{dut} is busy: {self._busy[dut]}", hint="wait for the running job (`bsp job list`)")

    def _start_job(self, dut: str, what: str, body, **meta):
        self._busy[dut] = what

        def wrapped(log):
            try:
                return body(log)
            finally:
                self._busy.pop(dut, None)

        return self.ctx.jobs.start(meta.pop("kind", "dut"), f"{dut}: {what}", wrapped, dut=dut, **meta)

    # ----------------------------------------------------------- commands --
    def status(self, dut: str | None = None) -> dict[str, Any]:  # type: ignore[override]
        if dut is None:   # module health (hub status)
            return {"duts": sorted(self.boards), "hooks": {k: self.hooks.names(k) for k in ("probe", "flash")}}
        b = self.board(dut)
        try:
            fb = b.fastboot.state()
        except BspError as e:
            fb = {"error": e.message}
        try:
            ctl = b.control.describe()
        except BspError as e:
            ctl = {"error": e.message}
        try:
            tail = b.console.tail(4)
        except OSError:
            tail = []
        return {**b.spec.summary(), "sdks": self.effective_sdks(b.spec), "sdks_explicit": bool(b.spec.sdks),
                "origin": self.registry.origin(dut), "lease": self.leases.view(dut),
                "busy": self._busy.get(dut), "fastboot": fb,
                "stage": ("needs-human" if self.attention.view(dut)
                          else _stage(fb, self.socs.download_mode(b.spec.soc).get("fastboot"))),
                "needs_human": self.attention.view(dut),
                "control_state": ctl, "console_tail": tail,
                "console_attach": f"{self.smtty} {b.console.alias}",     # interactive: run this on the host
                "console_socket": str(self.consoles.socket(b.console.alias)),   # `bsp dut pipe` connects here
                "console_up": self.consoles.up(b.console.alias), "slot": self.slot_of(dut),
                # its slot's adapter is plugged in (False: unplugged or moved, the host may need a rebuild)
                "slot_online": self._slot_online(b),
                # how a plain reset boots its media: `reset` (FDL not strapped), the SoC's media boot, or not at all
                "media_boot": self._media_state(b)}

    def _slot_online(self, b: Board) -> bool | None:
        slot = self.slots.by_alias(b.console.alias)
        return self.slots.view(slot)["online"] if slot else None

    def _media_state(self, b: Board) -> dict[str, Any]:
        if (b.spec.control.get("fdl") or {}).get("strategy", "strapped") != "strapped":
            return {"method": "reset", "available": True}
        m = b.media(self)
        why = m.unavailable()
        return {"method": m.name, "available": why is None, **({"why": why} if why else {})}

    def list(self) -> list[dict[str, Any]]:
        return [self.status(d) for d in self.boards]

    def acquire(self, dut: str, holder: str, ttl: int, purpose: str, lease: str | None) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.acquire(dut, holder, ttl, purpose, lease)
        self._event("leased", dut=dut, holder=holder, purpose=purpose)
        return {"dut": dut, "host": self.ctx.host_id,
                **{k: out[k] for k in ("token", "holder", "purpose", "expires_in_s")}}

    def release(self, dut: str, lease: str | None, force: bool) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.release(dut, lease, force)
        self._event("released", dut=dut, forced=force)
        return out

    def reset(self, dut: str, lease: str | None, fdl: bool) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)

        # A plain reset of a board whose reset lands in download mode boots its media when the SoC has a way to.
        media = None if fdl else b.media(self)
        media = media if media and media.available() else None

        def body(log) -> dict[str, Any]:
            booted = None
            if fdl:
                b.runner(self).enter_fdl(log)
            elif media:
                booted = media.run(b.runner(self), log)
            else:
                mark = b.console.mark()
                b.control.reset(log, self._reset_evidence(b, mark))
            self._event("reset", dut=dut, fdl=fdl, media_boot=bool(booted))
            return {"dut": dut, "fdl": fdl, "fastboot": b.fastboot.state(), **({"media_boot": booted} if booted else {})}

        if b.control.needs_human or media:   # may wait minutes for a person or a boot: run as a job
            what = "reset (waiting for a person)" if b.control.needs_human else f"boot from media ({media.name})"
            job = self._start_job(dut, what, body, kind="reset")
            return {"dut": dut, "fdl": fdl, "job": job.id, "needs_human": b.control.needs_human,
                    "media_boot": media.name if media else None, "host": self.ctx.host_id}
        lines: list[str] = []
        out = body(lines.append)
        return {**out, "log": lines}

    def _reset_evidence(self, b: "Board", mark):
        """For a plain reset: the SoC's boot-ROM fingerprint appearing on the console."""
        fps = [re.compile(f) for f in (self.socs.get(b.spec.soc) or {}).get("fingerprint", [])] if b.spec.soc else []

        def seen() -> str | None:
            text = b.console.read_since(mark) if fps else ""
            hit = next((f.pattern for f in fps if f.search(text)), None)
            return f"the boot ROM on the console (/{hit}/)" if hit else None
        return seen if fps else None

    def confirm(self, dut: str, note: str) -> dict[str, Any]:
        self.board(dut)
        return self.attention.confirm(dut, note or "")

    def boot(self, dut: str, sdk: str, method: str, variant: str | None, lease: str | None) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        inst = self.sdks.get(sdk)
        if inst.soc and b.spec.soc and inst.soc != b.spec.soc:
            raise InvalidRequest(f"sdk {sdk} is for {inst.soc}, but {dut} is a {b.spec.soc} board ({b.spec.board})",
                                 hint=f"SDKs for {b.spec.soc}: {', '.join(self.effective_sdks(b.spec)) or '(none)'}")
        if sdk not in self.effective_sdks(b.spec):
            raise InvalidRequest(f"{dut} ({b.spec.board}) is not declared to run sdk {sdk}",
                                 hint=f"supported: {', '.join(self.effective_sdks(b.spec))}; "
                                      f"change with `bsp dut set {dut} --sdks ...` (empty = all SDKs of its SoC)")
        inst.boot_plan(method, variant)       # validate first: todo methods fail here with their notes
        runner = b.runner(self)
        rev = self.catalog_rev()

        hub_follower = self.ctx.services.get("catalog_follower")
        overridden = [x for x in (hub_follower.shadowed if hub_follower else []) if x["ref"] == sdk]

        def body(log):
            log(f"recipe from catalog revision {rev}" if rev else "recipe from the site's catalog")
            for x in overridden:
                log(f"NOTE: {x['file']} on this host differs from the registry's {sdk} and is NOT used; "
                    f"publish it with `bsp catalog put {sdk} -f <file>` if it is the newer one")
            return runner.run(inst, method, variant, log)

        job = self._start_job(dut, f"boot {sdk} [{method}]", body,
                              kind="boot", sdk=sdk, method=method)
        self._event("boot", dut=dut, sdk=sdk, method=method, job=job.id)
        return {"job": job.id, "dut": dut, "sdk": sdk, "method": method, "host": self.ctx.host_id}

    def console(self, dut: str, cmd: str, expect: str | None, timeout: float, lease: str | None) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        out = b.console.run(cmd, expect or self._prompt_for(b), timeout)
        self._event("console", dut=dut, cmd=cmd)
        return {"dut": dut, **out}

    def renew(self, dut: str, lease: str | None, ttl: int) -> dict[str, Any]:
        self.board(dut)
        out = self.leases.renew(dut, lease, ttl)
        return {"dut": dut, "host": self.ctx.host_id,
                **{k: out[k] for k in ("token", "holder", "purpose", "expires_in_s")}}

    def present(self, dut: str) -> dict[str, Any]:
        b = self.board(dut)
        fb = b.fastboot.state()
        return {"dut": dut, "usb": b.spec.usb, "present": fb is not None, "fastboot": fb}

    def stage(self, dut: str, sha256: str, lease: str | None, cont: bool, timeout: float) -> dict[str, Any]:
        """Load one uploaded file into RAM through the download port (fastboot stage [+ continue])."""
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        path = self.store.get(sha256)
        self._busy[dut] = f"stage {path.name}"
        try:
            out = b.fastboot.cmd("stage", str(path), timeout=timeout)
            continued = False
            if cont:
                try:
                    out += "\n" + b.fastboot.cmd("continue", timeout=30)
                except HardwareError as e:      # the responder may detach on purpose
                    out += f"\n(continue: {e.message})"
                continued = True
        finally:
            self._busy.pop(dut, None)
        self._event("staged", dut=dut, sha256=sha256, file=path.name, continued=continued)
        return {"dut": dut, "sha256": sha256, "file": path.name, "size": path.stat().st_size,
                "continued": continued, "output": out}

    def boot_files(self, dut: str, files: str, lease: str | None, reset: bool, first_wait: float,
                   later_wait: float, port_timeout: float) -> dict[str, Any]:
        b = self.board(dut)
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        shas = [s.strip() for s in (files or "").split(",") if s.strip()]
        if not shas:
            raise InvalidRequest("no files to boot", hint="files = comma list of sha256 of uploaded files, in boot order")
        items = []
        for sha in shas:
            p = self.store.get(sha)
            items.append({"sha256": sha, "path": str(p), "name": p.name, "size": p.stat().st_size})
        runner = b.runner(self)
        names = " ".join(i["name"] for i in items)
        job = self._start_job(dut, f"boot files {names}",
                              lambda log: runner.run_files(items, log, reset=reset, first_wait=first_wait,
                                                           later_wait=later_wait, port_timeout=port_timeout),
                              kind="boot", files=[i["sha256"] for i in items])
        self._event("boot", dut=dut, files=names, job=job.id)
        return {"job": job.id, "dut": dut, "files": [{k: i[k] for k in ("name", "sha256", "size")} for i in items],
                "host": self.ctx.host_id}

    def artifact_has(self, sha256: str) -> dict[str, Any]:
        p = self.store.find(sha256)
        return {"sha256": sha256, "present": p is not None, "size": p.stat().st_size if p else None,
                "file": p.name if p else None}

    def router(self):
        from .routes import artifact_router

        return artifact_router(self)

    def effective_sdks(self, spec: DutSpec) -> list[str]:
        """Declared sdks, else every SDK instance for the board's SoC."""
        if spec.sdks:
            return list(spec.sdks)
        return sorted(i for i, inst in self.sdks.instances.items() if spec.soc and inst.soc == spec.soc)

    def _prompt_for(self, b: Board) -> str:
        explicit = b.spec.console.get("prompt")
        if explicit:
            return explicit
        prompts = {self.sdks.get(s).prompt for s in self.effective_sdks(b.spec) if s in self.sdks.instances}
        return "|".join(sorted(prompts)) or "# $"

    def log(self, dut: str, lines: int, grep: str | None) -> dict[str, Any]:
        return {"dut": dut, "lines": self.board(dut).console.tail(lines, grep)}

    def logs(self, dut: str, since: str | None = None, until: str | None = None, grep: str | None = None,
             regex: bool = False, limit: int = 20000, first: bool = False) -> dict[str, Any]:
        """The console's log over a time range, searched (see SerialConsole.range)."""
        if regex and grep:
            try:
                re.compile(grep)
            except re.error as e:
                raise InvalidRequest(f"not a valid regex: {e}") from None
        return {"dut": dut, **self.board(dut).console.range(since, until, grep, regex, max(1, min(int(limit), 200000)), first)}

    def add(self, spec: Any, replace: bool) -> dict[str, Any]:
        import json

        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except ValueError as e:
                raise InvalidRequest(f"spec is not valid JSON: {e}") from None
        if not isinstance(spec, dict):
            raise InvalidRequest("spec must be a JSON object (see examples/site/duts/*.yaml for the shape)")
        con = spec.get("console") or {}
        start = con.pop("start", False)
        s = self.registry.add(spec, replace)
        if start or con.get("device"):
            self.consoles.ensure(con["alias"], con.get("device"))
        self._rebuild()
        self.boards[s.id].control.setup()
        if not self.slots.by_alias(s.console_alias):
            self.refresh_slots()
        self._note_slot(s, "added")
        self._event("added", dut=s.id, slot=self.slot_of(s.id))
        return self.status(s.id)

    def _note_slot(self, s: DutSpec, event: str) -> None:
        reset = (s.control.get("reset") or {}).get("pin")
        self.slots.note(s.console_alias, event, dut=s.id, board=s.board or None, soc=s.soc or None,
                        reset=reset, control=s.control.get("backend"),
                        usb=s.usb if s.usb and "UNKNOWN" not in s.usb else None)

    def _free_for_edit(self, dut: str, lease: str | None) -> None:
        cur = self.leases.view(dut)
        if cur:
            self.leases.check(dut, lease)        # only the holder may edit a leased DUT
        self._guard_busy(dut)

    def rename(self, dut: str, new: str, lease: str | None, console: bool) -> dict[str, Any]:
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", new or ""):
            raise InvalidRequest(f"invalid DUT name {new!r}", hint="letters, digits, _ . - (max 63)")
        self._free_for_edit(dut, lease)
        old = self.board(dut).spec
        raw = {**old.raw, "id": new}
        renamed_console = None
        if console or old.console_alias == old.id:
            raw["console"] = {**old.console, "alias": new}
            renamed_console = (old.console_alias, new)
        self.registry.update(dut, raw)
        self._drop_aliases(dut)
        if renamed_console:
            self.consoles.rename(*renamed_console, old.console.get("device"))
            self.slots.rename_alias(*renamed_console)
        lease_info = self.leases.view(dut)
        if lease_info:
            self.leases.release(dut, lease, force=True)
        self._rebuild()
        self.boards[new].control.setup()
        self.slots.note(self.boards[new].spec.console_alias, "renamed", dut=new, old=dut)
        self._event("renamed", dut=new, old=dut)
        return {**self.status(new), "renamed_from": dut,
                "note": "lease dropped by rename; acquire the new name" if lease_info else None}

    def set_fields(self, dut: str, lease: str | None, **fields: Any) -> dict[str, Any]:
        self._free_for_edit(dut, lease)
        spec = self.board(dut).spec
        raw = {**spec.raw, "console": dict(spec.console), "fastboot": dict(spec.fastboot),
               "control": {**spec.control, "reset": dict(spec.control.get("reset") or {}),
                           "fdl": dict(spec.control.get("fdl") or {})}}
        changed = []
        for key in ("board", "soc", "description"):
            if fields.get(key) is not None:
                raw[key] = fields[key]
                changed.append(key)
        if fields.get("sdks") is not None:
            raw["sdks"] = [x for x in fields["sdks"].split(",") if x.strip()]
            unknown = [x for x in raw["sdks"] if x not in self.sdks.instances]
            if unknown:
                raise InvalidRequest(f"unknown sdk(s) {unknown}", hint=f"known: {', '.join(self.sdks.instances.names())}")
            changed.append("sdks")
        if fields.get("usb"):
            raw["fastboot"]["usb"] = fields["usb"]
            changed.append("usb")
        if fields.get("reset_pin"):
            raw["control"]["reset"]["pin"] = fields["reset_pin"]
            changed.append("reset_pin")
        if fields.get("pulse_ms"):
            raw["control"]["reset"]["pulse_ms"] = int(fields["pulse_ms"])
            changed.append("pulse_ms")
        if fields.get("fdl"):
            raw["control"]["fdl"]["strategy"] = fields["fdl"]
            changed.append("fdl")
        if fields.get("fdl_pin"):
            raw["control"]["fdl"]["pin"] = fields["fdl_pin"]
            changed.append("fdl_pin")
        if not changed:
            raise InvalidRequest("nothing to change", hint="see `bsp dut set --help`")
        self.registry.update(dut, raw)
        self._drop_aliases(dut)
        self._rebuild()
        self.boards[dut].control.setup()
        self._event("updated", dut=dut, fields=changed)
        return {**self.status(dut), "changed": changed}

    def export(self) -> dict[str, Any]:
        keep = ("id", "host", "board", "soc", "description", "console", "fastboot", "control", "sdks", "identity")
        return {"duts": [{k: s.raw[k] for k in keep if k in s.raw} for s in self.registry.all().values()]}

    def _drop_aliases(self, dut: str) -> None:
        try:
            self.ctx.service("gpio_relay").alias_delete(dut, prefix=True)
        except BspError:
            pass

    def remove(self, dut: str, force: bool) -> dict[str, Any]:
        if self.leases.view(dut) and not force:
            raise Conflict(f"{dut} is leased", hint="release it first or pass force=true")
        s = self.registry.remove(dut)
        self._drop_aliases(dut)
        self._rebuild()
        self.slots.note(s.console_alias, "removed", dut=s.id, board=s.board or None)
        self._event("removed", dut=dut)
        return {"removed": s.id}

    def probe(self, pins: str | None, consoles: str | None, identify: bool, sdks: str | None,
              dry_run: bool, human: bool = False, rounds: int = 8, wait_s: float = 120,
              register_found: bool = False, known: bool = False) -> dict[str, Any]:
        from .probe import Prober

        prober = Prober(self, pins=pins, consoles=consoles, identify=identify,
                        default_sdks=[s for s in (sdks or "").split(",") if s] or None,
                        human=human, rounds=rounds, wait_s=wait_s, register=register_found, known=known)
        if dry_run:
            return prober.plan()
        job = self.ctx.jobs.start("probe", f"{self.ctx.host_id}: probe", prober.run, host=self.ctx.host_id)
        return {"job": job.id, "host": self.ctx.host_id}

    def rebuild(self, identify: bool = True, sdks: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Start the host's wiring over after it was re-cabled: forget every probed DUT and every slot, give each
        adapter plugged in now a fresh slot and console, then probe them all (reset lines boards used before
        first, the boards' old names kept when identify reads the same board) and register each board as found.
        DUTs from the site's catalog stay. Nothing starts while a DUT is leased or busy."""
        held = sorted(d for d in self.boards if self.leases.view(d) or self._busy.get(d))
        if held:
            raise Conflict(f"{', '.join(held)} {'is' if len(held) == 1 else 'are'} leased or busy",
                           hint="a rebuild resets every board on the host: release them first")
        probed = sorted(d for d in self.boards if self.registry.origin(d) == "probed")
        static = {self.boards[d].spec.console_alias for d in self.boards if d not in probed}
        plan = {"host": self.ctx.host_id, "forget_duts": probed, "keep_duts": sorted(set(self.boards) - set(probed)),
                "slots": [s["id"] for s in self.slots.all()]}
        if dry_run:
            return plan

        def body(log) -> dict[str, Any]:
            from .probe import Prober

            for dut in probed:
                s = self.registry.remove(dut)
                self._drop_aliases(dut)
                self.slots.note(s.console_alias, "removed", dut=s.id, board=s.board or None)
                log(f"forgot {dut} (was on console {s.console_alias})")
            self._rebuild()
            dropped = self.slots.rebuild(lambda slot: slot.get("alias") in static)["dropped"]
            for slot in dropped:
                if slot.get("alias"):
                    self.consoles.stop(slot["alias"])
            log(f"dropped {len(dropped)} slot(s); matching the adapters plugged in now")
            change = self.refresh_slots()
            log(f"slots now: {', '.join(s['id'] + ('' if s['online'] else ' (offline)') for s in self.slots.all()) or 'none'}"
                + (f"; consoles that did not start: {change.get('failed')}" if change.get("failed") else ""))
            self._event("rebuilt", forgot=probed, slots=[s["id"] for s in self.slots.all()])
            prober = Prober(self, pins=None, consoles=None, identify=identify,
                            default_sdks=[s for s in (sdks or "").split(",") if s] or None, register=True)
            result = prober.run(log)
            return {**result, "forgot": probed}

        for d in probed:
            self._busy[d] = "rebuild"
        try:
            job = self.ctx.jobs.start("probe", f"{self.ctx.host_id}: rebuild", body, host=self.ctx.host_id)
        finally:
            for d in probed:
                self._busy.pop(d, None)
        return {"job": job.id, "host": self.ctx.host_id, **plan}

    def refresh(self, dut: str, lease: str | None, name: str | None = None, rename: bool = False,
                board: str | None = None, soc: str | None = None) -> dict[str, Any]:
        """Re-read the board in a DUT's slot, as a job (see `_refresh`)."""
        if name and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise InvalidRequest(f"invalid DUT name {name!r}", hint="letters, digits, _ . - (max 63)")
        if name and name != dut and name in self.boards:
            raise Conflict(f"DUT {name} already exists on {self.ctx.host_id}")
        self.leases.check(dut, lease)
        self._guard_busy(dut)
        job = self._start_job(dut, "refresh", lambda log: self._refresh(dut, name, rename, board, soc, log))
        return {"job": job.id, "host": self.ctx.host_id, "dut": dut}

    def _usb_now(self) -> dict[str, dict[str, Any]]:
        vids = self.probe_cfg.get("usb_vids") or self.socs.usb_vids()
        res = self.hooks.run("probe", "usb-devices", {"vids": vids}, timeout=30)
        return {d["usb"]: d for d in res.data.get("devices", [])}

    def _refresh(self, dut: str, name: str | None, rename: bool, board_name: str | None, soc_given: str | None,
                 log) -> dict[str, Any]:
        import copy
        import time

        from .probe import identify_board

        b = self.board(dut)
        spec = b.spec
        taken = {x.spec.usb for i, x in self.boards.items() if i != dut}
        before = self._usb_now()
        mark = b.console.mark()
        log(f"refresh {dut}: reset into download mode")
        b.control.enter_fdl(log, b.runner(self).download_evidence(mark))
        # The board's download port: where it was, or, after re-cabling, the one port that just enumerated.
        deadline, usb, text, products = time.monotonic() + 30, None, "", ""
        while time.monotonic() < deadline:
            text = b.console.read_since(mark)
            after = self._usb_now()
            fresh = [u for u, d in after.items() if u not in taken and
                     (u not in before or before[u].get("devnum") != d.get("devnum"))]
            products = " ".join(d.get("product", "") for d in after.values())
            if spec.usb in after:
                usb = spec.usb
                break
            if len(fresh) == 1:
                usb = fresh[0]
                log(f"download port moved: {spec.usb} -> {usb}")
                break
            time.sleep(0.5)
        soc = self.socs.detect(text)[0] or soc_given or spec.soc
        fdl = self.socs.in_download_mode(text, products) or bool(usb)
        if not usb:
            log(f"no download-mode USB device appeared (was {spec.usb}): plug its download cable into this host")
        ident = identify_board(self, usb, fdl, soc, str(self.consoles.run_dir.parent / "logs" / spec.console_alias), log)
        fields = ident.get("fields") or {}
        raw = copy.deepcopy(spec.raw)
        changed = []

        def put(key, value):
            if value and raw.get(key) != value:
                raw[key] = value
                changed.append(key)
        put("board", board_name or fields.get("product"))
        put("soc", soc)
        put("description", fields.get("model"))
        if ident:
            raw["identity"] = ident
        if usb and usb != spec.usb:
            raw["fastboot"] = {**spec.fastboot, "usb": usb}
            changed.append("usb")
        new = name or (ident.get("name_hint") if rename else None) or dut
        if new != dut:
            if new in self.boards:
                new = f"{new}-{dut}"
            raw["id"] = new
            changed.append("id")
        raw["refreshed"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "identified": bool(ident)}
        renamed_console = None
        if new != dut and spec.console_alias == dut:
            raw["console"] = {**spec.console, "alias": new}
            renamed_console = (dut, new)
        self.registry.update(dut, raw)
        if new != dut:
            self._drop_aliases(dut)
            self.leases.move(dut, new)
            if renamed_console:
                self.consoles.rename(*renamed_console, spec.console.get("device"))
                self.slots.rename_alias(*renamed_console)
        self._rebuild()
        self.boards[new].control.setup()
        self._note_slot(self.boards[new].spec, "refreshed")
        self._event("refreshed", dut=new, old=dut if new != dut else None, changed=changed)
        log(f"refreshed {new}: " + (", ".join(f"{k} {raw.get(k) if k != 'usb' else usb}" for k in changed) or "nothing changed"))
        return {"dut": new, "renamed_from": dut if new != dut else None, "changed": changed,
                "board": raw.get("board"), "soc": raw.get("soc"), "usb": raw["fastboot"]["usb"],
                "identified": bool(ident), "slot": self.slot_of(new)}

    # ----------------------------------------------------------- plumbing --
    def ports(self) -> dict[str, list[str]]:
        """Serial and USB devices on this host right now (as the port watcher last saw them)."""
        return self.port_watcher.current if self.port_watcher else {}

    def _ports_changed(self, change: dict, now: dict) -> None:
        # A new adapter is a new slot with its own console; one that went away drops its slot, or leaves it offline
        # (and the card greyed) while a board is registered there.
        if any(str(x).startswith("/dev/") for x in change["added"] + change["removed"]):
            self.refresh_slots()
        self._event("ports", added=change["added"], removed=change["removed"])

    def startup(self) -> None:
        from .ports import PortWatcher

        cfg = self.config.get("watch_ports", True)
        if cfg:
            interval = cfg.get("interval_s", 2.0) if isinstance(cfg, dict) else 2.0
            self.port_watcher = PortWatcher(self._ports_changed, interval).start()
        for b in self.boards.values():
            try:
                b.control.setup()
            except BspError as e:
                self._event("error", dut=b.spec.id, error=e.to_dict())
            try:
                self.consoles.ensure(b.spec.console_alias, b.spec.console.get("device"), only_if_known=True)
            except BspError as e:
                self._event("error", dut=b.spec.id, error=e.to_dict())
        self.refresh_slots()

    def shutdown(self) -> None:
        if self.port_watcher:
            self.port_watcher.stop()

    def commands(self) -> list[Command]:
        return build_commands(self)


# =================================================================== views --
def _stage(fb: dict | None, download_rx: str | None) -> str:
    """Coarse stage from the fastboot view only: download mode / fastboot / not on the bus."""
    import re

    if not fb or fb.get("error"):
        return "off-bus"
    if download_rx and re.search(download_rx, fb.get("product", "")):
        return "download-mode"
    return "fastboot"

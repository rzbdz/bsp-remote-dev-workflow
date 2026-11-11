"""dut command set: the single definition behind REST, CLI, console and AI tools for the DUTs of a
host. Each command is one atomic step of the DUT manager (`DutModule`), which composes the host's
relay controller (control backends over gpio_relay, or a person), its download path (fastboot and
the artifact cache) and its serial consoles (serial-mux) per board."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command
from ..sdk.catalog import BOOT_METHODS
from .slots import render_slots as _render_slots

if TYPE_CHECKING:
    from .module import DutModule

DUT = Param("dut", help="DUT id (see `bsp dut list`)")
SLOT = Param("slot", help="slot (s3, or <host>/s3; see `bsp slot list`)")
LEASE = Param("lease", help="lease token from `bsp dut acquire` (the CLI fills it in / $BSP_LEASE)",
              required=False, positional=False)


def build_commands(mod: "DutModule") -> list[Command]:
    def cmd(name, summary, handler, params=(), render=None, mutating=True, description=""):
        return Command(CommandSpec(name, summary, list(params), description, mutating), handler, render)

    cmds = [
        cmd("dut.list", "DUTs on this host: stage, lease, USB path, console", mod.list,
            render=_render_list, mutating=False),
        cmd("dut.status", "One DUT: lease, stage (fdl/fastboot/os), fastboot, control lines, console tail",
            mod.status, [DUT], render=_render_status, mutating=False),
        cmd("dut.acquire", "Lease a DUT (needed for reset/boot/console); returns a token",
            mod.acquire,
            [DUT, Param("holder", help="who holds it (user or agent id)", required=False, positional=False,
                        default="anonymous"),
             Param("ttl", "integer", "lease seconds (max 86400)", required=False, default=3600),
             Param("purpose", help="free text shown to others", required=False, default=""), LEASE],
            render=lambda r: f"leased {r['dut']} on {r['host']} to {r['holder']} for {r['expires_in_s']}s\n"
                             f"token: {r['token']}",
            description="Re-acquiring with your own token renews the lease."),
        cmd("dut.release", "Release a DUT lease", mod.release,
            [DUT, LEASE, Param("force", "boolean", "operator override", required=False, default=False)],
            render=lambda r: f"{r['resource']}: {'released' if r['released'] else r.get('message')}"),
        cmd("dut.reset", "Reset the DUT via its control backend; by default waits until it is in FDL",
            mod.reset,
            [DUT, LEASE, Param("fdl", "boolean", "enter/expect FDL download mode", required=False, default=True)],
            render=lambda r: (f"{r['dut']}: waiting for a person, job {r['job']}" if r.get("job")
                              else "\n".join(r["log"]) + f"\nfastboot: {r['fastboot']}")),
        cmd("dut.confirm", "Tell a waiting reset/boot that the requested manual action is done "
            "(human control backend)", mod.confirm,
            [DUT, Param("note", help="what you did, for the log", required=False, default="")],
            render=lambda r: f"{r['dut']}: confirmed '{r['confirmed']}' after {r['waited_s']}s"),
        cmd("dut.boot", "Boot an SDK: load = fastboot RAM boot, download = flash to media",
            mod.boot,
            [DUT, Param("sdk", help="sdk instance (bsp sdk list)", positional=False),
             Param("method", help="load | download", required=False, default="load", choices=list(BOOT_METHODS)),
             Param("variant", help="e.g. uboot-only", required=False), LEASE],
            render=lambda r: f"job {r['job']}: {r['dut']} boot {r['sdk']} [{r['method']}]",
            description="Pulls the SDK's artifacts from its build server into the host cache, then runs the "
                        "recipe (enter-fdl, stage, continue, waits on serial + fastboot). Background job: the "
                        "CLI follows it and exits non-zero if it fails."),
        cmd("dut.console", "Run one shell command on the DUT's serial console, return its output",
            mod.console,
            [DUT, Param("cmd", help="command line, e.g. 'dmesg | grep foo'"),
             Param("expect", help="regex marking completion (default: the SDK shell prompt)", required=False),
             Param("timeout", "number", "seconds", required=False, default=20), LEASE],
            render=lambda r: r["output"]),
        cmd("dut.renew", "Extend your lease on a DUT (holder, purpose and token stay the same)", mod.renew,
            [DUT, LEASE, Param("ttl", "integer", "new lease seconds from now (max 86400)", required=False,
                               default=3600)],
            render=lambda r: f"renewed {r['dut']} for {r['holder']}: {r['expires_in_s']}s left"),
        cmd("dut.present", "Whether the DUT's download/fastboot port is on the USB bus", mod.present,
            [DUT], mutating=False,
            render=lambda r: f"{r['dut']}: {'present' if r['present'] else 'not on bus'} ({r['usb']})"
                             + (f" {r['fastboot']['product']}" if r.get("fastboot") else "")),
        cmd("dut.stage", "Load an uploaded file into RAM through the download port (fastboot stage + continue)",
            lambda dut, sha256, lease, timeout, **kw: mod.stage(dut, sha256, lease, kw["continue"], timeout),
            [DUT, Param("sha256", help="sha256 of a file uploaded with PUT /api/v1/artifacts/<sha256>",
                        positional=False), LEASE,
             Param("continue", "boolean", "run `fastboot continue` after the stage", required=False, default=True),
             Param("timeout", "number", "seconds for the stage", required=False, default=300)],
            render=lambda r: f"{r['dut']}: staged {r['file']} ({r['size']:,} B)"
                             + (" and continued" if r["continued"] else "")),
        cmd("dut.boot_files", "Boot from RAM with uploaded files in order (FSBL, U-Boot/OpenSBI, kernel)",
            mod.boot_files,
            [DUT, Param("files", help="comma list of sha256 of uploaded files, in boot order", positional=False),
             LEASE,
             Param("reset", "boolean", "enter download mode first", required=False, default=True),
             Param("first_wait", "number", "seconds before polling for the port after the first file",
                   required=False, default=8),
             Param("later_wait", "number", "same, after later files", required=False, default=3),
             Param("port_timeout", "number", "how long to poll for the port to come back", required=False,
                   default=20)],
            render=lambda r: f"job {r['job']}: {r['dut']} boot {len(r['files'])} file(s)",
            description="Each file: fastboot stage + continue, then wait for the download port to come back "
                        "before the next. Background job: follow it with `bsp job get`. Never flashes."),
        cmd("dut.flash", "Write a package (flash images and their layout) to the board",
            mod.flash,
            [DUT, Param("package", help="http(s) URL the host downloads, or sha256 of an uploaded package file",
                        positional=False),
             Param("sha256", help="expected sha256 of the package at the URL (checked after download)",
                   required=False),
             Param("flasher", help="flash hook to use (default: the board's, its SoC's, then spacemit-pkg)",
                   required=False),
             LEASE,
             Param("timeout", "number", "seconds the flasher may take", required=False, default=3600),
             Param("reset", "boolean", "enter download mode first", required=False, default=True)],
            render=lambda r: f"job {r['job']}: {r['dut']} flash {r['package']} with {r['flasher']}",
            description="The package is a .tar.gz, .tgz, .tar or .zip (the CLI sends a local directory as a .tar). "
                        "The host unpacks it, puts the board in download mode and runs the flash hook "
                        "`flash/<flasher>` on the unpacked directory. Background job: follow it with `bsp job get`; "
                        "its progress is a percent when the flasher reports one."),
        cmd("artifact.has", "Whether a file with this sha256 is in the host's artifact cache",
            mod.artifact_has, [Param("sha256", help="64 hex digits")], mutating=False,
            render=lambda r: f"{r['sha256'][:16]}...: " + (f"cached ({r['size']:,} B, {r['file']})"
                                                            if r["present"] else "not cached")),
        cmd("dut.log", "Recent console lines (passive: no keystrokes, no lease)", mod.log,
            [DUT, Param("lines", "integer", "how many", required=False, default=40),
             Param("grep", help="regex filter", required=False)],
            render=lambda r: "\n".join(r["lines"]), mutating=False),
        cmd("dut.logs", "Console log over a time range, optionally searched (passive: no keystrokes, no lease)", mod.logs,
            [DUT, Param("since", help="from this time, YYYY-MM-DD HH:MM:SS (default: the oldest kept)", required=False),
             Param("until", help="up to this time (default: now)", required=False),
             Param("grep", help="keep lines containing this text (case-blind)", required=False),
             Param("regex", "boolean", "grep is a regex", required=False, default=False),
             Param("limit", "integer", "at most this many lines", required=False, default=20000),
             Param("first", "boolean", "keep the first lines rather than the last when capped", required=False, default=False)],
            render=lambda r: "\n".join(r["lines"]) + (f"\n({r['matched']} matched, showing {len(r['lines'])})" if r["truncated"] else ""),
            mutating=False),
        cmd("dut.add", "Register a DUT on this host from a JSON spec (what probe writes)", mod.add,
            [Param("spec", "string", "JSON object: id, board, console{alias}, fastboot{usb}, control{...}, sdks"),
             Param("replace", "boolean", "overwrite an existing id", required=False, default=False)],
            render=_render_status),
        cmd("dut.remove", "Forget a probed/added DUT", mod.remove,
            [DUT, Param("force", "boolean", "even if leased", required=False, default=False)]),
        cmd("dut.rename", "Rename a DUT (aliases follow; console alias too if it was the DUT name)",
            mod.rename,
            [DUT, Param("new", help="new DUT name"), LEASE,
             Param("console", "boolean", "also rename the serial-mux console alias", required=False, default=False)],
            render=_render_status),
        cmd("dut.set", "Edit a DUT: board, soc, description, sdks, usb, reset pin, FDL strategy",
            lambda dut, lease, **f: mod.set_fields(dut, lease, **f),
            [DUT, LEASE,
             Param("board", help="board name", required=False, positional=False),
             Param("soc", help="SoC id (see the SoC profiles)", required=False, positional=False),
             Param("description", help="free text", required=False, positional=False),
             Param("sdks", help="comma list; empty string = all SDKs of its SoC", required=False, positional=False),
             Param("usb", help="fastboot selector, e.g. usb:2-1.4", required=False, positional=False),
             Param("reset_pin", help="header pin of the reset line, e.g. P26", required=False, positional=False),
             Param("pulse_ms", "integer", "reset pulse width", required=False, positional=False),
             Param("fdl", help="strapped | button | unset", required=False, positional=False, choices=["strapped", "button", "unset"]),
             Param("fdl_pin", help="header pin of the FDL line (fdl=button)", required=False, positional=False)],
            render=_render_status),
        cmd("dut.logins", "ssh console logins on this host: `ssh <login>@<host>` opens that DUT's console",
            mod.login_list, mutating=False,
            render=lambda r: ("\n".join(f"{x['login']:<24} -> {x['dut']}" for x in r["logins"])
                              or "no console logins (modules.dut.ssh_console.enabled is off)")
            + (f"\nlast sync error: {r['error']}" if r.get("error") else "")),
        cmd("dut.export", "All DUT specs on this host (save into <site>/duts/ to make them static)",
            mod.export, mutating=False,
            render=lambda r: __import__("yaml").safe_dump(r, sort_keys=False, allow_unicode=True)),
        cmd("dut.probe", "Discover boards on this host: pulse free GPIOs, watch consoles + USB, identify",
            mod.probe,
            [Param("pins", help="comma list of candidate reset pins (default: all free header GPIOs)", required=False),
             Param("consoles", help="comma list of slots (s3) or consoles (serial-mux alias, /dev/tty*) to probe", required=False),
             Param("identify", "boolean", "boot U-Boot to read board identity (TLV)", required=False, default=True),
             Param("sdks", help="comma list of sdks the found boards run (default by soc)", required=False),
             Param("dry_run", "boolean", "only show candidates, touch nothing", required=False, default=False),
             Param("human", "boolean", "no GPIO: a person resets each unregistered board into download mode",
                   required=False, default=False),
             Param("rounds", "integer", "human: at most this many boards", required=False, default=8),
             Param("wait_s", "number", "human: seconds to wait for each board", required=False, default=120),
             Param("register_found", "boolean", "register each board the moment it is found", required=False,
                   default=False),
             Param("known", "boolean", "only try reset lines slots had before (fast recovery after re-cabling)",
                   required=False, default=False)],
            render=_render_probe,
            description="Hook-driven (hooks/probe/*). Results are proposals: confirm with `bsp dut add`, "
                        "or pass register=true to register each board as it is found."),
        cmd("dut.refresh", "Re-read the board in a DUT's slot (after swapping boards): reset into download mode, "
                           "identify, update board/soc/model and, if it moved, the download port",
            mod.refresh,
            [DUT, LEASE,
             Param("name", help="rename the DUT to this", required=False),
             Param("rename", "boolean", "rename the DUT after what identify read", required=False, default=False),
             Param("board", help="board name to record when identify cannot read it", required=False),
             Param("soc", help="SoC to record when the boot ROM does not tell", required=False)],
            render=lambda r: f"refresh job {r['job']} on {r['host']}" if "job" in r else str(r)),
        # ---- slots: every console on the host is a slot; reset, download port and DUT are attached to it
        cmd("slot.list", "Slots on this host: each console, whether its adapter is plugged in, and what is attached",
            mod.slot_list, render=_render_slots, mutating=False),
        cmd("slot.status", "One slot: console, reset line, download port, the DUT in it, history",
            mod.slot_status, [SLOT], render=lambda s: _render_slots([s]), mutating=False),
        cmd("slot.scan", "Match the host's consoles to slots now (new adapters get a slot and a console)",
            lambda: {**mod.refresh_slots(), "slots": mod.slot_list()}, render=lambda r: _render_slots(r["slots"])),
        cmd("slot.forget", "Forget a slot whose adapter is gone (stops its console daemon)",
            mod.slot_forget, [SLOT]),
        cmd("dut.rebuild", "After re-cabling the host: forget its probed DUTs and slots, then rediscover every board "
                           "(reset lines used before first, old names kept); keep_used only redoes what moved",
            mod.rebuild,
            [Param("identify", "boolean", "boot U-Boot to read each board's identity", required=False, default=True),
             Param("sdks", help="comma list of sdks the found boards run (default by soc)", required=False),
             Param("dry_run", "boolean", "only say what would be forgotten", required=False, default=False),
             Param("keep_used", "boolean", "keep leased or busy DUTs and slots whose adapter is still here; forget "
                   "the DUTs whose console is gone and probe only free consoles", required=False, default=False)],
            render=lambda r: (f"{'rediscover' if r.get('mode') == 'keep-used' else 'rebuild'} job {r['job']} on {r['host']}: "
                              if "job" in r else f"{r['host']} would ")
            + f"forget {', '.join(r['forget_duts']) or 'no DUTs'}"
            + (f", keep {', '.join(r['keep_duts'])} (site catalog)" if r["keep_duts"] else "")),
        cmd("site.reload", "Read the site again (SDK catalog, SoC profiles, hooks) after it changed on disk",
            mod.reload_site,
            render=lambda r: f"{r['site']}: {len(r['sdks'])} sdks ({', '.join(r['sdks']) or 'none'}), "
                             f"socs {', '.join(r['socs']) or 'none'}",
            description="For tools that ship the site to this host: boards, leases and jobs are untouched. A catalog "
                        "that does not load is reported and the running one is kept."),
    ]
    # Any DUT can be named by its slot: `s3`, or `<host>/s3`.
    for c in cmds:
        if c.spec.name != "dut.add" and any(p.name == "dut" for p in c.spec.params):
            c.handler = _resolving(mod, c.handler)
    return cmds


def _resolving(mod: "DutModule", handler):
    def run(**kw: Any) -> Any:
        return handler(**{**kw, "dut": mod.resolve(kw["dut"])})
    return run


# ================================================================= renders --
def _render_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(no DUTs on this host — run `bsp probe`)"
    rows = [("DUT", "HOST", "BOARD", "STAGE", "LEASE", "USB", "CONSOLE")]
    for d in items:
        lease = d["lease"]
        rows.append((d["id"], d["host"], d["board"], d["stage"] + (f" [{d['busy']}]" if d.get("busy") else ""),
                     f"{lease['holder']} ({lease['expires_in_s']}s)" if lease else "free", d["usb"], d["console"]))
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def _render_status(d: dict[str, Any]) -> str:
    lease = d["lease"]
    return "\n".join([
        f"{d['id']} @ {d['host']}: {d['board']} ({d['soc']}) — {d['description']} [{d.get('origin')}]",
        f"  stage    : {d['stage']}" + (f"  busy: {d['busy']}" if d.get("busy") else ""),
        (f"  lease    : {lease['holder']} ({lease['expires_in_s']}s left, {lease.get('purpose') or '-'})"
         if lease else "  lease    : free"),
        f"  fastboot : {d['fastboot'] or 'not on bus'} (usb {d['usb']})",
        f"  control  : {d['control_state']}",
        f"  sdks     : {', '.join(d['sdks'])}",
        "  console  :", *[f"    {l}" for l in d["console_tail"]],
    ])


def _render_probe(r: dict[str, Any]) -> str:
    if "job" in r:
        return f"probe job {r['job']} on {r['host']}"
    import json

    return json.dumps(r, indent=2, ensure_ascii=False)


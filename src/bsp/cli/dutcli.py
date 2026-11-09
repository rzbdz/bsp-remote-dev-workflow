"""`bsp dut ...`, `bsp probe`, `bsp hosts`, `bsp init` — fleet-aware board commands."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

from ..core.errors import InvalidRequest, BspError
from ..dut.slots import render_slots
from .fleet import Fleet, HostHandle, LeaseBook, config_dir
from .remotes import RemoteError


class CallError(BspError):
    def __init__(self, err: dict[str, Any], host: str):
        super().__init__(err.get("message", "error"), hint=err.get("hint"), details=err.get("details"))
        self.code = err.get("code", "error")
        self.host = host


def _ok(h: HostHandle, r: dict[str, Any]) -> Any:
    if not r.get("ok"):
        raise CallError(r.get("error") or {}, h.id)
    return r["result"]


def _lease(args, book: LeaseBook, dut: str) -> str | None:
    return args.lease or os.environ.get("BSP_LEASE") or book.get(f"dut:{dut}")


# -------------------------------------------------------------- hosts --
def hosts(fleet: Fleet, _args) -> tuple[Any, str]:
    hs = fleet.hosts()
    rows = [("HOST", "VIA", "MODULES", "DUTS")]
    out = []
    for h in hs:
        mods = h.status.get("modules", {})
        duts = (mods.get("dut") or {}).get("duts", [])
        rows.append((h.id, h.transport.label, ",".join(mods), ",".join(duts) or "-"))
        out.append({"host": h.id, "via": h.transport.label, "modules": list(mods), "duts": duts})
    for name, err in fleet.errors.items():
        rows.append((name, "UNREACHABLE", "", err[:60]))
        out.append({"host": name, "error": err})
    return out, _table(rows) + f"\n(mode: {fleet.mode})"


def init(fleet: Fleet, args) -> tuple[Any, str]:
    hs = fleet.hosts()
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    lines = [f"config dir: {cfg}", f"mode: {fleet.mode} ({'this machine is a board host' if fleet.mode == 'local' else 'talking to remote hosts'})"]
    for h in hs:
        lines.append(f"  host {h.id:<16} via {h.transport.label}  duts: {', '.join((h.status['modules'].get('dut') or {}).get('duts', [])) or '-'}")
    for n, e in fleet.errors.items():
        lines.append(f"  host {n:<16} UNREACHABLE ({e[:80]})")
    lines.append("next: `bsp probe` to discover boards, `bsp dut list`, `bsp sdk list`")
    return {"mode": fleet.mode, "hosts": [h.id for h in hs], "unreachable": fleet.errors}, "\n".join(lines)


# ----------------------------------------------------------------- dut --
def dut_list(fleet: Fleet, args) -> tuple[Any, str]:
    items = []
    for h, r in fleet.fan("dut_list"):
        if r.get("ok"):
            items += r["result"]
        elif "unknown command" in r["error"]["message"]:
            fleet.errors[h.id] = "dut module not enabled on this host"
        else:
            fleet.errors[h.id] = r["error"]["message"]
    if getattr(args, "soc", None):
        items = [d for d in items if d.get("soc") == args.soc]
    if getattr(args, "sdk", None):
        items = [d for d in items if args.sdk in d.get("sdks", [])]
    if getattr(args, "free", False):
        items = [d for d in items if not d["lease"]]
    rows = [("DUT", "SLOT", "HOST", "SOC", "BOARD", "STAGE", "LEASE", "USB", "CONSOLE", "SDKS")]
    for d in items:
        l = d["lease"]
        rows.append((d["id"], d.get("slot") or "-", d["host"], d.get("soc") or "-", d["board"],
                     d["stage"] + (f" [{d['busy']}]" if d.get("busy") else ""),
                     f"{l['holder']} ({l['expires_in_s']}s)" if l else "free", d["usb"], d["console"],
                     ",".join(d["sdks"])))
    text = _table(rows) if items else "(no DUTs — run `bsp probe`)"
    if fleet.errors:
        text += "\nunreachable: " + ", ".join(f"{k} ({v[:50]})" for k, v in fleet.errors.items())
    return items, text


def _resolve(fleet: Fleet, ref: str, c: str) -> tuple[HostHandle, str, dict[str, Any] | None]:
    """A DUT by name or slot: its host, its DUT name, and the slot when it was named by one."""
    h = fleet.find_dut(ref)
    if not re.fullmatch(r"(?:[^/]+/)?s\d+", ref):
        return h, ref, None
    ids = {d["id"] for d in _ok(h, h.call("dut_list"))}
    if ref in ids:
        return h, ref, None
    st = _ok(h, h.call("slot_status", {"slot": ref.rsplit("/", 1)[-1]}))
    if st.get("dut"):
        return h, st["dut"], st
    if c in ("pipe", "console", "status", "log"):
        return h, ref, st
    raise InvalidRequest(f"slot {st['ref']} has no board registered",
                         hint=f"probe it (`bsp probe -r {h.id} --slots {st['id']} --register`), "
                              f"or open its console (`bsp slot console {st['ref']}`)")


def dut_cmd(fleet: Fleet, args, book: LeaseBook) -> tuple[Any, str]:
    c = args.dut_cmd
    h, dut, slot = _resolve(fleet, args.dut, c)
    if slot and not slot.get("dut"):        # an empty slot: only its console
        return slot_cmd_on(h, slot, "pipe" if c == "pipe" else "console" if c == "console" else "status", args)
    if c == "status":
        r = _ok(h, h.call("dut_status", {"dut": dut}))
        return r, _status_text(r)
    if c == "acquire":
        r = _ok(h, h.call("dut_acquire", {"dut": dut, "holder": args.holder, "ttl": args.ttl,
                                           "purpose": args.purpose or "", "lease": _lease(args, book, dut)}))
        book.put(f"dut:{dut}", r["token"], r["expires_in_s"], host=h.id, holder=r["holder"])
        return r, f"leased {dut} on {h.id} to {r['holder']} for {r['expires_in_s']}s\ntoken: {r['token']}"
    if c == "release":
        r = _ok(h, h.call("dut_release", {"dut": dut, "lease": _lease(args, book, dut), "force": args.force}))
        book.drop(f"dut:{dut}")
        return r, f"{dut}: {'released' if r['released'] else r.get('message')}"
    if c == "reset":
        r = _ok(h, h.call("dut_reset", {"dut": dut, "lease": _lease(args, book, dut), "fdl": not args.no_fdl},
                          timeout=120))
        if r.get("job"):                 # a person has to act, or the board boots its media: follow the job
            out = sys.stderr if args.json else sys.stdout
            if r.get("media_boot") and not r.get("needs_human"):
                print(f"{dut} @ {h.id}: booting its media ({r['media_boot']}), job {r['job']}", file=out, flush=True)
            else:
                print(f"{dut} @ {h.id}: reset needs a person, job {r['job']} "
                      f"(confirm with `bsp dut confirm {dut}`)", file=out, flush=True)
            if args.no_wait:
                return r, f"follow with: bsp job get {r['job']} -r {h.id} --wait 30"
            return follow_job(h, r["job"], out, args.timeout)
        return r, "\n".join(r["log"]) + f"\nfastboot: {r['fastboot']}"
    if c == "renew":
        r = _ok(h, h.call("dut_renew", {"dut": dut, "lease": _lease(args, book, dut), "ttl": args.ttl}))
        book.put(f"dut:{dut}", r["token"], r["expires_in_s"], host=h.id, holder=r["holder"])
        return r, f"renewed {dut} on {h.id} for {r['holder']}: {r['expires_in_s']}s left"
    if c == "present":
        r = _ok(h, h.call("dut_present", {"dut": dut}))
        return r, f"{dut}: " + (f"download port present ({r['fastboot'].get('product')})" if r["present"]
                                else f"not on the bus ({r['usb']})")
    if c == "stage":
        art = upload(h, args.file, out=sys.stderr if args.json else sys.stdout)
        r = _ok(h, h.call("dut_stage", {"dut": dut, "sha256": art["sha256"], "lease": _lease(args, book, dut),
                                         "continue": not args.no_continue, "timeout": args.timeout},
                          timeout=args.timeout + 60))
        return {**r, "path": os.path.abspath(args.file)}, \
            f"{dut}: staged {art['name']} ({art['size']:,} B, sha256 {art['sha256'][:16]}...)" \
            + (" and continued" if r["continued"] else "")
    if c == "pipe":
        return _pipe(h, dut)
    if c == "refresh":
        out = sys.stderr if args.json else sys.stdout
        r = _ok(h, h.call("dut_refresh", {"dut": dut, "lease": _lease(args, book, dut), "name": args.name,
                                           "rename": args.rename, "board": args.board, "soc": args.soc}))
        print(f"{dut} @ {h.id}: refresh job {r['job']}", file=out, flush=True)
        v, text = follow_job(h, r["job"], out, args.timeout)
        new = (v.get("result") or {}).get("dut") if isinstance(v, dict) else None
        if new and new != dut:
            entry = book.get(f"dut:{dut}")
            if entry:
                book.put(f"dut:{new}", entry, 3600, host=h.id)
        return v, text
    if c == "confirm":
        r = _ok(h, h.call("dut_confirm", {"dut": dut, "note": " ".join(args.note or [])}))
        return r, f"{dut}: confirmed '{r['confirmed']}' after {r['waited_s']}s; the waiting job continues"
    if c == "boot":
        out = sys.stderr if args.json else sys.stdout
        if bool(args.sdk) == bool(args.files):
            raise InvalidRequest("give either --sdk SDK or --files F1 [F2 ...]",
                                 hint="--sdk runs the SDK's boot recipe; --files boots these local files in order")
        if args.files:
            arts = [upload(h, f, out) for f in args.files]
            r = _ok(h, h.call("dut_boot_files", {
                "dut": dut, "files": ",".join(a["sha256"] for a in arts), "lease": _lease(args, book, dut),
                "reset": not args.no_reset, "first_wait": args.first_wait, "later_wait": args.later_wait,
                "port_timeout": args.port_timeout}))
            print(f"{dut} @ {h.id}: boot {len(arts)} file(s) job {r['job']}", file=out, flush=True)
        else:
            r = _ok(h, h.call("dut_boot", {"dut": dut, "sdk": args.sdk, "method": args.method,
                                            "variant": args.variant, "lease": _lease(args, book, dut)}))
            print(f"{dut} @ {h.id}: boot {args.sdk} [{args.method}] job {r['job']}", file=out, flush=True)
        if args.no_wait:
            return r, f"follow with: bsp job get {r['job']} -r {h.id} --wait 30"
        return follow_job(h, r["job"], out, args.timeout)
    if c == "console" and not args.command:
        return _attach(h, dut, args)
    if c == "console":
        cmd = " ".join(args.command)
        r = _ok(h, h.call("dut_console", {"dut": dut, "cmd": cmd, "expect": args.expect, "timeout": args.timeout,
                                           "lease": _lease(args, book, dut)}, timeout=args.timeout + 30))
        return r, r["output"]
    if c == "log":
        r = _ok(h, h.call("dut_log", {"dut": dut, "lines": args.lines, "grep": args.grep}))
        return r, "\n".join(r["lines"])
    if c == "remove":
        r = _ok(h, h.call("dut_remove", {"dut": dut, "force": args.force}))
        return r, f"removed {dut} from {h.id}"
    if c == "rename":
        r = _ok(h, h.call("dut_rename", {"dut": dut, "new": args.new, "console": args.console,
                                          "lease": _lease(args, book, dut)}, timeout=120))
        book.drop(f"dut:{dut}")
        return r, f"renamed {dut} -> {args.new} on {h.id}" + (f" ({r['note']})" if r.get("note") else "") \
            + "\n" + _status_text(r)
    if c == "set":
        fields = {k: getattr(args, k) for k in ("board", "soc", "description", "sdks", "usb", "reset_pin",
                                                 "pulse_ms", "fdl", "fdl_pin")}
        r = _ok(h, h.call("dut_set", {"dut": dut, "lease": _lease(args, book, dut), **fields}, timeout=60))
        return r, f"updated {dut}: {', '.join(r['changed'])}\n" + _status_text(r)
    raise InvalidRequest(f"unknown dut action {c}")


def follow_job(h: HostHandle, job: str, out, timeout: float) -> tuple[Any, str]:
    since, t0 = 0, time.time()
    while True:
        v = _ok(h, h.call("job_get", {"id": job, "since": since, "wait": 5}, timeout=60))
        for line in v["lines"]:
            print(f"  {line}", file=out, flush=True)
        since = v["next"]
        if v["status"] != "running":
            break
        if time.time() - t0 > timeout:
            raise BspError(f"job {job} still running after {timeout:g}s",
                           hint=f"keep following: bsp job get {job} -r {h.id} --wait 60")
    if v["status"] == "failed":
        e = v.get("error") or {}
        raise CallError({**e, "message": f"job {job} failed: {e.get('message')}"}, h.id)
    return v, f"job {job} succeeded in {v['elapsed_s']}s"


def _sha256(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upload(h: HostHandle, path: str, out=None) -> dict[str, Any]:
    """Make sure the host has this file in its artifact cache (by sha256); send it only if missing."""
    from urllib.parse import quote

    if not os.path.isfile(path):
        raise InvalidRequest(f"no such file: {path}")
    size = os.path.getsize(path)
    if not size:
        raise InvalidRequest(f"file is empty: {path}")
    sha, name = _sha256(path), os.path.basename(path)
    have = _ok(h, h.get(f"/api/v1/artifacts/{sha}"))
    if not have.get("present"):
        t0 = time.time()
        _ok(h, h.transport.upload(f"/api/v1/artifacts/{sha}?name={quote(name)}", path,
                                  timeout=max(120.0, size / (1 << 20) * 3)))
        if out:
            print(f"  sent {name} to {h.id} ({size:,} B in {time.time() - t0:.1f}s)", file=out, flush=True)
    elif out:
        print(f"  {name} already on {h.id} (sha256 {sha[:12]})", file=out, flush=True)
    return {"sha256": sha, "name": name, "size": size, "path": os.path.abspath(path)}


class Quiet:
    """A result that prints nothing (the command wrote its own output); `rc` is the exit code."""

    def __init__(self, rc: int):
        self.rc = rc


def _pipe(h: HostHandle, dut: str) -> tuple[Any, str]:
    """Raw serial-mux protocol on stdin/stdout, for programs (boardlab's console view uses it)."""
    import subprocess

    from . import pipe
    from .transport import HttpTransport, _ssh_base

    st = _ok(h, h.call("dut_status", {"dut": dut}))
    return _pipe_to(h, st, dut)


def _pipe_to(h: HostHandle, st: dict[str, Any], what: str) -> tuple[Any, str]:
    import subprocess

    from . import pipe
    from .transport import HttpTransport, _ssh_base

    sock = st.get("console_socket")
    if not sock:
        raise InvalidRequest(f"{h.id} does not report console sockets", hint="update bsp on that host")
    if not st.get("console_up"):
        raise BspError(f"console {st.get('console') or st.get('alias')} of {what} is not running on {h.id}",
                       hint=f"see `bsp slot list -r {h.id}`: its adapter may be unplugged")
    loopback = isinstance(h.transport, HttpTransport) and \
        re.match(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:|/|$)", h.transport.url)
    if loopback and os.path.exists(sock):
        return Quiet(pipe.local(sock)), None
    target = getattr(h.transport, "target", None) or h.remote.ssh
    if not target:
        raise InvalidRequest(f"no ssh route to {h.id} for a console pipe",
                             hint=f"add one: bsp remote add {h.remote.name} {h.remote.url} --ssh root@<host>")
    sys.stdout.flush()
    return Quiet(subprocess.call(_ssh_base(target) + [pipe.remote_command(sock)])), None


def _attach(h: HostHandle, dut: str, args) -> tuple[Any, str]:
    """Interactive console for humans: smtty on the host over ssh (Ctrl-A Ctrl-X detaches)."""
    import shutil
    import subprocess

    st = _ok(h, h.call("dut_status", {"dut": dut}))
    return _attach_to(h, st, dut)


def _attach_to(h: HostHandle, st: dict[str, Any], dut: str) -> tuple[Any, str]:
    import shutil
    import subprocess

    target = getattr(h.transport, "target", None) or h.remote.ssh
    if not target:
        raise InvalidRequest(f"no ssh route to {h.id} for an interactive console",
                             hint=f"add one: bsp remote add {h.remote.name} {h.remote.url} --ssh root@<host>; "
                                  f"or run one command: bsp dut console {dut} 'uname -a'")
    if not sys.stdin.isatty():
        raise InvalidRequest("interactive console needs a terminal", hint=f"run one command: bsp dut console {dut} 'CMD'")
    ssh = shutil.which("ssh") or "ssh"
    con = st.get("console") or st.get("alias")
    print(f"attaching to {dut} console ({con} on {h.id}); Ctrl-A Ctrl-X detaches", file=sys.stderr)
    cmd = st.get("console_attach") or f"smtty {con}"      # host-reported smtty path
    rc = subprocess.call([ssh, "-t", target, cmd])
    return {"rc": rc}, ""


def export(fleet: Fleet, args) -> tuple[Any, str]:
    """Probed/bound boards in the site's duts/ format (one mapping per board)."""
    import yaml
    from pathlib import Path

    specs = []
    for h, r in fleet.fan("dut_export"):
        if r.get("ok") and (not getattr(args, "host", None) or h.id == args.host):
            specs += r["result"]["duts"]
    if getattr(args, "dir", None):
        d = Path(args.dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        files = []
        for spec in specs:
            p = d / f"{spec['id']}.yaml"
            p.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
            files.append(str(p))
        return {"files": files}, "\n".join(f"wrote {f}" for f in files) or "no probed or bound boards to export"
    text = "".join("---\n" + yaml.safe_dump(s, sort_keys=False, allow_unicode=True) for s in specs)
    return specs, text or "# no probed or bound boards"

def logins(fleet: Fleet, args) -> tuple[Any, str]:
    """`ssh <login>@<host>` lines for every DUT console login, across hosts."""
    rows, off = [], []
    for h, r in fleet.fan("dut_logins"):
        if not r.get("ok"):
            continue
        res = r["result"]
        addr = (h.remote.ssh or "").split("@")[-1] or h.remote.url.split("//")[-1].split(":")[0]
        if not res.get("enabled"):
            off.append(h.id)
        for x in res.get("logins", []):
            rows.append({"host": h.id, "dut": x["dut"], "ssh": f"ssh {x['login']}@{addr}"})
    lines = [f"{x['dut']:<20} {x['ssh']}" for x in rows]
    if off:
        lines.append(f"(console logins are off on: {', '.join(off)}; enable modules.dut.ssh_console in the host config)")
    return rows, "\n".join(lines) or "no console logins"


def dut_bind(fleet: Fleet, args) -> tuple[Any, str]:
    """A DUT made of a console and a USB download port; a person does the resets."""
    hs = fleet.require()
    if args.host:
        hs = [h for h in hs if h.id == args.host]
        if not hs:
            raise RemoteError(f"host {args.host!r} not reachable", hint=f"reachable: {', '.join(h.id for h in fleet.hosts())}")
    elif len(hs) > 1:
        raise InvalidRequest("several board hosts are reachable", hint=f"pass --host ({', '.join(h.id for h in hs)})")
    h = hs[0]
    console = {"alias": args.dut, "device": args.console} if args.console.startswith("/dev/") else {"alias": args.console}
    spec = {"id": args.dut, "host": h.id, "board": args.board, "soc": args.soc, "description": args.description
            or "bound slot (console + USB, manual reset)", "console": console, "fastboot": {"usb": args.usb},
            "control": {"backend": "human", "fdl": {"strategy": args.fdl}},
            "sdks": [s for s in args.sdks.split(",") if s]}
    r = _ok(h, h.call("dut_add", {"spec": json.dumps(spec), "replace": args.replace}, timeout=120))
    return r, (f"bound {args.dut} on {h.id}: console {args.console}, {args.usb}, resets by a person\n"
               f"reset/boot it in the background; when asked, reset the board and run `bsp dut confirm {args.dut}`\n"
               + _status_text(r))


def dut_add(fleet: Fleet, args) -> tuple[Any, str]:
    spec = args.spec
    if spec.startswith("@"):
        spec = open(spec[1:]).read()
    hs = [h for h in fleet.require() if h.id == args.host]
    if not hs:
        raise RemoteError(f"host {args.host!r} not reachable", hint=f"reachable: {', '.join(h.id for h in fleet.hosts())}")
    r = _ok(hs[0], hs[0].call("dut_add", {"spec": spec, "replace": args.replace}))
    return r, f"added {r['id']} on {args.host}\n" + _status_text(r)


# --------------------------------------------------------------- probe --
def probe(fleet: Fleet, args) -> tuple[Any, str]:
    if getattr(args, "rebuild", False):
        return rebuild(fleet, args)
    consoles = ",".join(x for x in (args.consoles, args.slots) if x) or None
    params = {"pins": args.pins, "consoles": consoles, "identify": not args.no_identify,
              "sdks": args.sdks, "dry_run": args.dry_run, "human": args.human, "rounds": args.rounds,
              "wait_s": args.wait, "register_found": bool(args.register or (args.yes and not args.dry_run)),
              "known": args.known}
    if args.human and len(fleet.require()) > 1:
        raise InvalidRequest("a human probe asks a person to reset boards on one host at a time",
                             hint=f"pick the host with -r ({', '.join(h.id for h in fleet.hosts())})")
    out = sys.stderr if args.json else sys.stdout
    jobs = []
    for h, r in fleet.fan("dut_probe", params):
        if not r.get("ok"):
            print(f"[{h.id}] probe not started: {r['error']['message']}", file=out)
            continue
        if args.dry_run:
            jobs.append((h, r["result"]))
            continue
        print(f"[{h.id}] probing (job {r['result']['job']}) ...", file=out, flush=True)
        jobs.append((h, r["result"]["job"]))
    if args.dry_run:
        return {h.id: res for h, res in jobs}, json.dumps({h.id: res for h, res in jobs}, indent=2, ensure_ascii=False)
    results: dict[str, Any] = {}
    for h, job in jobs:
        try:
            v, _ = follow_job(h, job, _Prefixed(out, f"[{h.id}]"),
                              max(900.0, args.rounds * args.wait + 300) if args.human else 900)
            results[h.id] = v["result"]
        except BspError as e:
            print(f"[{h.id}] probe failed: {e.message}", file=out)
    taken = {d["id"] for _, r in fleet.fan("dut_list") if r.get("ok") for d in r["result"]}
    interactive = sys.stdin.isatty() and not args.yes and not args.json
    added, lines = [], []
    for host, res in results.items():
        for entry in res.get("registered", []):
            added.append({"host": host, "dut": entry["dut"], "slot": entry.get("slot")})
            taken.add(entry["dut"])
            lines.append(f"[{host}] registered {entry['dut']}" + (f" in slot {host}/{entry['slot']}" if entry.get("slot") else ""))
        for reason in res.get("reasons", []):
            lines.append(f"[{host}] {reason}")
        for p in res.get("proposals", []):
            if p.get("registered"):
                continue
            spec = p["spec"]
            spec["id"] = _unique(spec["id"], taken)
            reset = (spec["control"].get("reset") or {}).get("pin") or "by a person"
            print(f"\n[{host}] found {spec['board']} ({spec['description']})\n"
                  f"    reset {reset}  console {spec['console']['alias']}  "
                  f"usb {spec['fastboot']['usb']}  fdl {spec['control']['fdl']['strategy']}", file=out)
            for w in p.get("warnings", []):
                print(f"    warning: {w}", file=out)
            if interactive:
                ans = input(f"    name [{spec['id']}] (or 'skip'): ").strip()
                if ans == "skip":
                    continue
                if ans:
                    spec["id"] = ans
            elif not args.yes:
                lines.append(f"[{host}] proposal {spec['id']} not registered (pass --yes, or run interactively)")
                continue
            taken.add(spec["id"])
            h = next(x for x in fleet.hosts() if x.id == host)
            r = h.call("dut_add", {"spec": json.dumps(spec), "replace": args.replace})
            if r.get("ok"):
                added.append({"host": host, "dut": spec["id"]})
                lines.append(f"[{host}] registered {spec['id']}")
            else:
                lines.append(f"[{host}] {spec['id']} not registered: {r['error']['message']}"
                             + (f" ({r['error'].get('hint')})" if r['error'].get('hint') else ""))
    if not results:
        lines.append("no host completed a probe")
    return {"results": results, "added": added}, "\n".join(lines) or "nothing found"


def rebuild(fleet: Fleet, args) -> tuple[Any, str]:
    """`bsp probe --rebuild -r HOST --yes`: start the host's wiring over and rediscover its boards."""
    hosts = fleet.require()
    if len(hosts) != 1:
        raise InvalidRequest("a rebuild starts one host over at a time", hint=f"pick it with -r ({', '.join(h.id for h in hosts)})")
    h = hosts[0]
    params = {"identify": not args.no_identify, "sdks": args.sdks, "dry_run": args.dry_run or not args.yes}
    r = _ok(h, h.call("dut_rebuild", params))
    if "job" not in r:
        return r, (f"{h.id}: a rebuild would forget {', '.join(r['forget_duts']) or 'no DUTs'} and every slot, then "
                   f"rediscover the boards. Run again with --yes to do it.")
    out = sys.stderr if args.json else sys.stdout
    print(f"[{h.id}] rebuilding (job {r['job']}) ...", file=out, flush=True)
    v, _ = follow_job(h, r["job"], _Prefixed(out, f"[{h.id}]"), 1800)
    res = v["result"]
    lines = [f"[{h.id}] forgot {', '.join(res.get('forgot') or []) or 'no DUTs'}"]
    lines += [f"[{h.id}] registered {e['dut']}" + (f" in slot {h.id}/{e['slot']}" if e.get("slot") else "")
              for e in res.get("registered", [])]
    lines += [f"[{h.id}] {reason}" for reason in res.get("reasons", [])]
    return {"results": {h.id: res}, "added": [{"host": h.id, **e} for e in res.get("registered", [])]}, "\n".join(lines)


class _Prefixed:
    def __init__(self, out, prefix):
        self.out, self.prefix = out, prefix

    def write(self, s):
        if s.strip():
            self.out.write(f"{self.prefix}{s if s.startswith(' ') else ' ' + s}")
        elif s:
            self.out.write(s)

    def flush(self):
        self.out.flush()


def _unique(name: str, taken: set) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


# --------------------------------------------------------------- views --
def _status_text(d: dict[str, Any]) -> str:
    l = d.get("lease")
    return "\n".join([
        f"{d['id']} @ {d['host']}: {d['board']} ({d['soc']}) — {d['description']}",
        f"  stage    : {d['stage']}" + (f"  busy: {d['busy']}" if d.get("busy") else ""),
        *([f"  ACTION NEEDED: {d['needs_human']['message']} "
           f"(waiting {d['needs_human']['waiting_s']}s; then `bsp dut confirm {d['id']}`)"] if d.get("needs_human") else []),
        f"  lease    : {l['holder']} ({l['expires_in_s']}s left)" if l else "  lease    : free",
        f"  fastboot : {d['fastboot'] or 'not on bus'} (usb {d['usb']})",
        f"  control  : {d['control_state']}",
        f"  sdks     : {', '.join(d['sdks'])}",
        "  console  :", *[f"    {x}" for x in d.get("console_tail", [])],
    ])


def _table(rows) -> str:
    w = [max(len(str(r[c])) for r in rows) for c in range(len(rows[0]))]
    return "\n".join("  ".join(str(x).ljust(w[c]) for c, x in enumerate(r)).rstrip() for r in rows)


def add_parsers(sub) -> None:
    sub.add_parser("hosts", help="reachable bsp hosts, how they are reached, their DUTs")
    sub.add_parser("init", help="first-run check: mode (local/remote), reachable hosts, next steps")
    p = sub.add_parser("probe", help="discover boards on the host(s): reset lines, consoles, USB, identity")
    p.add_argument("--pins", help="candidate reset pins, e.g. P33,P11")
    p.add_argument("--consoles", help="candidate consoles (serial-mux alias or /dev/tty*)")
    p.add_argument("--sdks", help="sdks the found boards run (default per SoC)")
    p.add_argument("--no-identify", action="store_true", help="skip booting U-Boot to read the board identity")
    p.add_argument("--dry-run", action="store_true", help="only list candidates")
    p.add_argument("--yes", action="store_true", help="register proposals without asking (agents); implies --register")
    p.add_argument("--register", action="store_true", help="register each board on its host the moment it is found")
    p.add_argument("--slots", help="only these slots, e.g. s3,s5 (see `bsp slot list`)")
    p.add_argument("--known", action="store_true",
                   help="only try reset lines slots had before (quick recovery after re-cabling)")
    p.add_argument("--replace", action="store_true", help="overwrite DUTs with the same id")
    p.add_argument("--human", action="store_true",
                   help="boards without reset wiring: a person resets each unregistered board into download mode "
                        "and bsp pairs the console that speaks with the USB port that appears")
    p.add_argument("--rebuild", action="store_true",
                   help="after re-cabling a host: forget its probed DUTs and slots and rediscover every board "
                        "(reset lines used before first, old names kept); needs -r and --yes")
    p.add_argument("--rounds", type=int, default=8, help="--human: at most this many boards (default 8)")
    p.add_argument("--wait", type=float, default=120, help="--human: seconds to wait for each board (default 120)")

    d = sub.add_parser("dut", help="boards: list/status, lease, reset, boot an SDK, serial console")
    ds = d.add_subparsers(dest="dut_cmd", metavar="<action>")
    dl = ds.add_parser("list", help="all DUTs on all reachable hosts")
    dl.add_argument("--soc", help="only this SoC (as named in the SoC profiles)")
    dl.add_argument("--sdk", help="only DUTs that can run this SDK")
    dl.add_argument("--free", action="store_true", help="only DUTs nobody leases")
    ex = ds.add_parser("export", help="registered DUT specs as YAML; --dir writes one <site>/duts/<id>.yaml per board")
    ex.add_argument("--dir", help="write each board to DIR/<id>.yaml (e.g. $BSP_SITE/duts) instead of printing")
    ex.add_argument("--host", help="only boards of this host")
    ds.add_parser("logins", help="ssh console logins: `ssh bsp-<dut>@<host>` opens a DUT console (no password)")
    for name, hlp in (("status", "stage, lease, fastboot, control lines, console tail"),
                      ("log", "recent console lines (passive)")):
        x = ds.add_parser(name, help=hlp)
        x.add_argument("dut")
        if name == "log":
            x.add_argument("--lines", type=int, default=40)
            x.add_argument("--grep")
    pr = ds.add_parser("present", help="whether the DUT's download/fastboot port is on the USB bus")
    pr.add_argument("dut")
    rf = ds.add_parser("refresh", help="re-read the board in a slot after swapping it: reset into download "
                                       "mode, identify, update board/soc/model (and the download port if it moved)")
    rf.add_argument("dut", help="DUT name or slot (s3, <host>/s3)")
    rf.add_argument("--name", help="rename the DUT to this")
    rf.add_argument("--rename", action="store_true", help="rename the DUT after what identify read")
    rf.add_argument("--board", help="board name to record when identify cannot read it")
    rf.add_argument("--soc", help="SoC to record when the boot ROM does not tell")
    rf.add_argument("--lease")
    rf.add_argument("--timeout", type=float, default=600, help="how long to follow the job (s)")
    sl = sub.add_parser("slot", help="slots: every serial console on a host, with or without a board")
    sls = sl.add_subparsers(dest="slot_cmd", metavar="<action>")
    sll = sls.add_parser("list", help="all slots on all reachable hosts")
    sll.add_argument("--free", action="store_true", help="only slots without a board")
    sls.add_parser("scan", help="match each host's consoles to slots now (new adapters get a slot and a console)")
    for name, hlp in (("status", "one slot: console, reset line, download port, board, history"),
                      ("console", "attach to the slot's console interactively (Ctrl-A Ctrl-X detaches)"),
                      ("pipe", "raw serial-mux protocol of the slot's console on stdin/stdout, for programs"),
                      ("forget", "forget a slot whose adapter is gone (stops its console daemon)")):
        x = sls.add_parser(name, help=hlp)
        x.add_argument("slot", help="<host>/s3, or s3 with one host")
    pp = ds.add_parser("pipe", help="raw serial-mux protocol on stdin/stdout for programs (no lease; "
                                    "input follows the console's own gate)")
    pp.add_argument("dut")
    sg = ds.add_parser("stage", help="send a local file to the host (once, by sha256) and load it into RAM: "
                                     "fastboot stage + continue")
    sg.add_argument("dut")
    sg.add_argument("file")
    sg.add_argument("--no-continue", action="store_true", help="only stage, do not run `fastboot continue`")
    sg.add_argument("--timeout", type=float, default=300, help="seconds for the stage")
    sg.add_argument("--lease")
    rn_ = ds.add_parser("renew", help="extend your lease (same token, holder and purpose)")
    rn_.add_argument("dut")
    rn_.add_argument("--ttl", type=int, default=3600)
    rn_.add_argument("--lease")
    a = ds.add_parser("acquire", help="lease a DUT (token remembered for later commands)")
    a.add_argument("dut")
    a.add_argument("--holder", default=os.environ.get("BSP_HOLDER") or os.environ.get("USER") or "anonymous")
    a.add_argument("--ttl", type=int, default=3600)
    a.add_argument("--purpose")
    a.add_argument("--lease")
    r = ds.add_parser("release", help="release a DUT lease")
    r.add_argument("dut")
    r.add_argument("--force", action="store_true")
    r.add_argument("--lease")
    rs = ds.add_parser("reset", help="reset (by default: into FDL download mode, waits for it)")
    rs.add_argument("dut")
    rs.add_argument("--no-fdl", action="store_true", help="plain reset, do not wait for FDL")
    rs.add_argument("--lease")
    rs.add_argument("--no-wait", action="store_true", help="human backend: return the job id instead of waiting")
    rs.add_argument("--timeout", type=float, default=1800, help="how long to follow a waiting reset job (s)")
    cf = ds.add_parser("confirm", help="tell a waiting reset/boot that you did what it asked (human backend)")
    cf.add_argument("dut")
    cf.add_argument("note", nargs="*", help="optional note for the log, e.g. 'pressed reset'")
    bd = ds.add_parser("bind", help="register a board slot from a console + USB port only (no GPIO): "
                                    "resets are done by a person (human backend)")
    bd.add_argument("dut", help="name for the slot, e.g. flash-slot-1")
    bd.add_argument("--console", required=True, help="serial-mux alias or tty (/dev/ttyUSB0)")
    bd.add_argument("--usb", required=True, help="fastboot USB selector, e.g. usb:1-1.2 (see `fastboot devices -l`)")
    bd.add_argument("--soc", help="SoC id (packs/*/socs); enables download-mode auto-detection")
    bd.add_argument("--board", default="", help="board model, free text")
    bd.add_argument("--description", default="")
    bd.add_argument("--sdks", default="", help="comma list (default: every SDK of its SoC)")
    bd.add_argument("--fdl", choices=["strapped", "button"], default="strapped",
                    help="strapped: a reset enters download mode; button: the person also holds BOOT/download")
    bd.add_argument("--host", help="board host the console and USB are attached to (needed with several hosts)")
    bd.add_argument("--replace", action="store_true", help="overwrite an existing DUT with this name")
    b = ds.add_parser("boot", help="boot an SDK (--method load: RAM boot | download: flash), "
                                   "or local files in order (--files, RAM boot)")
    b.add_argument("dut")
    b.add_argument("--sdk")
    b.add_argument("--files", nargs="+", metavar="FILE",
                   help="boot these local files in order, e.g. FSBL.bin u-boot-opensbi.itb Image.itb")
    b.add_argument("--no-reset", action="store_true", help="--files: the board is already in download mode")
    b.add_argument("--first-wait", type=float, default=8, help="--files: seconds after the first file (default 8)")
    b.add_argument("--later-wait", type=float, default=3, help="--files: seconds after later files (default 3)")
    b.add_argument("--port-timeout", type=float, default=20,
                   help="--files: then poll this long for the download port (default 20)")
    b.add_argument("--method", default="load", choices=["load", "download"])
    b.add_argument("--variant", help="e.g. uboot-only")
    b.add_argument("--no-wait", action="store_true")
    b.add_argument("--timeout", type=float, default=900)
    b.add_argument("--lease")
    c = ds.add_parser("console", help="run a shell command on the DUT console (no command: interactive attach)")
    c.add_argument("dut")
    c.add_argument("command", nargs="*", help="e.g. 'dmesg | grep mytag' (quote pipes); omit to attach")
    c.add_argument("--expect", help="completion regex (default: shell prompt)")
    c.add_argument("--timeout", type=float, default=20)
    c.add_argument("--lease")
    ad = ds.add_parser("add", help="register a DUT on a host from a JSON spec (what probe produces)")
    ad.add_argument("host")
    ad.add_argument("--spec", required=True, help="JSON or @file.json")
    ad.add_argument("--replace", action="store_true")
    rm = ds.add_parser("remove", help="forget a probed DUT")
    rm.add_argument("dut")
    rm.add_argument("--force", action="store_true")
    rn = ds.add_parser("rename", help="rename a DUT (its reset/fdl aliases follow)")
    rn.add_argument("dut")
    rn.add_argument("new")
    rn.add_argument("--console", action="store_true", help="also rename the serial-mux console alias")
    rn.add_argument("--lease")
    st = ds.add_parser("set", help="edit a DUT: board, soc, description, sdks, usb, reset pin, FDL")
    st.add_argument("dut")
    st.add_argument("--board")
    st.add_argument("--soc")
    st.add_argument("--description")
    st.add_argument("--sdks", help="comma list; '' = all SDKs of the board's SoC")
    st.add_argument("--usb", help="fastboot selector, e.g. usb:2-1.4")
    st.add_argument("--reset-pin", dest="reset_pin")
    st.add_argument("--pulse-ms", dest="pulse_ms", type=int)
    st.add_argument("--fdl", choices=["strapped", "button"])
    st.add_argument("--fdl-pin", dest="fdl_pin")
    st.add_argument("--lease")


def run(fleet: Fleet, args, book: LeaseBook):
    if args.cmd == "hosts":
        return hosts(fleet, args)
    if args.cmd == "init":
        return init(fleet, args)
    if args.cmd == "probe":
        return probe(fleet, args)
    if args.cmd == "slot":
        return slot_cmd(fleet, args)
    if args.dut_cmd is None:
        raise InvalidRequest("missing dut action", hint="bsp dut --help")
    if args.dut_cmd == "list":
        return dut_list(fleet, args)
    if args.dut_cmd == "add":
        return dut_add(fleet, args)
    if args.dut_cmd == "bind":
        return dut_bind(fleet, args)
    if args.dut_cmd == "export":
        return export(fleet, args)
    if args.dut_cmd == "logins":
        return logins(fleet, args)
    return dut_cmd(fleet, args, book)


# --------------------------------------------------------------- slots --
def slot_cmd(fleet: Fleet, args) -> tuple[Any, str]:
    c = args.slot_cmd
    if c is None:
        raise InvalidRequest("missing slot action", hint="bsp slot --help")
    if c in ("list", "scan"):
        items: list[dict[str, Any]] = []
        for h, r in fleet.fan("slot_scan" if c == "scan" else "slot_list"):
            if r.get("ok"):
                items += r["result"]["slots"] if c == "scan" else r["result"]
            else:
                fleet.errors[h.id] = r["error"]["message"]
        if getattr(args, "free", False):
            items = [s for s in items if not s.get("dut")]
        text = render_slots(items)
        if fleet.errors:
            text += "\nunreachable: " + ", ".join(f"{k} ({v[:50]})" for k, v in fleet.errors.items())
        return items, text
    h = fleet.find_dut(args.slot)
    if c == "forget":
        r = _ok(h, h.call("slot_forget", {"slot": args.slot.rsplit("/", 1)[-1]}))
        return r, f"forgot slot {h.id}/{r['forgotten']}"
    st = _ok(h, h.call("slot_status", {"slot": args.slot.rsplit("/", 1)[-1]}))
    return slot_cmd_on(h, st, c, args)


def slot_cmd_on(h: HostHandle, st: dict[str, Any], c: str, args) -> tuple[Any, str]:
    if c == "pipe":
        return _pipe_to(h, st, st["ref"])
    if c == "console":
        return _attach_to(h, st, st["ref"])
    hist = [f"  {time.strftime('%m-%d %H:%M', time.localtime(x['at']))} {x['event']} {x.get('dut') or ''}"
            f"{' reset ' + x['reset'] if x.get('reset') else ''}{' usb ' + x['usb'] if x.get('usb') else ''}"
            for x in st.get("history", [])]
    return st, render_slots([st]) + ("\nhistory:\n" + "\n".join(hist) if hist else "")

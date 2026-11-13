"""`bsp` — the atomic CLI for the bsp dev loop (humans and agents alike).

Client-side groups:
    bsp hosts | init                    which board hosts are reachable (local or remote mode)
    bsp probe                           discover boards on the host(s) and register them
    bsp dut list|status|log|acquire|renew|release|reset|boot|stage|present|console|pipe|confirm|bind|logins|
            add|remove|rename|set|export
                                        boards, routed to whichever host owns them
    bsp sdk list|show|status|plan|acquire|release|build|job|jobs|exec|get|put
                                        SDK targets on build servers (lease, task branch, build)
    bsp catalog list|get|put|recipe|rm|history|rollback|import|export|status
                                        the SDK catalog in the registry (shared, versioned, validated)
    bsp install                         write ./SKILL.md for AI agents working in this directory
    bsp hints                           hints from the configuration (packs, site)
    bsp remote list|add|remove|use|show manage known hosts (~/.config/bsp/remotes.json)

Host-level groups are fetched from the host and passed through
(`gpio`, `relay`, `button`, `board`, `job`, `events`, `status`); pick the host
with -r when more than one is reachable.

Global options (anywhere on the line): -r/--remote NAME[,NAME|all], --url URL,
--token T, --json (machine output: {"ok":..., "result"|"error"}), --stateless
(ignore saved defaults and saved lease tokens; for parallel agents).

Exit codes: 0 ok, 1 failed, 2 usage error, 3 host unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..core.cmdspec import CommandSpec, build_parser, parse_tokens
from ..core.errors import BspError
from . import catalogcli, dutcli, sdkcli
from .fleet import Fleet, HostHandle, LeaseBook, connect
from .remotes import Remote, RemoteError, RemoteStore
from .transport import Unreachable

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 3
CLIENT_GROUPS = {"hosts", "init", "probe", "dut", "slot", "sdk", "install", "hints", "catalog", "images"}


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr)


def print_error(err: dict[str, Any], prefix: str = "") -> None:
    eprint(f"{prefix}error[{err.get('code')}]: {err.get('message')}")
    if err.get("hint"):
        eprint(f"{prefix}hint: {err['hint']}")
    det = err.get("details") or {}
    tail = det.get("tail") or det.get("console_tail") or det.get("console")
    if tail:
        eprint(f"{prefix}details:")
        for line in (tail if isinstance(tail, list) else str(tail).splitlines())[-25:]:
            eprint(f"{prefix}  {line}")


# ------------------------------------------------------------ remote --
def _probe_remote(r: Remote) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        h = connect(r)
    except Unreachable as u:
        return {"reachable": False, "error": str(u.reason)}
    return {"reachable": True, "bsp": True, "latency_ms": round((time.monotonic() - t0) * 1000),
            "via": h.transport.label, "host": h.id, "version": h.status.get("version")}


def remote_main(store: RemoteStore, argv: list[str], as_json: bool) -> int:
    ap = argparse.ArgumentParser(prog="bsp remote", description="Manage named bsp host instances")
    sub = ap.add_subparsers(dest="cmd", metavar="<action>")
    ls = sub.add_parser("list", help="list instances with live status")
    ls.add_argument("--no-check", action="store_true", help="do not contact the instances")
    ad = sub.add_parser("add", help="add or update an instance")
    ad.add_argument("name")
    ad.add_argument("url")
    ad.add_argument("--token")
    ad.add_argument("--ssh", help="user@host: tunnel over ssh when the URL is not routable")
    ad.add_argument("--description", default="")
    ad.add_argument("--fallback", action="append", default=[], metavar="URL")
    ad.add_argument("--default", action="store_true", help="also make it the default")
    rm = sub.add_parser("remove", help="remove (or hide a built-in) instance")
    rm.add_argument("name")
    us = sub.add_parser("use", help="set the default instance (interactive convenience)")
    us.add_argument("name")
    sh = sub.add_parser("show", help="show one instance with live status")
    sh.add_argument("name", nargs="?")
    a = ap.parse_args(argv or ["list"])

    if a.cmd == "add":
        out: Any = store.add(a.name, a.url, a.token, a.description, a.default, a.fallback, a.ssh).to_dict()
    elif a.cmd == "remove":
        out = store.remove(a.name).to_dict()
    elif a.cmd == "use":
        out = store.use(a.name).to_dict()
    elif a.cmd == "show":
        r = store.lookup(a.name) if a.name else store.select()[0]
        out = {**r.to_dict(), "default": r.name == store.default_name, "status": _probe_remote(r)}
    else:
        rs = list(store.remotes().values())
        with ThreadPoolExecutor(max_workers=8) as pool:
            probes = list(pool.map(_probe_remote, rs)) if not a.no_check else [None] * len(rs)
        out = [{**r.to_dict(), "default": r.name == store.default_name, "status": p} for r, p in zip(rs, probes)]
        if not as_json:
            rows = [("", "NAME", "STATUS", "VIA", "HOST", "DESCRIPTION")]
            for o in out:
                p = o["status"]
                stat = "-" if p is None else (f"up {p['latency_ms']}ms" if p["reachable"] else "DOWN")
                rows.append(("*" if o["default"] else "", o["name"], stat, (p or {}).get("via") or o["url"],
                             (p or {}).get("host") or "", o["description"]))
            print(dutcli._table(rows))
            print(f"\nuser config: {store.user_path}")
            return EXIT_OK
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return EXIT_OK


# ------------------------------------------------------------- global --
def _global_parser() -> argparse.ArgumentParser:
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("-r", "--remote")
    g.add_argument("--url")
    g.add_argument("--token", default=os.environ.get("BSP_TOKEN"))
    g.add_argument("--json", action="store_true")
    g.add_argument("--stateless", action="store_true")
    return g


def _client_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bsp", formatter_class=argparse.RawDescriptionHelpFormatter,
                                description=__doc__)
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    dutcli.add_parsers(sub)
    sdkcli.add_parser(sub)
    catalogcli.add_parser(sub)
    ins = sub.add_parser("install", help="write ./SKILL.md for agents: full bsp skill + what this directory is")
    ins.add_argument("--dir", default=".", help="directory to write SKILL.md into (default: current)")
    ins.add_argument("--offline", action="store_true", help="do not query board hosts for the board snapshot")
    sub.add_parser("hints", help="hints from the configuration layers (packs, site) for agents and people")
    im = sub.add_parser("images", help="images hosts stage (probe images, media-boot helpers): list, or fetch into the site")
    im.add_argument("images_cmd", nargs="?", choices=["list", "sync"], default="list",
                    help="list: where each comes from and whether the site has it; sync: fetch the missing ones once")
    sub.add_parser("remote", help="manage known bsp hosts")
    sub.add_parser("watch", help="stream live events from the host(s)")
    sub.add_parser("tools", help="AI tool schemas of the host commands (anthropic format)")
    for g in ("gpio", "relay", "button", "board", "job", "events", "status", "site"):
        sub.add_parser(g, help="host command group (passed through, see `bsp -r HOST <group> --help`)")
    return p


def _emit(g, result: Any, text: str | None, warnings: list[str] | None = None) -> None:
    if g.json:
        # `warnings` is what the human output adds after the result (hosts unreachable, a host without the module):
        # without it, JSON and text told different stories and the exit code said everything was fine.
        body: dict[str, Any] = {"ok": True, "result": result}
        if warnings:
            body["warnings"] = warnings
        print(json.dumps(body, indent=2, ensure_ascii=False, default=str))
    elif text is not None:
        print(text.rstrip("\n"))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _fail(g, err: dict[str, Any], code: int) -> int:
    if g.json:
        print(json.dumps({"ok": False, "error": err}, indent=2, ensure_ascii=False, default=str))
    else:
        print_error(err)
    return code


def cli() -> int:
    """The `bsp` command: `bsp ... | head` ends quietly. (Not in main(), which tests and tools call in-process.)"""
    import signal

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    return main()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    g, rest = _global_parser().parse_known_args(argv)
    stateless = g.stateless or os.environ.get("BSP_STATELESS", "").lower() in ("1", "true", "yes")
    try:
        store = RemoteStore(stateless=stateless)
    except RemoteError as e:
        return _fail(g, {"code": "config", "message": e.message, "hint": e.hint}, EXIT_USAGE)
    book = LeaseBook(enabled=not stateless)

    if not rest or rest[0] in ("-h", "--help", "help"):
        _client_parser().print_help()
        return EXIT_OK
    if rest[0] == "remote":
        try:
            return remote_main(store, rest[1:], g.json)
        except RemoteError as e:
            return _fail(g, {"code": "remote", "message": e.message, "hint": e.hint}, EXIT_USAGE)

    fleet = Fleet(store, g.remote, g.url, g.token)
    if rest[0] in CLIENT_GROUPS:
        parser = _client_parser()
        trailing = None
        if "--" in rest:                     # `bsp sdk exec SDK [opts] -- cmd ...`: keep cmd verbatim
            i = rest.index("--")
            rest, trailing = rest[:i], rest[i + 1:]
        try:
            args = parser.parse_args(rest)
        except SystemExit as e:
            return int(e.code or 0)
        if trailing is not None:
            if not hasattr(args, "command"):
                return _fail(g, {"code": "invalid_request", "message": "`--` is only valid for `bsp sdk exec`"}, EXIT_USAGE)
            args.command = list(args.command or []) + trailing
        args.json = g.json
        try:
            if args.cmd == "sdk":
                result, text = sdkcli.run(args, book)
            elif args.cmd == "catalog":
                result, text = catalogcli.run(args, store, g.token)
            elif args.cmd == "install":
                result, text = _install(fleet, args)
            elif args.cmd == "hints":
                result, text = _hints()
            elif args.cmd == "images":
                result, text = _images(args)
            else:
                result, text = dutcli.run(fleet, args, book)
        except RemoteError as e:
            return _fail(g, {"code": e.code, "message": e.message, "hint": e.hint},
                         EXIT_UNREACHABLE if e.code == "unreachable" else EXIT_FAIL)
        except BspError as e:
            err = e.to_dict()
            if getattr(e, "host", None):
                err["host"] = e.host
            return _fail(g, err, EXIT_FAIL)
        except KeyboardInterrupt:
            return 130
        if isinstance(result, dutcli.Quiet):
            return result.rc
        warnings = [f"{host} unreachable: {reason}" for host, reason in fleet.errors.items()]
        _emit(g, result, text, warnings)
        # The same exit code in both forms: hosts that could not be reached are not a success, even if the rest was.
        return EXIT_UNREACHABLE if fleet.errors else EXIT_OK

    # ---------------- passthrough to host commands ----------------
    if rest[0] in ("watch", "tools") and any(a in ("-h", "--help") for a in rest[1:]):
        print({"watch": "usage: bsp [-r HOST] watch [--registry]\n\nStream live events (GPIO changes, leases, boots, jobs, "
                        "cables coming and going) from the host(s) until Ctrl-C.\n  --registry  the registry's events instead: "
                        "hosts announcing themselves and their ports changing (registry.ports)\n"
                        "With --json each event is one JSON line with its host.",
               "tools": "usage: bsp [-r HOST] tools\n\nPrint the host's AI tool schemas (Anthropic format) as JSON."}[rest[0]])
        return EXIT_OK
    if rest[0] == "watch" and "--registry" in rest[1:]:
        if not store.registry_url:
            return _fail(g, {"code": "config", "message": "no registry configured",
                             "hint": "set \"registry\" in the site's remotes.json, or BSP_REGISTRY"}, EXIT_USAGE)
        try:
            reg = connect(Remote("registry", store.registry_url, g.token, source="registry"))
        except Unreachable as u:
            return _fail(g, {"code": "unreachable", "message": f"registry {store.registry_url}: {u.reason}"}, EXIT_UNREACHABLE)
        return _watch([reg], g.json)
    try:
        hosts = fleet.require()
    except RemoteError as e:
        return _fail(g, {"code": "unreachable", "message": e.message, "hint": e.hint}, EXIT_UNREACHABLE)
    if rest[0] == "watch":
        return _watch(hosts, g.json)
    if rest[0] == "tools":
        print(json.dumps(hosts[0].get("/api/v1/tools?format=anthropic"), indent=2, ensure_ascii=False))
        return EXIT_OK
    specs = [CommandSpec.from_dict(t) for t in hosts[0].get("/api/v1/tools")["tools"]]
    parser = argparse.ArgumentParser(prog="bsp", description=f"host commands ({', '.join(h.id for h in hosts)})")
    build_parser(specs, parent=parser)
    try:
        name, cargs = parse_tokens(parser, rest)
    except SystemExit as e:
        return int(e.code or 0)
    spec = next(s for s in specs if s.name == name)
    if spec.mutating and len(hosts) > 1:
        return _fail(g, {"code": "ambiguous_host",
                         "message": f"`{name}` changes state and {len(hosts)} hosts are reachable",
                         "hint": f"pick one with -r ({', '.join(h.id for h in hosts)})"}, EXIT_USAGE)
    code, multi = EXIT_OK, len(hosts) > 1
    for h in hosts:
        try:
            resp = h.call(spec.tool_name, cargs, timeout=300)
        except Unreachable as u:
            resp = {"ok": False, "error": {"code": "unreachable", "message": str(u)}}
        prefix = f"[{h.id}] " if multi else ""
        if not resp.get("ok"):
            code = EXIT_FAIL
            _fail(g, {**resp.get("error", {}), "message": prefix + str(resp.get("error", {}).get("message"))}, 1)
            continue
        if multi and not g.json:
            print(f"== {h.id} ==")
        _emit(g, resp.get("result"), resp.get("text"))
    return code


def _images(args) -> tuple[Any, str]:
    """`bsp images [list|sync]`: SoC images of this machine's site, fetched into <site>/.blobs by sync."""
    from ..core.site import layered, site_dir
    from ..dut import images
    from ..dut.socs import SocProfiles

    site = site_dir()
    if site is None:
        raise RemoteError("no site directory", code="invalid_request", hint="set BSP_SITE or create ~/.config/bsp/site")
    from .catalogcli import registry_files

    shared = registry_files(sync=False)        # the registry's SoC profiles (`bsp catalog sync`), above the site
    profiles = SocProfiles(layered("socs", site) + ([shared / "socs"] if shared and (shared / "socs").is_dir() else []))
    if args.images_cmd == "sync":
        lines: list[str] = []
        r = images.sync(profiles, site, (lambda line: print(line, file=sys.stderr)) if not args.json else lines.append)
        text = (f"fetched {len(r['fetched'])}, already here {len(r['present'])}"
                + (f", failed {len(r['failed'])}: " + "; ".join(f"{f['image']}: {f['error']}" for f in r["failed"]) if r["failed"] else ""))
        return r, text
    rows = images.plan(profiles, site)
    return rows, "\n".join(f"{r['soc']}/{r['image']:<16} {r['state']:<9} {r['file']}"
                           + (f"  sha256 {r['sha256'][:16]}..." if r.get("sha256") else "") for r in rows) or "no images"


def _hints() -> tuple[Any, str]:
    from ..core.site import hints, site_dir

    rows = [{"name": n, "text": t} for n, t in hints()]
    site = site_dir()
    if not rows:
        return rows, f"no hints configured (add <site>/hints/*.md; site: {site or 'none found'})"
    return rows, "\n\n".join(f"## {r['name']}\n{r['text']}" for r in rows)


def _install(fleet: Fleet, args) -> tuple[Any, str]:
    from pathlib import Path

    from . import install

    duts = None
    if not args.offline:
        rows, ok = [], False
        for _, r in fleet.fan("dut_list", timeout=30):
            if r.get("ok"):
                ok = True
                rows += r["result"]
        duts = rows if ok else None
    bsp_path = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else "bsp"
    from ..core.site import hints

    res = install.install(Path(args.dir), sdkcli.catalog(), duts, bsp_path, hints())
    what = (f"configured as {', '.join(res['sdks'])}" if res["sdks"]
            else "not a configured SDK (the file tells the agent to ask the user)")
    return res, f"wrote {res['file']} ({what})" + (f"\nbacked up the previous one to {res['backup']}" if res["backup"] else "")


def _watch(hosts: list[HostHandle], as_json: bool = False) -> int:
    import threading

    def one(h: HostHandle) -> None:
        try:
            for line in h.transport.stream("/api/v1/events"):
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if as_json:                      # for programs: one event per line, with its host
                        print(json.dumps({"host": h.id, **ev}, ensure_ascii=False, default=str), flush=True)
                        continue
                    ts = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
                    data = " ".join(f"{k}={v}" for k, v in ev["data"].items() if v is not None)
                    print(f"[{h.id}] {ts} {ev['topic']:<18} {data}", flush=True)
        except Unreachable as u:
            eprint(f"[{h.id}] stream lost: {u}")

    ts = [threading.Thread(target=one, args=(h,), daemon=True) for h in hosts]
    for t in ts:
        t.start()
    try:
        for t in ts:
            t.join()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(cli())

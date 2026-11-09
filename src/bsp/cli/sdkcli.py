"""`bsp sdk ...` — SDK instances on build servers (client side, no service needed).

Everything runs on the build server through bsp/sdk/helper.py: piped over ssh,
or executed locally when the CLI already runs on that server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..core.errors import InvalidRequest, NotFound, BspError
from ..sdk.catalog import SdkCatalog, sharing

HELPER = Path(__file__).resolve().parents[1] / "sdk" / "helper.py"

# default (non -v) view: problems and milestones only; a heartbeat shows progress
HIGHLIGHT = re.compile(r"\berror\b|\bwarning:|\bfailed\b|\*\*\*|Image\.itb|\.itb\b.*(created|done)|"
                       r"^\s*(OBJCOPY|ITB|MKIMAGE)\s|^bsp:", re.I)


def catalog() -> SdkCatalog:
    from ..core.site import layered

    roots = layered("sdk")
    from .catalogcli import registry_layer

    layer = registry_layer()                 # the registry's catalog above the site, when there is a registry
    if layer:
        roots.append(layer)
    if os.environ.get("BSP_SDK_CATALOG"):
        roots.append(Path(os.environ["BSP_SDK_CATALOG"]))
    return SdkCatalog(roots)


def _local_ips() -> set[str]:
    ips = {"127.0.0.1"}
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
        ips |= set(out.split())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ips


def is_local(server) -> bool:
    if os.environ.get("BSP_SDK_LOCAL") == "1":
        return True
    fq = socket.getfqdn()
    names = {server.id, server.raw.get("dns", ""), server.host}
    return server.host in _local_ips() or fq in names or socket.gethostname() in names


def helper(inst, op: str, timeout: float = 120, **args: Any) -> Any:
    req = {"op": op, "path": inst.path, **args}
    src = HELPER.read_text()
    argv = ["python3", "-", json.dumps(req)]
    if not is_local(inst.server):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", inst.server.ssh,
                " ".join(shlex.quote(a) for a in argv)]
    try:
        r = subprocess.run(argv, input=src, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BspError(f"build server {inst.server.id} did not answer within {timeout:g}s") from None
    line = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        reply = json.loads(line)
    except ValueError:
        raise BspError(f"cannot run the bsp helper on {inst.server.ssh}: {(r.stderr or r.stdout).strip()[-300:]}",
                       hint="needs ssh key access and python3 on the build server") from None
    if not reply.get("ok"):
        e = reply["error"]
        cls = {"conflict": _Conflict, "forbidden": _Forbidden, "not_found": NotFound,
               "invalid_request": InvalidRequest}.get(e.get("code"), BspError)
        raise cls(e["message"], hint=e.get("hint"), details=e.get("details"))
    return reply["result"]


class _Conflict(BspError):
    status, code = 409, "conflict"


class _Forbidden(BspError):
    status, code = 403, "forbidden"


class _CommandFailed(BspError):
    status, code = 400, "command_failed"


# ------------------------------------------------------------ commands --
def _key(i) -> str:
    return f"ws:{i.workspace.id}"            # leases belong to the workspace, not the SDK target


def sdk_list(args) -> tuple[Any, str]:
    items = [i.summary() for _, i in sorted(catalog().instances.items())]
    if getattr(args, "soc", None):
        items = [s for s in items if s["soc"] == args.soc]
    rows = [("SDK", "SOC", "WORKSPACE", "SERVER", "LOAD", "DOWNLOAD", "PATH")]
    rows += [(s["id"], s["soc"] or "-", s["workspace"], s["server"], s["boot"]["load"], s["boot"]["download"],
              s["path"]) for s in items]
    return items, _table(rows)


def sdk_show(args) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    out = {**i.summary(), "dev_projects": i.dev_projects, "artifacts": i.artifacts(),
           "shares_workspace_with": [x for x in sharing(catalog(), i.workspace.id) if x != i.id],
           "boot_notes": {m: b.notes for m, b in i.boot.items()}}
    text = "\n".join([f"{i.id}: {out['description']}",
                      f"  server     : {i.server.id} ({i.server.ssh})  path {i.path}",
                      f"  soc        : {i.soc}   workspace: {i.workspace.id}"
                      + (f" (shared with {', '.join(out['shares_workspace_with'])}: one lease covers all)"
                         if out["shares_workspace_with"] else ""),
                      f"  components : {', '.join(out['components'])}",
                      f"  dev repos  : {', '.join(i.dev_projects)}",
                      f"  boot       : " + ", ".join(f"{m}={s}" for m, s in out['boot'].items()),
                      "  artifacts  :", *[f"    {r:<11} {p}" for r, p in out["artifacts"].items()]])
    return out, text


def sdk_status(args) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    st = helper(i, "status", projects=i.dev_projects)
    lease = st.get("lease")
    shared = [x for x in sharing(catalog(), i.workspace.id) if x != i.id]
    lines = [f"{i.id} @ {i.server.id}:{i.path}  (workspace {i.workspace.id}"
             + (f", shared with {', '.join(shared)}" if shared else "") + ")",
             f"  lease : {lease['holder']} task={lease['task']} branch={lease['branch']} via {lease.get('sdk')} "
             f"({lease['expires_in_s']}s left)" if lease else "  lease : free"]
    for p in st["projects"]:
        lines.append(f"  {p['project']:<14} {p['branch']:<28} {p['head']} dirty={p['dirty']} stashes={p['stashes']}  {p['subject']}")
    for j in st.get("recent_jobs", [])[-3:]:
        lines.append(f"  job {j['id']}: {j['status']} {j['meta'].get('component', '')}")
    return st, "\n".join(lines)


def sdk_acquire(args, book) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    token = args.lease or os.environ.get("BSP_SDK_LEASE") or book.get(_key(i))
    projects = args.projects.split(",") if args.projects else i.dev_projects
    res = helper(i, "acquire", task=args.task, holder=args.holder, ttl=args.ttl, token=token,
                 projects=projects, branch=args.branch, sdk=i.id, workspace=i.workspace.id)
    book.put(_key(i), res["token"], args.ttl, holder=args.holder, task=args.task)
    shared = [x for x in sharing(catalog(), i.workspace.id) if x != i.id]
    lines = [f"leased workspace {i.workspace.id} (sdk {i.id}) to {res['holder']} for task '{res['task']}' "
             f"({res['expires_in_s']}s)" + (f"; also covers {', '.join(shared)}" if shared else ""),
             f"token: {res['token']}"]
    for proj, rec in res["projects"].items():
        lines.append(f"  {proj}: was {rec['orig_branch']}@{rec['orig_head'][:12]}"
                     + (f", stashed local changes as {rec['stash'][:12]}" if rec.get("stash") else ", clean")
                     + f" -> now on {res['branch']}")
    lines.append(f"edit under {i.server.id}:{i.path}/{projects[0]} , then `bsp sdk build {i.id} <component>`")
    return res, "\n".join(lines)


def sdk_release(args, book) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    token = args.lease or os.environ.get("BSP_SDK_LEASE") or book.get(_key(i))
    res = helper(i, "release", token=token, restore=not args.keep, force=args.force)
    book.drop(_key(i))
    if not res.get("released"):
        return res, res.get("message", "not leased")
    lines = [f"released {i.id} (task {res['task']}); {res['note']}"]
    for proj, r in res["projects"].items():
        lines.append(f"  {proj}: " + ", ".join(f"{k}={v}" for k, v in r.items()))
    return res, "\n".join(lines)


def sdk_build(args, book, out=sys.stdout) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    token = args.lease or os.environ.get("BSP_SDK_LEASE") or book.get(_key(i))
    cmd = i.build_command(args.component, args.clean)
    job = helper(i, "build_start", command=cmd, component=args.component, token=token,
                 path_prepend=i.path_prepend, env_vars=i.env_vars)
    print(f"build job {job['id']} on {i.server.id}: {cmd}", file=out, flush=True)
    if job.get("attach"):
        print(f"  (attach: ssh {i.server.ssh} -t {job['attach']})", file=out, flush=True)
    if args.no_wait:
        return job, f"started; follow with `bsp sdk job {i.id} {job['id']}`"
    return follow_build(i, job["id"], args.verbose, out, args.timeout)


def follow_build(i, job_id: str, verbose: bool, out, timeout: float) -> tuple[Any, str]:
    offset, t0, tail, buf, count, beat = 0, time.time(), [], "", 0, time.time()
    while True:
        v = helper(i, "build_poll", id=job_id, offset=offset)
        offset = v["offset"]
        buf += v["chunk"]
        lines = buf.split("\n")
        buf = lines.pop()
        for line in lines:
            tail = (tail + [line])[-40:]
            count += 1
            if verbose or HIGHLIGHT.search(line):
                print(f"  | {line}", file=out, flush=True)
        if not verbose and time.time() - beat > 30:
            beat = time.time()
            last = tail[-1].strip()[:90] if tail else ""
            print(f"  ... {int(beat - t0)}s, {count} log lines, last: {last}", file=out, flush=True)
        if v["status"] != "running" and not v["chunk"]:
            break
        if time.time() - t0 > timeout:
            raise BspError(f"build {job_id} still running after {timeout:g}s",
                           hint=f"it keeps running; follow with `bsp sdk job {i.id} {job_id}`")
        if not v["chunk"]:
            time.sleep(1.0)
    v["elapsed_s"] = round(time.time() - t0, 1)
    v.pop("chunk", None)
    v["tail"] = tail[-25:]
    if v["status"] != "succeeded":
        raise BspError(f"build {job_id} failed (exit {v['rc']})",
                       hint=f"full log: {i.server.ssh}:{v['log']}  (bsp sdk job {i.id} {job_id} --log)",
                       details={"tail": tail[-25:]})
    arts = i.artifacts()
    return v, f"build {job_id} succeeded in {v['elapsed_s']}s; artifacts in {i.artifact_dir()}" + (
        f" (kernel-fit: {arts.get('kernel-fit')})" if "kernel-fit" in arts else "")


def sdk_job(args) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    if args.log:
        v = helper(i, "build_poll", id=args.id, offset=0)
        return v, v["chunk"][-20000:]
    if args.follow:
        return follow_build(i, args.id, False, sys.stdout, 7200)
    v = helper(i, "build_poll", id=args.id, offset=10 ** 12)
    v.pop("chunk", None)
    return v, f"job {v['id']}: {v['status']} rc={v['rc']} log={v['log']}"


def sdk_jobs(args) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    js = helper(i, "jobs")
    return js, "\n".join(f"{j['id']}  {j['status']:<10} {j['meta'].get('component', '')}" for j in js) or "(no jobs)"


def _token(i, args, book):
    return getattr(args, "lease", None) or os.environ.get("BSP_SDK_LEASE") or book.get(_key(i))


def sdk_exec(args, book) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    words = list(args.command)
    if words and words[0] == "--":
        words = words[1:]
    cmd = " ".join(words)
    if not cmd.strip():
        raise InvalidRequest("no command given", hint="bsp sdk exec <sdk> -- git status")
    cwd = args.cwd if args.cwd is not None else (i.dev_projects[0] if i.dev_projects else ".")
    r = helper(i, "exec", timeout=args.timeout + 30, command=cmd, cwd=cwd, timeout_s=args.timeout,
               token=_token(i, args, book), mutating=not args.read_only)
    if r["rc"] != 0:
        raise _CommandFailed(f"command exited {r['rc']} in {i.server.id}:{r['cwd']}", hint="output in details",
                       details={"tail": r["output"].splitlines()[-40:]})
    return r, r["output"].rstrip("\n")


def sdk_get(args, book) -> tuple[Any, str]:
    import base64

    i = catalog().get(args.sdk)
    r = helper(i, "get", file=args.file)
    data = base64.b64decode(r["b64"])
    if args.output:
        Path(args.output).write_bytes(data)
        return {"file": args.file, "saved": args.output, "size": r["size"]}, f"saved {args.file} -> {args.output} ({r['size']} B)"
    return {"file": args.file, "size": r["size"], "text": data.decode("utf-8", "replace")}, data.decode("utf-8", "replace").rstrip("\n")


def sdk_put(args, book) -> tuple[Any, str]:
    import base64

    i = catalog().get(args.sdk)
    data = Path(args.input).read_bytes() if args.input != "-" else sys.stdin.buffer.read()
    r = helper(i, "put", file=args.file, b64=base64.b64encode(data).decode(), token=_token(i, args, book))
    return r, f"wrote {args.file} ({r['size']} B) on {i.server.id}"


def sdk_plan(args) -> tuple[Any, str]:
    i = catalog().get(args.sdk)
    if args.boot:
        p = i.boot_plan(args.boot, args.variant)
        return p, "\n".join([f"{i.id} boot {args.boot}"] + [
            f"  {n:>2}. {s['do']:<10} " + " ".join(f"{k}={v}" for k, v in s.items() if k not in ("do", "artifact"))
            for n, s in enumerate(p["steps"], 1)])
    cmd = i.build_command(args.component or "kernel", False)
    return {"server": i.server.ssh, "command": cmd}, f"ssh {i.server.ssh}\n  {cmd}"


def _table(rows) -> str:
    w = [max(len(str(r[c])) for r in rows) for c in range(len(rows[0]))]
    return "\n".join("  ".join(str(x).ljust(w[c]) for c, x in enumerate(r)).rstrip() for r in rows)


def add_parser(sub) -> None:
    sp = sub.add_parser("sdk", help="SDK instances on build servers: lease + task branch, build, plans")
    s = sp.add_subparsers(dest="sdk_cmd", metavar="<action>")
    sl = s.add_parser("list", help="SDK targets: SoC, workspace, boot-method status")
    sl.add_argument("--soc", help="only SDKs for this SoC")
    for name, hlp in (("show", "one SDK: components, dev repos, artifact paths"),
                      ("status", "lease, branch and dirty state of the dev repos, recent builds"),
                      ("jobs", "recent build jobs")):
        s.add_parser(name, help=hlp).add_argument("sdk")
    a = s.add_parser("acquire", help="lease the SDK: stash local changes, check out task branch bsp/<task>")
    a.add_argument("sdk")
    a.add_argument("--task", required=True, help="task name -> branch bsp/<task>")
    a.add_argument("--holder", default=os.environ.get("BSP_HOLDER") or os.environ.get("USER") or "anonymous")
    a.add_argument("--ttl", type=int, default=8 * 3600)
    a.add_argument("--branch", help="explicit branch name (default bsp/<task>)")
    a.add_argument("--projects", help="comma list of repos (default: family dev_projects)")
    a.add_argument("--lease")
    r = s.add_parser("release", help="release: park uncommitted work, restore original branch + stash")
    r.add_argument("sdk")
    r.add_argument("--keep", action="store_true", help="stay on the task branch, do not restore")
    r.add_argument("--force", action="store_true")
    r.add_argument("--lease")
    b = s.add_parser("build", help="build a component on the build server (follows the log, exit code = result)")
    b.add_argument("sdk")
    b.add_argument("component")
    b.add_argument("--clean", action="store_true")
    b.add_argument("--no-wait", action="store_true")
    b.add_argument("-v", "--verbose", action="store_true", help="print every log line")
    b.add_argument("--timeout", type=float, default=7200)
    b.add_argument("--lease")
    j = s.add_parser("job", help="state of a build job (--follow, --log)")
    j.add_argument("sdk")
    j.add_argument("id")
    j.add_argument("--follow", action="store_true")
    j.add_argument("--log", action="store_true")
    e = s.add_parser("exec", help="run a shell command in the SDK tree on its build server (remote dev)")
    e.add_argument("sdk")
    e.add_argument("command", nargs="*", help="after --, e.g. -- git diff --stat")
    e.add_argument("--cwd", help="dir relative to the SDK root (default: first dev repo, e.g. linux-6.18)")
    e.add_argument("--timeout", type=int, default=300)
    e.add_argument("--read-only", action="store_true", help="allowed even if someone else holds the lease")
    e.add_argument("--lease")
    gt = s.add_parser("get", help="print (or -o save) a file from the SDK tree")
    gt.add_argument("sdk")
    gt.add_argument("file", help="path relative to the SDK root, e.g. linux-6.18/init/main.c")
    gt.add_argument("-o", "--output")
    pt = s.add_parser("put", help="write a file into the SDK tree (needs your lease)")
    pt.add_argument("sdk")
    pt.add_argument("file", help="path relative to the SDK root")
    pt.add_argument("input", help="local file, or - for stdin")
    pt.add_argument("--lease")
    p = s.add_parser("plan", help="dry run: build command, or boot steps with --boot load|download")
    p.add_argument("sdk")
    p.add_argument("component", nargs="?")
    p.add_argument("--boot", choices=["load", "download"])
    p.add_argument("--variant")


def run(args, book):
    c = args.sdk_cmd
    if c == "list":
        return sdk_list(args)
    if c == "show":
        return sdk_show(args)
    if c == "status":
        return sdk_status(args)
    if c == "acquire":
        return sdk_acquire(args, book)
    if c == "release":
        return sdk_release(args, book)
    if c == "build":
        return sdk_build(args, book, sys.stderr if args.json else sys.stdout)
    if c == "job":
        return sdk_job(args)
    if c == "jobs":
        return sdk_jobs(args)
    if c == "plan":
        return sdk_plan(args)
    if c == "exec":
        return sdk_exec(args, book)
    if c == "get":
        return sdk_get(args, book)
    if c == "put":
        return sdk_put(args, book)
    raise InvalidRequest("missing sdk action", hint="bsp sdk --help")

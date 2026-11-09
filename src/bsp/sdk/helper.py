#!/usr/bin/env python3
"""bsp SDK helper — runs ON the build server (piped over ssh, or locally).

Stdlib only, Python >= 3.6. One JSON request on argv[1], one JSON reply on stdout:
    {"ok": true, "result": ...}  |  {"ok": false, "error": {"code", "message", "hint"}}

State lives inside the SDK tree under .bsp/ :
    .bsp/lease.json            who holds the SDK, task branch, what was stashed
    .bsp/jobs/<id>/{script,log,rc,meta.json}   detached build jobs
    .bsp/build.lock            flock: one build per SDK at a time
"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

DEFAULT_TTL = 8 * 3600


class Fail(Exception):
    def __init__(self, code, message, hint=None, details=None):
        Exception.__init__(self, message)
        self.err = {"code": code, "message": message}
        if hint:
            self.err["hint"] = hint
        if details is not None:
            self.err["details"] = details


def sh(args, cwd=None, check=True):
    p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    out, err = p.communicate()
    if check and p.returncode != 0:
        raise Fail("command_failed", "{} failed in {}: {}".format(" ".join(args), cwd, (err or out).strip()[-400:]))
    return p.returncode, out.strip()


def git(proj, *args, **kw):
    return sh(["git"] + list(args), cwd=proj, **kw)[1]


def state_dir(path):
    d = os.path.join(path, ".bsp")
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def read_lease(path):
    p = os.path.join(path, ".bsp", "lease.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        lease = json.load(f)
    if lease.get("expires", 0) < time.time():
        lease["expired"] = True
    return lease


def write_lease(path, lease):
    p = os.path.join(state_dir(path), "lease.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(lease, f, indent=2)
    os.rename(tmp, p)


def public(lease):
    if not lease:
        return None
    out = dict(lease)
    out.pop("token", None)
    out["expires_in_s"] = int(lease["expires"] - time.time())
    return out


def proj_state(path, proj):
    p = os.path.join(path, proj)
    if not os.path.isdir(os.path.join(p, ".git")) and not os.path.isfile(os.path.join(p, ".git")):
        raise Fail("not_found", "{} is not a git checkout".format(p))
    branch = git(p, "rev-parse", "--abbrev-ref", "HEAD")
    head = git(p, "rev-parse", "HEAD")
    dirty = [l for l in git(p, "status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
    stashes = git(p, "stash", "list", "--format=%H %gs").splitlines()
    return {"project": proj, "branch": branch, "head": head[:12], "head_full": head,
            "dirty": len(dirty), "dirty_files": [l[3:] for l in dirty][:30], "stashes": len(stashes),
            "subject": git(p, "log", "-1", "--format=%s")[:100]}


# ------------------------------------------------------------------ ops --
def op_status(req):
    path = req["path"]
    lease = read_lease(path)
    jobs_dir = os.path.join(path, ".bsp", "jobs")
    jobs = sorted(os.listdir(jobs_dir))[-5:] if os.path.isdir(jobs_dir) else []
    return {"path": path, "lease": public(lease) if lease and not lease.get("expired") else None,
            "projects": [proj_state(path, p) for p in req.get("projects", [])],
            "recent_jobs": [job_view(path, j) for j in jobs]}


def _check_holder(lease, token, what):
    if lease and not lease.get("expired") and lease.get("token") != token:
        raise Fail("conflict", "workspace is leased by {} for task '{}' via sdk {} ({}s left)".format(
            lease["holder"], lease.get("task"), lease.get("sdk", "?"), int(lease["expires"] - time.time())),
            hint="every SDK target in this workspace shares the lease: wait, ask {} to release it, "
                 "or use an SDK in another workspace".format(lease["holder"]),
            details={"what": what, "holder": lease["holder"], "task": lease.get("task"), "sdk": lease.get("sdk")})


def _exclude_state_dir(path, proj):
    """Keep .bsp/ out of `git status` when the workspace root is itself a repo."""
    p = os.path.join(path, proj)
    if os.path.realpath(p) != os.path.realpath(path):
        return
    info = os.path.join(p, ".git", "info")
    if not os.path.isdir(info):
        return
    ex = os.path.join(info, "exclude")
    lines = open(ex).read().splitlines() if os.path.exists(ex) else []
    if "/.bsp/" not in lines:
        with open(ex, "a") as f:
            f.write("/.bsp/\n")


def op_acquire(req):
    path, task, holder = req["path"], req["task"], req.get("holder") or "anonymous"
    if not task or any(c in task for c in " ~^:?*[\\"):
        raise Fail("invalid_request", "task name must be a simple branch-safe word, e.g. fix-usb-resume")
    lease = read_lease(path)
    _check_holder(lease, req.get("token"), "acquire")
    branch = req.get("branch") or "bsp/{}".format(task)
    renewing = lease and not lease.get("expired") and lease.get("token") == req.get("token")
    if renewing and lease.get("task") != task:
        raise Fail("conflict", "you already hold this SDK for task '{}'".format(lease["task"]),
                   hint="release it first (restores the stashed work) or keep using that task")
    projects = dict(lease["projects"]) if renewing else {}
    for proj in req.get("projects", []):
        p = os.path.join(path, proj)
        _exclude_state_dir(path, proj)
        if proj in projects:
            continue
        st = proj_state(path, proj)
        rec = {"orig_branch": st["branch"], "orig_head": st["head_full"], "stash": None}
        if st["dirty"]:
            git(p, "stash", "push", "-m", "bsp:{}:{} (before task)".format(task, holder))
            rec["stash"] = git(p, "rev-parse", "stash@{0}")
        rc, _ = sh(["git", "rev-parse", "--verify", "--quiet", branch], cwd=p, check=False)
        if rc == 0:
            git(p, "checkout", branch)
            rec["resumed_branch"] = True
        else:
            git(p, "checkout", "-b", branch)
        projects[proj] = rec
    new = {"holder": holder, "task": task, "branch": branch, "projects": projects,
           "sdk": (lease.get("sdk") if renewing else None) or req.get("sdk"), "workspace": req.get("workspace"),
           "token": lease["token"] if renewing else uuid.uuid4().hex[:16],
           "acquired": lease["acquired"] if renewing else time.time(),
           "expires": time.time() + int(req.get("ttl") or DEFAULT_TTL)}
    write_lease(path, new)
    out = public(new)
    out["token"] = new["token"]
    out["state"] = [proj_state(path, p) for p in req.get("projects", [])]
    return out


def op_release(req):
    path = req["path"]
    lease = read_lease(path)
    if not lease:
        return {"released": False, "message": "SDK was not leased"}
    if lease.get("token") != req.get("token") and not req.get("force") and not lease.get("expired"):
        raise Fail("forbidden", "SDK is leased by {}; token does not match".format(lease["holder"]),
                   hint="only the holder can release it (or pass force=true as an operator)")
    report = {}
    for proj, rec in lease.get("projects", {}).items():
        p = os.path.join(path, proj)
        r = {"branch": lease["branch"]}
        if req.get("restore", True):
            st = proj_state(path, proj)
            if st["dirty"]:
                git(p, "stash", "push", "-m", "bsp:{}:{} (uncommitted work at release)".format(lease["task"], lease["holder"]))
                r["wip_stash"] = git(p, "rev-parse", "stash@{0}")
            orig = rec["orig_branch"] if rec["orig_branch"] != "HEAD" else rec["orig_head"]
            git(p, "checkout", orig)
            if rec.get("stash"):
                hashes = git(p, "stash", "list", "--format=%H").splitlines()
                if rec["stash"] in hashes:
                    git(p, "stash", "pop", "stash@{{{}}}".format(hashes.index(rec["stash"])))
                    r["restored_stash"] = rec["stash"][:12]
                else:
                    r["warning"] = "stash {} no longer exists".format(rec["stash"][:12])
            r["now_on"] = orig if len(orig) < 40 else orig[:12]
        report[proj] = r
    os.remove(os.path.join(path, ".bsp", "lease.json"))
    return {"released": True, "holder": lease["holder"], "task": lease["task"], "projects": report,
            "note": "the task branch {} is kept".format(lease["branch"])}


def job_view(path, job_id, offset=0, max_bytes=65536):
    d = os.path.join(path, ".bsp", "jobs", job_id)
    if not os.path.isdir(d):
        raise Fail("not_found", "no build job {}".format(job_id))
    meta = {}
    if os.path.exists(os.path.join(d, "meta.json")):
        with open(os.path.join(d, "meta.json")) as f:
            meta = json.load(f)
    rc = None
    if os.path.exists(os.path.join(d, "rc")):
        with open(os.path.join(d, "rc")) as f:
            txt = f.read().strip()
            rc = int(txt) if txt.lstrip("-").isdigit() else -1
    log = os.path.join(d, "log")
    size = os.path.getsize(log) if os.path.exists(log) else 0
    chunk = ""
    if offset < size:
        with open(log, "rb") as f:
            f.seek(offset)
            chunk = f.read(max_bytes).decode("utf-8", "replace")
    return {"id": job_id, "status": "running" if rc is None else ("succeeded" if rc == 0 else "failed"),
            "rc": rc, "size": size, "offset": offset + len(chunk.encode("utf-8")), "chunk": chunk,
            "meta": meta, "log": log}


def op_build_start(req):
    path = req["path"]
    lease = read_lease(path)
    _check_holder(lease, req.get("token"), "build")
    job_id = "{}-{}".format(time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])
    d = os.path.join(state_dir(path), "jobs", job_id)
    os.makedirs(d)
    lock = os.path.join(path, ".bsp", "build.lock")
    script = os.path.join(d, "script")
    with open(script, "w") as f:
        f.write("#!/bin/bash\n")
        f.write("exec 9>{}\n".format(lock))
        f.write("if ! flock -n 9; then echo 'bsp: another build of this SDK is running'; exit 75; fi\n")
        for pre in req.get("path_prepend", []):
            f.write('export PATH="{}:$PATH"\n'.format(os.path.expanduser(pre)))
        for k, v in sorted((req.get("env_vars") or {}).items()):
            f.write('export {}="{}"\n'.format(k, os.path.expanduser(str(v))))
        f.write("set -o pipefail\n")
        f.write("echo \"bsp: $(date '+%F %T') start on $(hostname): {}\"\n".format(req["command"].replace('"', '\\"')))
        f.write(req["command"] + "\n")
    meta = {"component": req.get("component"), "command": req["command"], "started": time.time(),
            "holder": (lease or {}).get("holder") if lease and not lease.get("expired") else None}
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump(meta, f)
    runner = "bash {s} > {d}/log 2>&1; echo $? > {d}/rc".format(s=script, d=d)
    if shutil.which("tmux"):
        sh(["tmux", "new-session", "-d", "-s", "bsp-{}".format(job_id), runner])
        meta["attach"] = "tmux attach -t bsp-{}".format(job_id)
    else:
        subprocess.Popen(["setsid", "bash", "-c", runner], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"id": job_id, "log": os.path.join(d, "log"), "attach": meta.get("attach")}


def op_build_poll(req):
    return job_view(req["path"], req["id"], int(req.get("offset", 0)))


def op_jobs(req):
    d = os.path.join(req["path"], ".bsp", "jobs")
    ids = sorted(os.listdir(d))[-int(req.get("limit", 10)):] if os.path.isdir(d) else []
    out = []
    for j in ids:
        v = job_view(req["path"], j, offset=10 ** 12)
        v.pop("chunk", None)
        out.append(v)
    return out


def _inside(path, rel):
    full = os.path.realpath(os.path.join(path, rel))
    if not (full == os.path.realpath(path) or full.startswith(os.path.realpath(path) + os.sep)):
        raise Fail("forbidden", "{} is outside the SDK tree".format(rel))
    return full


def op_exec(req):
    """Run a shell command inside the SDK tree (cwd = path/cwd). Needs the lease if one is held."""
    lease = read_lease(req["path"])
    if req.get("mutating", True):
        _check_holder(lease, req.get("token"), "exec")
    cwd = _inside(req["path"], req.get("cwd") or ".")
    p = subprocess.Popen(["bash", "-c", req["command"]], cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True)
    try:
        out, _ = p.communicate(timeout=int(req.get("timeout_s", 300)))
    except subprocess.TimeoutExpired:
        p.kill()
        raise Fail("timeout", "command timed out after {}s".format(req.get("timeout_s", 300)))
    return {"rc": p.returncode, "output": out[-200000:], "cwd": cwd}


def op_get(req):
    full = _inside(req["path"], req["file"])
    if not os.path.isfile(full):
        raise Fail("not_found", "{} not found in the SDK tree".format(req["file"]))
    with open(full, "rb") as f:
        data = f.read()
    if len(data) > 4 * 1024 * 1024:
        raise Fail("invalid_request", "file is larger than 4 MiB")
    import base64
    return {"file": req["file"], "size": len(data), "b64": base64.b64encode(data).decode()}


def op_put(req):
    _check_holder(read_lease(req["path"]), req.get("token"), "put")
    full = _inside(req["path"], req["file"])
    import base64
    data = base64.b64decode(req["b64"])
    tmp = full + ".bsptmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if os.path.exists(full):
        os.chmod(tmp, os.stat(full).st_mode)
    os.rename(tmp, full)
    return {"file": req["file"], "size": len(data)}


OPS = {"exec": op_exec, "get": op_get, "put": op_put, "status": op_status, "acquire": op_acquire, "release": op_release,
       "build_start": op_build_start, "build_poll": op_build_poll, "jobs": op_jobs}


def main():
    try:
        req = json.loads(sys.argv[1])
        op = OPS.get(req.get("op"))
        if not op:
            raise Fail("invalid_request", "unknown op {}".format(req.get("op")))
        if not os.path.isdir(req.get("path", "")):
            raise Fail("not_found", "SDK path {} does not exist on {}".format(req.get("path"), os.uname()[1]))
        print(json.dumps({"ok": True, "result": op(req)}))
    except Fail as e:
        print(json.dumps({"ok": False, "error": e.err}))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"code": "internal_error", "message": repr(e)}}))


if __name__ == "__main__":
    main()

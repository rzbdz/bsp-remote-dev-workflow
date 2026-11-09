import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "src" / "bsp" / "sdk" / "helper.py"


def call(req):
    out = subprocess.run([sys.executable, str(HELPER), json.dumps(req)], capture_output=True, text=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def sdk(tmp_path):
    proj = tmp_path / "linux"
    proj.mkdir()
    git(proj, "init", "-q")
    git(proj, "config", "user.email", "t@t")
    git(proj, "config", "user.name", "t")
    (proj / "a.c").write_text("int a;\n")
    git(proj, "add", ".")
    git(proj, "commit", "-qm", "base")
    git(proj, "checkout", "-q", "--detach")          # repo trees are usually detached
    (proj / "a.c").write_text("int a; /* user's local work */\n")
    return tmp_path


def test_acquire_stashes_branches_and_release_restores(sdk):
    proj = sdk / "linux"
    r = call({"op": "acquire", "path": str(sdk), "projects": ["linux"], "task": "add-print", "holder": "agent"})
    assert r["ok"], r
    tok = r["result"]["token"]
    assert git(proj, "rev-parse", "--abbrev-ref", "HEAD") == "bsp/add-print"
    assert "user's local work" not in (proj / "a.c").read_text()          # stashed away
    assert r["result"]["projects"]["linux"]["stash"]

    # someone else cannot take or build it
    other = call({"op": "acquire", "path": str(sdk), "projects": ["linux"], "task": "x", "holder": "bob"})
    assert not other["ok"] and other["error"]["code"] == "conflict" and "agent" in other["error"]["message"]
    assert call({"op": "build_start", "path": str(sdk), "command": "true"})["error"]["code"] == "conflict"

    # the task edits code and builds
    (proj / "a.c").write_text("int a; /* task change */\n")
    job = call({"op": "build_start", "path": str(sdk), "command": "echo building && exit 3", "token": tok})["result"]
    for _ in range(50):
        v = call({"op": "build_poll", "path": str(sdk), "id": job["id"], "offset": 0})["result"]
        if v["status"] != "running":
            break
        time.sleep(0.1)
    assert v["status"] == "failed" and v["rc"] == 3 and "building" in v["chunk"]

    rel = call({"op": "release", "path": str(sdk), "token": tok})["result"]
    assert rel["released"] and rel["projects"]["linux"]["wip_stash"]       # task work parked, not lost
    assert "user's local work" in (proj / "a.c").read_text()               # original work back
    assert git(proj, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"        # back to detached head
    assert "bsp/add-print" in git(proj, "branch", "--list", "bsp/*")      # task branch kept
    assert call({"op": "status", "path": str(sdk), "projects": ["linux"]})["result"]["lease"] is None


def test_errors_are_diagnostic(sdk):
    r = call({"op": "acquire", "path": str(sdk / "missing"), "projects": [], "task": "t"})
    assert r["error"]["code"] == "not_found"
    r = call({"op": "acquire", "path": str(sdk), "projects": ["linux"], "task": "bad name"})
    assert r["error"]["code"] == "invalid_request"


def test_shared_workspace_lease_names_the_target(sdk):
    a = call({"op": "acquire", "path": str(sdk), "projects": ["linux"], "task": "t1", "holder": "alice",
              "sdk": "upstream-a", "workspace": "kernel-tree"})["result"]
    b = call({"op": "acquire", "path": str(sdk), "projects": ["linux"], "task": "t2", "holder": "bob",
              "sdk": "upstream-b", "workspace": "kernel-tree"})
    assert b["error"]["code"] == "conflict"
    assert "via sdk upstream-a" in b["error"]["message"] and b["error"]["details"]["holder"] == "alice"
    # the holder may build any target of the workspace with the same token
    job = call({"op": "build_start", "path": str(sdk), "command": "true", "token": a["token"]})
    assert job["ok"]

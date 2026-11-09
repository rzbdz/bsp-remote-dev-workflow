"""The registry's SDK catalog: versioned documents, conflicts, validation, distribution to hosts."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bsp.core.errors import Conflict, InvalidRequest, NotFound
from bsp.registry.catalog import CatalogStore, layer_rev, materialize
from bsp.sdk.catalog import SdkCatalog
from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]
SEED = [ROOT / "examples" / "site" / "sdk"]

RECIPE = """
- {do: enter-fdl}
- {do: stage, artifact: kernel}
- {do: continue}
- {do: wait, fastboot: "Fastboot", timeout: 300}
"""


@pytest.fixture
def store(tmp_path):
    return CatalogStore(tmp_path / "c.sqlite", SEED)


def test_site_documents_are_the_seed_and_edits_need_the_revision_they_saw(store):
    rows = {r["ref"]: r for r in store.list()}
    assert rows["bsp-a"]["origin"] == "site" and rows["server/build-a"]["rev"] == 0
    doc = store.get("bsp-a")
    edited = doc["text"].replace("example vendor BSP for soc-a", "vendor BSP, edited")
    r1 = store.put("bsp-a", edited, author="claude:a", message="describe it")
    assert r1["rev"] == 1 and store.get("bsp-a")["origin"] == "registry"
    # a second writer still holding revision 0 conflicts instead of overwriting
    with pytest.raises(Conflict) as e:
        store.put("bsp-a", doc["text"], base=0, author="claude:b")
    assert "changed since revision 0" in e.value.message and "claude:a" in e.value.message
    assert store.put("bsp-a", doc["text"], base=1, author="claude:b")["rev"] == 2
    with pytest.raises(Conflict):
        store.put("bsp-a", doc["text"], author="claude:c")                 # no base for an existing document
    assert [h["author"] for h in store.history("bsp-a")] == ["claude:b", "claude:a"]


def test_changes_that_break_the_catalog_are_refused(store):
    bad_ref = store.get("bsp-a")["text"].replace("server: build-a", "server: no-such-server")
    with pytest.raises(InvalidRequest) as e:
        store.put("bsp-a", bad_ref, base=0, author="x")
    assert "would not load" in e.value.message and store.rev == 0
    with pytest.raises(InvalidRequest):                                     # artifact role that does not exist
        store.recipe("bsp-a", "load", "- {do: stage, artifact: nope}", author="x")
    with pytest.raises(InvalidRequest):
        store.put("bsp-a", "id: other\n", base=0, author="x")               # id must match
    with pytest.raises(InvalidRequest):                                     # deleting a server SDKs use
        store.delete("server/build-a", base=0, author="x")
    assert store.rev == 0


def test_recipes_new_sdks_deletion_and_rollback(store, tmp_path):
    r = store.recipe("bsp-b", "download", RECIPE, author="claude:flash", message="flash k3 over fastboot")
    cat = SdkCatalog(SEED + [materialize(store.snapshot(), tmp_path / "layer")])
    assert cat.get("bsp-b").boot["download"].status == "ready" and cat.get("bsp-b").boot["load"].status == "todo"
    assert store.recipe("bsp-b", "download", RECIPE, author="x", check=True)["checked"]
    new = store.get("bsp-a")["text"].replace("id: bsp-a", "id: bsp-c")
    assert store.put("bsp-c", new, author="claude:new")["rev"] == r["rev"] + 1
    with pytest.raises(InvalidRequest):
        store.delete("bsp-c", author="x")                                  # needs the revision looked at
    store.delete("bsp-c", base=store.get("bsp-c")["rev"], author="x")
    with pytest.raises(NotFound):
        store.get("bsp-c")
    store.delete("bsp-b", base=store.get("bsp-b")["rev"], author="x")       # hides the site's bsp-b too
    assert "bsp-b" not in [row["ref"] for row in store.list()]
    assert SdkCatalog(SEED + [materialize(store.snapshot(), tmp_path / "l2")]).instances.names() == ["bsp-a", "upstream-a", "upstream-b"]
    store.rollback("bsp-b", r["rev"], author="x")
    assert "flash k3" not in store.get("bsp-b")["message"] and "fastboot" in store.get("bsp-b")["text"]


def test_import_takes_the_site_once(store):
    first = store.import_seed("import")
    assert "bsp-a" in first["imported"] and "family/make-bsp" in first["imported"]
    assert store.import_seed("import")["imported"] == []
    assert all(r["origin"] == "registry" for r in store.list())


def _app(tmp_path, name, text):
    p = tmp_path / name / "hosts" / f"{name}.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(text)
    for d in ("sdk", "socs", "boards", "duts"):
        src = ROOT / "examples" / "site" / d
        (p.parent.parent / d).symlink_to(src)
    return create_app(HostConfig.load(p))


def test_writes_need_no_token_and_hosts_pick_the_catalog_up(tmp_path, monkeypatch):
    import threading

    import uvicorn

    monkeypatch.setenv("BSP_TOKEN", "host-secret")
    reg_app = _app(tmp_path, "reg", "host: {id: reg}\nserver: {state_dir: state}\nmodules: {registry: {}}\n")
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(reg_app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}"
        with TestClient(reg_app) as reg:
            assert reg.post("/api/v1/registry/announce", json={"host": "h"}).status_code == 403   # hosts need the token
            text = reg.get("/api/v1/catalog/get?ref=bsp-b").json()["result"]["text"]
            r = reg.post("/api/v1/tools/catalog_recipe", json={"sdk": "bsp-b", "method": "load", "text": RECIPE,
                                                               "author": "claude:x"}).json()
            assert r["ok"] and r["result"]["rev"] == 1, r
        monkeypatch.delenv("BSP_TOKEN")
        host_app = _app(tmp_path, "h1", f"host: {{id: h1}}\nserver: {{state_dir: state, announce: {{url: '{url}', interval_s: 3600}}}}\n"
                                        "modules: {gpio_relay: {board: example-40pin, backend: mock, pinmux: false}, sdk: {}, dut: {}}\n")
        with TestClient(host_app) as host:
            assert host.get("/api/v1/sdk/boot-plan?sdk=bsp-b&method=load").json()["result"]["steps"][0]["do"] == "enter-fdl"
            st = host.get("/api/v1/status").json()["result"]
            assert st["catalog"]["rev"] == 1
            # a later change reaches the host when it syncs (the registry nudges; here we ask)
            with TestClient(reg_app) as reg:
                reg.post("/api/v1/tools/catalog_recipe", json={"sdk": "bsp-b", "method": "load", "base": 1,
                                                               "text": "{status: todo, notes: gone, steps: []}", "author": "y"})
            assert host.post("/api/v1/tools/catalog_sync", json={}).json()["result"]["rev"] == 2
            assert host.get("/api/v1/sdk/boot-plan?sdk=bsp-b&method=load").json()["error"]["code"] == "invalid_request"
            assert layer_rev(Path(st["catalog"].get("layer", tmp_path / "h1" / "hosts" / "state" / "catalog"))) == 2
    finally:
        server.should_exit = True


def test_cli_edit_round_trip_uses_the_revision_in_the_file(tmp_path, monkeypatch, capsys):
    from bsp.cli import catalogcli
    from bsp.cli.main import main

    reg_app = _app(tmp_path, "reg", "host: {id: reg}\nserver: {state_dir: state}\nmodules: {registry: {}}\n")
    with TestClient(reg_app) as reg:
        class T:                                       # the CLI's transport, served by the test client
            kind, label = "http", "test"
            def request(self, method, path, body=None, timeout=0):
                return (reg.get(path) if method == "GET" else reg.post(path, json=body)).json()
        monkeypatch.setattr("bsp.cli.fleet.connect", lambda remote, quick=2.0: type("H", (), {"call": lambda self, tool, args, timeout=0: T().request("POST", f"/api/v1/tools/{tool}", args), "id": "reg"})())
        monkeypatch.setenv("BSP_REGISTRY", "http://registry.invalid")
        monkeypatch.setenv("BSP_HOLDER", "claude:cli")
        f = tmp_path / "bsp-a.yaml"
        assert main(["catalog", "get", "bsp-a", "-o", str(f)]) == 0
        assert f.read_text().startswith("# bsp catalog: bsp-a revision 0")
        f.write_text(f.read_text().replace("example vendor BSP for soc-a", "edited by cli"))
        assert main(["catalog", "put", "bsp-a", "-f", str(f), "-m", "edit"]) == 0
        assert "written as catalog revision 1" in capsys.readouterr().out
        assert main(["catalog", "put", "bsp-a", "-f", str(f)]) == 1                    # same stale file again
        assert "changed since revision 0" in capsys.readouterr().err
        assert reg.get("/api/v1/catalog/history?ref=bsp-a").json()["result"][0]["author"] == "claude:cli"


def test_a_host_reports_site_edits_the_registry_overrides(tmp_path, store):
    """Editing the site file on a host does nothing once the registry has the document: say so, loudly."""
    import shutil

    from bsp.core.events import EventBus
    from bsp.server.catalog_follow import CatalogFollower

    site = tmp_path / "site-sdk"
    shutil.copytree(SEED[0], site)
    store.recipe("bsp-b", "download", RECIPE, author="claude:flash")
    f = CatalogFollower("http://registry.invalid", tmp_path / "layer", EventBus(), [site])
    materialize(store.snapshot(), f.layer)
    assert f.find_shadowed() == [{"ref": "bsp-b", "file": str(site / "instances" / "bsp-b.yaml"),
                                  "modified": int((site / "instances" / "bsp-b.yaml").stat().st_mtime)}]
    # once the registry has what the site says, nothing is overridden
    store.put("bsp-b", (site / "instances" / "bsp-b.yaml").read_text(), base=store.get("bsp-b")["rev"], author="x")
    materialize(store.snapshot(), f.layer)
    assert f.find_shadowed() == []


def test_git_registry_commits_writes_and_takes_in_pushed_commits(tmp_path):
    """The catalog kept in a git repository: writes become commits; commits pushed by others are validated in."""
    import shutil
    import subprocess

    from bsp.core.events import EventBus
    from bsp.registry.gitsync import GitMirror

    def git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()

    remote, site, other = tmp_path / "remote.git", tmp_path / "site", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    shutil.copytree(ROOT / "examples" / "site" / "sdk", seed / "sdk")
    for d, cfg in ((seed, None),):
        git(d.parent, "init", "-q", "-b", "main", str(d))
        git(d, "-c", "user.name=t", "-c", "user.email=t@x", "add", ".")
        git(d, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "site")
        git(d, "push", "-q", str(remote), "main")
    subprocess.run(["git", "clone", "-q", str(remote), str(site)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)

    store = CatalogStore(tmp_path / "c.sqlite", [site / "sdk"])
    bus = EventBus()
    mirror = GitMirror(store, site, {"remote": str(remote), "branch": "main", "pull_s": 3600}, bus)
    store.listeners.append(mirror.record)
    mirror.ensure()

    # a registry write is committed (author and message kept) and pushed
    store.recipe("bsp-b", "download", RECIPE, author="claude:flash", message="flash over fastboot")
    job = mirror.queue.get_nowait()
    assert mirror._commit(job)
    mirror.push()
    log = git(remote, "log", "-1", "--format=%an|%s|%b", "main")
    assert log.startswith("claude:flash|bsp-b: flash over fastboot|Catalog-Revision: 1")

    # someone edits the repository directly: the change is validated and taken into the catalog as them
    git(other, "pull", "-q")
    doc = other / "sdk" / "instances" / "bsp-a.yaml"
    doc.write_text(doc.read_text().replace("example vendor BSP for soc-a", "edited on gitlab"))
    git(other, "-c", "user.name=alice", "-c", "user.email=a@x", "commit", "-qam", "describe bsp-a")
    bad = other / "sdk" / "instances" / "upstream-a.yaml"
    bad.write_text(bad.read_text().replace("family: kernel-script", "family: no-such-family"))
    git(other, "-c", "user.name=bob", "-c", "user.email=b@x", "commit", "-qam", "break it")
    git(other, "push", "-q", "origin", "main")
    res = mirror.pull_now()
    assert [a["ref"] for a in res["applied"]] == ["bsp-a"] and res["applied"][0]["author"] == "alice"
    assert "edited on gitlab" in store.get("bsp-a")["text"] and store.get("bsp-a")["author"] == "git:alice"
    assert store.get("bsp-a")["text"] == doc.read_text()                     # exactly, final newline included
    assert [r["ref"] for r in res["rejected"]] == ["upstream-a"] and "would not load" in res["rejected"][0]["error"]
    assert "no-such-family" not in store.get("upstream-a")["text"]          # the catalog keeps what worked
    assert store.get("upstream-a")["author"] == "registry" and "revert git" in store.get("upstream-a")["message"]
    # the pinned version goes back into git as a revert with the reason
    commits = [mirror._commit(mirror.queue.get_nowait()) for _ in range(mirror.queue.qsize())]
    assert commits.count(True) == 1                                          # only the revert: no echo of alice's change
    mirror.push()
    assert "no-such-family" not in (site / "sdk" / "instances" / "upstream-a.yaml").read_text()
    assert "does not validate" in git(remote, "log", "-1", "--format=%s", "main")
    assert git(site, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")


def test_hosts_and_clis_share_the_registrys_soc_profiles_hooks_and_images(tmp_path, monkeypatch):
    """SoC profiles, hooks and the images they stage go from the registry's site to every host and CLI."""
    import hashlib
    import threading

    import uvicorn

    from bsp.registry.files import SharedFiles, layer_digest

    image = b"\x00handoff image\n"
    sha = hashlib.sha256(image).hexdigest()
    reg_site = tmp_path / "reg"
    (reg_site / "socs" / "soc-a").mkdir(parents=True)
    (reg_site / "socs" / "soc-a" / "soc.yaml").write_text(
        "soc: soc-a\nimages:\n  handoff: {file: handoff.bin, sha256: %s}\n"
        "media_boot:\n  name: shared-handoff\n  steps:\n    - {do: stage, image: handoff}\n    - {do: continue}\n" % sha)
    (reg_site / ".blobs" / sha).mkdir(parents=True)
    (reg_site / ".blobs" / sha / "handoff.bin").write_bytes(image)        # fetched there by `bsp images sync`
    hook = reg_site / "hooks" / "boot" / "say"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\necho '{\"said\": true}'\n")
    hook.chmod(0o755)
    (reg_site / "socs" / "soc-a" / "probe.bin").write_bytes(b"not shared: no sha256 in the profile")
    shared = SharedFiles(reg_site)
    assert sorted(f["path"] for f in shared.manifest()["files"]) == ["hooks/boot/say", "socs/soc-a/soc.yaml"]
    monkeypatch.setenv("BSP_TOKEN", "reg-secret")
    monkeypatch.setenv("BSP_REGISTRY_TOKEN", "reg-secret")

    reg_app = _app(tmp_path, "r", "host: {id: r}\nserver: {state_dir: state}\nmodules: {registry: {site: ../../reg}}\n")
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(reg_app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    try:
        url = f"http://127.0.0.1:{port}"
        with TestClient(reg_app) as reg:                       # hooks are code: only token holders get them
            assert reg.get("/api/v1/registry/files").status_code == 403
            assert reg.get(f"/api/v1/registry/blobs/{sha}").status_code == 403
            assert reg.get("/api/v1/registry/files", headers={"Authorization": "Bearer reg-secret"}).status_code == 200
        monkeypatch.delenv("BSP_TOKEN")
        host_app = _app(tmp_path, "h2", f"host: {{id: h2}}\nserver: {{state_dir: state, announce: {{url: '{url}', interval_s: 3600}}}}\n"
                                        "modules: {gpio_relay: {board: example-40pin, backend: mock, pinmux: false}, sdk: {}, dut: {}}\n")
        with TestClient(host_app) as host:
            st = host.get("/api/v1/status").json()["result"]
            assert st["catalog"]["files_digest"] == shared.digest()
            dut = [m for m in host_app.state.hub.modules.values() if m.name == "dut"][0]
            prof = dut.socs.get("soc-a")
            assert prof["media_boot"]["name"] == "shared-handoff"                      # the registry's profile wins
            assert prof["images"]["handoff"]["path"].endswith(f"{sha}/handoff.bin")     # with its image, by hash
            assert "say" in dut.hooks.names("boot")
            # a change in the registry's site reaches the host at its next sync
            hook.write_text("#!/bin/sh\necho '{\"said\": \"again\"}'\n")
            res = host.post("/api/v1/tools/catalog_sync", json={}).json()["result"]   # (or the announcer got there first)
            assert res["files"]["ok"] and res["files_digest"] == shared.digest()
            assert dut.hooks.run("boot", "say", {}).data == {"said": "again"}
        # a compile machine's CLI gets the same files with `bsp catalog sync`
        monkeypatch.setenv("BSP_REGISTRY", url)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        from bsp.cli.catalogcli import registry_files

        layer = registry_files()
        assert layer and layer_digest(layer) == shared.digest()
        assert (layer / "socs" / "soc-a" / "soc.yaml").exists() and (layer / ".blobs" / sha / "handoff.bin").read_bytes() == image
    finally:
        server.should_exit = True

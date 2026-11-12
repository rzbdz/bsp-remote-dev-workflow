"""SDK resource catalogue (read-only, data-driven).

Layers, all YAML under <dir>/sdk/ for each catalog root (the repo library ships generic
families; a site directory adds servers, workspaces and instances — see core/site.py):
  families/    how a kind of SDK builds (make targets, envsetup shell functions, a
               script) and where its artifacts land, by role (fsbl, uboot-sbi, ...)
  servers/     build servers (ssh identity, path aliases)
  workspaces/  one checkout on one server = THE unit of mutual exclusion
               (lease, task branch, build lock, build jobs). Optional: an instance
               without `workspace:` owns an implicit one (its server + path).
  instances/   an SDK *target*: workspace + family + SoC + artifacts + boot methods.
               Several targets may share a workspace (one upstream kernel tree building
               for two SoCs): leasing one leases the tree for all.

Every instance has exactly two boot methods:
  load       fastboot RAM boot (stage + continue), nothing written to media
  download   fastboot flash to NOR/UFS/..., then cold boot from media
A method is `ready` (has steps) or `todo` (placeholder, explains what is missing).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import InvalidRequest, NotFound
from ..core.registry import Registry

BOOT_METHODS = ("load", "download")
# The kinds of catalog documents, as directory names (servers before workspaces before instances).
CATALOG_KINDS = ("families", "servers", "workspaces", "instances")
BOOT_STATUS = ("ready", "todo")
# enter-fdl  reset through the DUT control backend into BROM download mode
# wait       wait for a serial regex and/or a fastboot product regex (since the last action);
#            options: fail (regex), timeout, stall (s of silence = failure), nudge (s of silence ->
#            press Enter, for prompts printed without a newline; never while a bootloader may
#            take the key as "stop autoboot")
# stage / continue / oem / flash / getvar   fastboot verbs on the DUT's USB path
# settle     fixed hardware delay (documented `why`), the only sanctioned sleep
# reset      plain reset (no FDL expectation)
# hook       run hooks/<kind>/<name> with the DUT + artifact paths (flash mechanisms plug in here)
# check      the console since the last action matches a regex, else stop (what board is this?)
# boot-media boot the board's own media: a plain reset, or the SoC's media boot (dut/mediaboot.py)
# `when: fdl-not-strapped` marks a step that needs the board to boot its media (the cold boot after a flash):
# on a board whose FDL is strapped it runs through the media boot, or is skipped when there is none.
STEP_KINDS = ("enter-fdl", "reset", "wait", "stage", "continue", "oem", "flash", "getvar", "settle", "hook",
              "check", "boot-media")


def _load_yaml(p: Path) -> dict[str, Any]:
    import yaml

    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    if not isinstance(d, dict) or "id" not in d:
        raise InvalidRequest(f"{p}: expected a mapping with an `id`")
    return d


@dataclass
class Family:
    id: str
    runner: str
    targets: dict[str, str]
    clean: dict[str, str]
    artifact_dir: str
    roles: dict[str, str]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class Server:
    id: str
    host: str
    user: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def ssh(self) -> str:
        return f"{self.user}@{self.host}"


@dataclass
class Workspace:
    id: str
    server: Server
    path: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    implicit: bool = False


@dataclass
class BootMethod:
    name: str
    status: str
    steps: list[dict[str, Any]]
    notes: str = ""
    variants: dict[str, str] = field(default_factory=dict)


@dataclass
class Instance:
    id: str
    family: Family
    workspace: Workspace
    boot: dict[str, BootMethod]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def server(self) -> Server:
        return self.workspace.server

    @property
    def path(self) -> str:
        return self.workspace.path

    @property
    def soc(self) -> str | None:
        return self.raw.get("soc") or self.raw.get("chip")

    # ------------------------------------------------------------ build --
    def targets(self) -> dict[str, str]:
        return {**self.family.targets, **(self.raw.get("build", {}).get("targets") or {})}

    def build_command(self, component: str, clean: bool = False) -> str:
        """Shell command (run on the build server) for one component; nothing is executed."""
        table = self.family.clean if clean else self.targets()
        if component not in table:
            raise InvalidRequest(
                f"{self.id}: no {'clean ' if clean else ''}target '{component}'",
                hint=f"known: {', '.join(sorted(table))}",
                details={"targets": sorted(table)},
            )
        target = table[component]
        pre = None if clean else (self.raw.get("build", {}).get("pre") or {}).get(component)
        path = shlex.quote(self.path)
        if self.family.runner == "make":
            steps = [f"cd {path}"]
            if pre:
                steps += [f"( {pre} )", f"cd {path}"]
            steps.append(f"make {target}")
            return " && ".join(steps)
        if self.family.runner == "script":
            # target is a shell command run from the workspace root (e.g. ./build.sh -b {board})
            cmd = target.format(**{k: v for k, v in self.raw.items() if isinstance(v, str)})
            steps = [f"cd {path}"] + ([f"( {pre} )"] if pre else []) + [cmd]
            return " && ".join(steps)
        if self.family.runner == "envsetup":
            plat = self.raw.get("plat")
            if not plat:
                raise InvalidRequest(f"{self.id}: envsetup family needs `plat` in the instance config")
            env = self.family.raw["build"]["envsetup"]
            inner = f"cd {path} && export TOPDIR=$PWD && source {env} {shlex.quote(plat)} >/dev/null && {target}"
            if pre:
                inner = f"( {pre} ) && {inner}"
            return f"bash -c {shlex.quote(inner)}"
        raise InvalidRequest(f"family {self.family.id}: unknown runner {self.family.runner!r}")

    def setting(self, key: str, default: Any = None) -> Any:
        """Workspace value, else instance value, else family value, else default."""
        if key in self.workspace.raw and not self.workspace.implicit:
            return self.workspace.raw[key]
        if key in self.raw:
            return self.raw[key]
        return self.family.raw.get(key, default)

    @property
    def dev_projects(self) -> list[str]:
        return list(self.setting("dev_projects", []) or [])

    @property
    def path_prepend(self) -> list[str]:
        server = (self.server.raw.get("env") or {}).get("path_prepend") or []
        return list((self.setting("env", {}) or {}).get("path_prepend") or []) + list(server)

    @property
    def env_vars(self) -> dict[str, str]:
        """Server-wide variables, overridden by family/workspace/instance `env.vars`."""
        return {**((self.server.raw.get("env") or {}).get("vars") or {}),
                **((self.setting("env", {}) or {}).get("vars") or {})}

    @property
    def prompt(self) -> str:
        return (self.setting("console", {}) or {}).get("prompt") or "# $"

    # -------------------------------------------------------- artifacts --
    def artifact_dir(self) -> str:
        d = (self.raw.get("artifacts_dir") or self.family.artifact_dir).format(plat=self.raw.get("plat", ""))
        return f"{self.path.rstrip('/')}/{d}"

    def artifacts(self) -> dict[str, str]:
        roles = {**self.family.roles, **(self.raw.get("artifacts") or {})}
        return {r: f"{self.artifact_dir()}/{f.format(plat=self.raw.get('plat', ''))}" for r, f in roles.items()}

    # ------------------------------------------------------------- boot --
    def breakers(self, method: str) -> list[str]:
        """Where a recipe can stop early (`breaker: NAME` on a step), in order: for debugging that stage on its own."""
        return [str(s["breaker"]) for s in self.boot[method].steps if s.get("breaker")]

    def boot_plan(self, method: str, variant: str | None = None, brk: str | None = None) -> dict[str, Any]:
        if method not in BOOT_METHODS:
            raise InvalidRequest(f"boot method must be one of {list(BOOT_METHODS)}, got {method!r}",
                                 hint="load = fastboot RAM boot; download = flash to media then cold boot")
        bm = self.boot[method]
        if bm.status != "ready":
            raise InvalidRequest(
                f"{self.id}: boot method '{method}' is not implemented yet (status {bm.status})",
                hint=bm.notes or f"fill in boot.{method}.steps in <site>/sdk/instances/{self.id}.yaml",
                details={"available": {m: b.status for m, b in self.boot.items()}},
            )
        if variant and variant not in bm.variants:
            raise InvalidRequest(f"{self.id}/{method}: unknown variant {variant!r}",
                                 hint=f"variants: {', '.join(bm.variants) or '(none)'}")
        if brk and brk not in self.breakers(method):
            raise InvalidRequest(f"{self.id}/{method}: no break point {brk!r}",
                                 hint=f"break points: {', '.join(self.breakers(method)) or '(none — put `breaker: NAME` on a step)'}")
        arts = self.artifacts()
        steps = []
        for s in bm.steps:
            s = dict(s)
            if variant and s.get("stop_if") == variant:
                break
            # A variant can also swap steps: `only_if: <variant>` runs only in it, `unless: <variant>` only outside it.
            if "only_if" in s and s["only_if"] != variant:
                continue
            if "unless" in s and s["unless"] == variant:
                continue
            for key in ("stop_if", "only_if", "unless"):
                s.pop(key, None)
            if "artifact" in s:
                s["file"] = arts.get(s["artifact"])
            steps.append(s)
            # A break point: the run stops once this step is done (debug U-Boot alone, say).
            if brk and s.get("breaker") == brk:
                break
            s.pop("breaker", None)
        for s in steps:
            s.pop("breaker", None)
        return {"sdk": self.id, "method": method, "variant": variant, "break": brk, "steps": steps}

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "family": self.family.id, "server": self.server.id, "ssh": self.server.ssh,
            "workspace": self.workspace.id, "soc": self.soc,
            "path": self.path, "chip": self.raw.get("chip"), "board": self.raw.get("board"),
            "description": self.raw.get("description", ""),
            "components": sorted(self.targets()),
            "boot": {m: b.status for m, b in self.boot.items()},
        }


class SdkCatalog:
    def __init__(self, roots: "Path | list[Path]"):
        """`roots`: one dir or several, later ones override earlier ones (library, then site)."""
        self.roots = [Path(r) for r in (roots if isinstance(roots, (list, tuple)) else [roots])]
        self.root = self.roots[-1] if self.roots else Path(".")
        self.families: Registry[Family] = Registry("sdk family")
        self.servers: Registry[Server] = Registry("build server")
        self.instances: Registry[Instance] = Registry("sdk instance")
        self.workspaces: Registry[Workspace] = Registry("sdk workspace")

        # Later roots override earlier ones by id; a root's `removed.json` ({"instances": [ids], ...}) takes ids
        # out of the roots before it (a document deleted in the registry hides the site's copy).
        layered: dict[str, dict[str, tuple[Path, dict[str, Any]]]] = {k: {} for k in CATALOG_KINDS}
        for r in self.roots:
            removed = r / "removed.json"
            if removed.exists():
                import json

                for kind, ids in (json.loads(removed.read_text()) or {}).items():
                    for i in ids:
                        layered.get(kind, {}).pop(i, None)
            for kind in CATALOG_KINDS:
                for p in sorted((r / kind).glob("*.yaml")):
                    d = _load_yaml(p)
                    layered[kind][d["id"]] = (p, d)

        def docs(kind: str) -> list[tuple[Path, dict[str, Any]]]:
            return list(layered[kind].values())

        for p, d in docs("families"):
            b, a = d.get("build") or {}, d.get("artifacts") or {}
            self.families.register(d["id"], Family(d["id"], b.get("runner", ""), b.get("targets") or {},
                                                   b.get("clean") or {}, a.get("dir", "output"), a.get("roles") or {}, d))
        for p, d in docs("servers"):
            if "host" not in d or "user" not in d:
                raise InvalidRequest(f"{p}: a build server needs `host` and `user`")
            self.servers.register(d["id"], Server(d["id"], d["host"], d["user"], d))
        for p, d in docs("workspaces"):
            self.workspaces.register(d["id"], Workspace(d["id"], self.servers.get(d["server"]), d["path"], d))
        for p, d in docs("instances"):
            self.instances.register(d["id"], self._instance(d, p))

    def _instance(self, d: dict[str, Any], p: Path) -> Instance:
        boot_raw = d.get("boot") or {}
        missing = [m for m in BOOT_METHODS if m not in boot_raw]
        if missing:
            raise InvalidRequest(f"{p}: boot section must declare {list(BOOT_METHODS)} (missing {missing})",
                                 hint="use `status: todo` with `steps: []` as a placeholder")
        boot = {}
        for m in BOOT_METHODS:
            b = boot_raw[m] or {}
            status, steps = b.get("status", "todo"), b.get("steps") or []
            if status not in BOOT_STATUS:
                raise InvalidRequest(f"{p}: boot.{m}.status must be one of {list(BOOT_STATUS)}")
            if status == "ready" and not steps:
                raise InvalidRequest(f"{p}: boot.{m} is `ready` but has no steps")
            for s in steps:
                if s.get("do") not in STEP_KINDS:
                    raise InvalidRequest(f"{p}: boot.{m}: unknown step {s.get('do')!r}", details={"kinds": STEP_KINDS})
            boot[m] = BootMethod(m, status, steps, b.get("notes", ""), b.get("variants") or {})
        if d.get("workspace"):
            ws = self.workspaces.get(d["workspace"])
        else:
            if "server" not in d or "path" not in d:
                raise InvalidRequest(f"{p}: give either `workspace:` or `server:` + `path:`")
            ws = Workspace(d["id"], self.servers.get(d["server"]), d["path"], {}, implicit=True)
            if d["id"] in self.workspaces and not self.workspaces.get(d["id"]).implicit:
                raise InvalidRequest(f"{p}: implicit workspace {d['id']!r} clashes with a declared workspace")
            self.workspaces.register(d["id"], ws)
        inst = Instance(d["id"], self.families.get(d["family"]), ws, boot, d)
        roles = inst.artifacts()
        for m in boot.values():
            for s in m.steps:
                for r in s.get("artifacts", []):
                    if r not in roles:
                        raise InvalidRequest(f"{p}: boot.{m.name} hook references unknown artifact role {r!r}",
                                             details={"roles": sorted(roles)})
                if "artifact" in s and s["artifact"] not in roles:
                    raise InvalidRequest(f"{p}: boot.{m.name} references unknown artifact role {s['artifact']!r}",
                                         details={"roles": sorted(roles)})
        return inst

    def get(self, sdk_id: str) -> Instance:
        try:
            return self.instances.get(sdk_id)
        except NotFound:
            import difflib

            close = difflib.get_close_matches(sdk_id, self.instances.names(), n=3, cutoff=0.4)
            raise NotFound(f"unknown sdk {sdk_id!r}",
                           hint=(f"did you mean: {', '.join(close)}? " if close else "")
                           + f"known: {', '.join(self.instances.names())}") from None


def sharing(cat: "SdkCatalog", ws_id: str) -> list[str]:
    """SDK instances that build in the same workspace."""
    return sorted(i for i, inst in cat.instances.items() if inst.workspace.id == ws_id)

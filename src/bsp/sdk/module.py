"""SDK module (phase 0: read-only catalogue + dry-run plans; no remote execution yet)."""

from __future__ import annotations

from typing import Any

from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command
from ..core.module import MODULES, Module, ModuleContext
from .catalog import BOOT_METHODS, SdkCatalog


def _render_list(items: list[dict[str, Any]]) -> str:
    rows = [("SDK", "FAMILY", "SERVER", "LOAD", "DOWNLOAD", "PATH")]
    for s in items:
        rows.append((s["id"], s["family"], s["server"], s["boot"]["load"], s["boot"]["download"], s["path"]))
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def _render_plan(p: dict[str, Any]) -> str:
    head = f"{p['sdk']} boot {p['method']}" + (f" ({p['variant']})" if p.get("variant") else "")
    lines = [head]
    for i, s in enumerate(p["steps"], 1):
        extra = {k: v for k, v in s.items() if k not in ("do", "artifact")}
        lines.append(f"  {i:>2}. {s['do']:<14} " + " ".join(f"{k}={v}" for k, v in extra.items()))
    return "\n".join(lines)


@MODULES.register("sdk")
class SdkModule(Module):
    name = "sdk"
    title = "SDK catalogue"

    def __init__(self, ctx: ModuleContext, config: dict[str, Any]):
        super().__init__(ctx, config)
        from ..core.site import layered

        self._site = (ctx.config_dir / config.get("site", "..")).resolve()
        self._roots = layered("sdk", self._site)
        self._layer = ctx.state_dir / "catalog"            # the registry's catalog, when this host follows one
        self.catalog = self._load()
        ctx.bus.on("catalog.updated", lambda ev: self._reload())
        ctx.bus.on("site.updated", lambda ev: self._reload(reread_site=True))

    def _load(self) -> SdkCatalog:
        return SdkCatalog(self._roots + ([self._layer] if self._layer.is_dir() else []))

    def _reload(self, reread_site: bool = False) -> None:
        try:
            if reread_site:                                 # the site's sdk/ may be new since start
                from ..core.site import layered

                self._roots = layered("sdk", self._site)
            self.catalog = self._load()
        except Exception:  # noqa: BLE001 - keep the catalog that loads
            pass

    def commands(self) -> list[Command]:
        def c_():
            return self.catalog
        sdk = Param("sdk", help=f"sdk instance id ({', '.join(c_().instances.names())})")
        return [
            Command(CommandSpec("sdk.list", "List SDK instances, their build server and boot-method status",
                                mutating=False), lambda: [i.summary() for _, i in sorted(c_().instances.items())],
                    _render_list),
            Command(CommandSpec("sdk.show", "One SDK: components, artifact paths by role, boot methods",
                                [sdk], mutating=False),
                    lambda sdk: {**c_().get(sdk).summary(), "artifacts": c_().get(sdk).artifacts(),
                                 "boot_notes": {m: b.notes for m, b in c_().get(sdk).boot.items()}}),
            Command(CommandSpec("sdk.build-plan", "Dry run: the exact command a build would run on the build server",
                                [sdk, Param("component", help="e.g. kernel, uboot, linux"),
                                 Param("clean", "boolean", "clean instead of build", required=False, default=False)],
                                mutating=False),
                    lambda sdk, component, clean: {"sdk": sdk, "server": c_().get(sdk).server.ssh,
                                                   "command": c_().get(sdk).build_command(component, clean)},
                    lambda r: f"ssh {r['server']}\n  {r['command']}"),
            Command(CommandSpec("sdk.boot-plan", "Dry run: boot steps for load (RAM) or download (flash) boot",
                                [sdk, Param("method", help="load = fastboot RAM boot; download = flash then boot",
                                            choices=list(BOOT_METHODS)),
                                 Param("variant", help="e.g. uboot-only", required=False)],
                                mutating=False),
                    lambda sdk, method, variant: c_().get(sdk).boot_plan(method, variant), _render_plan),
        ]

    def status(self) -> dict[str, Any]:
        return {"instances": len(self.catalog.instances.names())}

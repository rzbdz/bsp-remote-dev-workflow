"""FastAPI application factory.

Every module command is exposed four ways from one CommandSpec:
  REST     POST /api/v1/<group>/<target>/<verb>   (GET too for read-only commands)
  AI tool  GET /api/v1/tools  +  POST /api/v1/tools/<tool_name>
  CLI      `bsp <group> <verb> ...` (thin client builds argparse from /api/v1/tools)
  console  POST /api/v1/cli {"line": "gpio set 33 low"} (web remote CLI)
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import inspect
import json
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Body, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, create_model

from .. import __version__
from ..core.cmdspec import CommandSpec, Param
from ..core.commands import Command, CommandRegistry
from ..core.errors import Forbidden, BspError
from ..core.events import EventBus
from ..core.jobs import JobManager
from ..core.module import MODULES, Module, ModuleContext
from .config import HostConfig

BUILTIN_MODULES = ["bsp.gpio_relay.module", "bsp.sdk.module", "bsp.dut.module", "bsp.registry.module"]
WEB_DIR = Path(__file__).parent / "web"
_PYTYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


class CliLine(BaseModel):
    line: str = Field(..., examples=["gpio set 33 low"])


class Hub:
    """Runtime container: config, bus, loaded modules and the command registry."""

    def __init__(self, config: HostConfig):
        for m in BUILTIN_MODULES:
            importlib.import_module(m)
        self.config = config
        self.bus = EventBus()
        self.jobs = JobManager(self.bus)
        self.started = time.time()
        config.state_dir.mkdir(parents=True, exist_ok=True)
        ctx = ModuleContext(config.host_id, self.bus, config.state_dir, config.config_dir, jobs=self.jobs,
                            port=config.port, auth_token=config.auth_token)
        self.ctx = ctx
        self.modules: dict[str, Module] = {
            name: MODULES.get(name)(ctx, mcfg) for name, mcfg in config.modules.items() if mcfg.get("enabled", True)
        }
        # A host announcing itself to a registry follows the registry's SDK catalog.
        self.follower = None
        if config.announce:
            from .catalog_follow import CatalogFollower

            from ..core.site import layered

            site = (config.config_dir / "..").resolve()
            import os

            self.follower = CatalogFollower(config.announce["url"], config.state_dir / "catalog", self.bus,
                                            layered("sdk", site),
                                            config.announce.get("token") or os.environ.get("BSP_REGISTRY_TOKEN"))
        if self.follower:
            ctx.services["catalog_follower"] = self.follower
        self.registry = CommandRegistry()
        self.registry.extend(self._core_commands())
        for m in self.modules.values():
            self.registry.extend(m.commands())

    announcer: Any = None

    def announcement(self) -> dict[str, Any]:
        dut = self.modules.get("dut")
        return {"host": self.config.host_id, "port": self.config.port, "description": self.config.description,
                "version": __version__, "duts": sorted(getattr(dut, "boards", {}) or {}),
                # compact, so a registry notices a slot coming online or a board registered into it
                "slots": [f"{s['id']}:{'on' if s['online'] else 'off'}:{s.get('dut') or ''}" for s in dut.slot_list()]
                if dut and hasattr(dut, "slot_list") else [],
                "ports": dut.ports() if dut else {}, "catalog_rev": self.follower.rev if self.follower else 0,
                "files_digest": self.follower.files_digest if self.follower else "",
                "catalog_shadowed": self.follower.shadowed if self.follower else []}

    def status(self) -> dict[str, Any]:
        return {
            "host": self.config.host_id,
            "description": self.config.description,
            "hostname": socket.gethostname(),
            "version": __version__,
            "uptime_s": round(time.time() - self.started, 1),
            "auth": bool(self.config.auth_token),
            "modules": {n: {"title": m.title, **m.status()} for n, m in self.modules.items()},
            **({"announce": {"url": self.announcer.url, **self.announcer.last}} if self.announcer else {}),
            **({"catalog": {"rev": self.follower.rev, **self.follower.last, "shadowed": self.follower.shadowed}}
               if self.follower else {}),
        }

    def _core_commands(self) -> list[Command]:
        def render_status(s: dict[str, Any]) -> str:
            lines = [f"{s['host']} ({s['hostname']}) bsp {s['version']} up {s['uptime_s']}s"]
            for n, m in s["modules"].items():
                be = m.get("backend") or {}
                lines.append(f"  module {n}: {m.get('title')} board={m.get('board')} backend={be.get('name')}"
                             f" degraded={be.get('degraded')} pinmux={(m.get('pinmux') or {}).get('state')}")
            return "\n".join(lines)

        def render_events(evs: list[dict[str, Any]]) -> str:
            return "\n".join(
                f"#{e['seq']} {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['topic']} "
                + " ".join(f"{k}={v}" for k, v in e["data"].items() if k not in ("error",) and v is not None)
                + (f" error={e['data']['error'].get('message')}" if e["data"].get("error") else "")
                for e in evs
            ) or "(no events)"

        follow = []
        if self.follower:
            def shown(r: dict[str, Any]) -> str:
                text = f"catalog revision {r.get('rev')}" + ("" if r.get("ok") else f" (registry unreachable: {r.get('error')})")
                for x in r.get("shadowed") or []:
                    text += f"\nNOT IN EFFECT: {x['file']} differs from the registry's {x['ref']}; publish it: bsp catalog put {x['ref']} -f <file>"
                return text

            follow = [Command(CommandSpec("catalog.sync", "Fetch the registry's SDK catalog now (the registry asks after changes)",
                                          [Param("reset", "boolean", "throw the local copy away and fetch it again",
                                                 required=False, default=False)]),
                              lambda reset: self.follower.sync(reset), shown)]
        return follow + [
            Command(CommandSpec("status", "Service, module and backend health", [], mutating=False),
                    self.status, render_status),
            Command(CommandSpec("job.get", "Status, progress lines and result of a background job (boot, flash)",
                                [Param("id", help="job id"),
                                 Param("since", "integer", "only lines after this index", required=False, default=0),
                                 Param("wait", "number", "block up to N seconds while it runs", required=False, default=0)],
                                mutating=False),
                    lambda id, since, wait: (self.jobs.wait(id, wait) if wait else self.jobs.get(id)).view(since),
                    _render_job),
            Command(CommandSpec("job.list", "Recent background jobs", [], mutating=False),
                    lambda: [{k: v for k, v in j.view().items() if k != "lines"} for j in self.jobs.list()],
                    lambda js: "\n".join(f"{j['id']:<28} {j['status']:<10} "
                                         f"{str(j['progress']) + '%' if j.get('progress') is not None else '':>4} "
                                         f"{j['elapsed_s']:>7}s  {j['title']}" for j in js) or "(no jobs)"),
            Command(CommandSpec("events.recent", "Recent events (status monitor history)",
                                [Param("limit", "integer", "max events", required=False, default=30)], mutating=False),
                    lambda limit: [e.to_dict() for e in self.bus.recent(limit)], render_events),
        ]


def _render_job(j: dict[str, Any]) -> str:
    pct = f" {j['progress']}%" if j.get("progress") is not None else ""
    head = f"job {j['id']} [{j['status']}{pct}] {j['title']} ({j['elapsed_s']}s)"
    body = "\n".join(j["lines"])
    tail = ""
    if j.get("error"):
        tail = f"\nerror[{j['error'].get('code')}]: {j['error'].get('message')}" + (
            f"\nhint: {j['error']['hint']}" if j["error"].get("hint") else "")
    return "\n".join(x for x in (head, body) if x) + tail


# ====================================================================== app --
def create_app(config: HostConfig) -> FastAPI:
    hub = Hub(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for m in hub.modules.values():
            await run_in_threadpool(m.startup)
        hub.bus.publish("service.started", version=__version__)
        announcer = None
        if config.announce:
            from .announce import Announcer

            announcer = Announcer(config.announce, hub.announcement)
            hub.announcer = announcer
            if hub.follower:
                announcer.on_reply = lambda result: hub.follower.check(result.get("catalog_rev"), result.get("files_digest"))
                # tell the registry at once which catalog revision this host now has
                hub.bus.on("catalog.updated", lambda ev: announcer.poke())
                await run_in_threadpool(hub.follower.sync)
            hub.bus.on("dut.ports", lambda ev: announcer.poke())     # tell the registry about new cables at once
            # ... and about boards and slots, so front ends (boardlab) list a board the moment it is registered
            for topic in ("dut.added", "dut.removed", "dut.renamed", "dut.updated", "dut.refreshed", "dut.slots"):
                hub.bus.on(topic, lambda ev: announcer.poke())
            announcer.start()
        yield
        if announcer:
            announcer.stop()
        for m in hub.modules.values():
            m.shutdown()

    app = FastAPI(
        title=f"bsp hostd · {config.host_id}",
        version=__version__,
        description="bsp dev-loop host controller. AI agents: start at GET /llms.txt or GET /api/v1/tools.",
        lifespan=lifespan,
    )
    app.state.hub = hub

    # ---- every error is a self-explaining envelope: what failed, why, and what to do next
    def envelope(request: Request, status: int, err: dict[str, Any]) -> JSONResponse:
        err.setdefault("request", f"{request.method} {request.url.path}")
        err.setdefault("docs", "/llms.txt")
        return JSONResponse({"ok": False, "error": err}, status_code=status)

    @app.exception_handler(BspError)
    async def _bsp_error(request: Request, exc: BspError):
        return envelope(request, exc.status, exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        problems = [f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}" for e in exc.errors()]
        return envelope(request, 400, {
            "code": "invalid_request", "message": "; ".join(problems),
            "hint": "send arguments as a JSON object body (Content-Type: application/json) or as query parameters; "
                    "schemas: GET /api/v1/tools",
            "details": exc.errors(),
        })

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            templates = sorted({r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")})
            close = difflib.get_close_matches(request.url.path, templates, n=5, cutoff=0.5)
            return envelope(request, 404, {
                "code": "not_found", "message": f"no route {request.url.path}",
                "hint": (f"similar routes: {', '.join(close)}" if close else "routes follow /api/v1/<group>/<target>/<verb>")
                        + "; full list: GET /api/v1/tools or /docs",
                "details": {"did_you_mean": close},
            })
        if exc.status_code == 405:
            allowed = exc.headers.get("allow", "") if exc.headers else ""
            return envelope(request, 405, {
                "code": "method_not_allowed", "message": f"{request.method} not allowed on {request.url.path}",
                "hint": f"use {allowed or 'POST'} (mutating commands are POST-only; read-only commands accept GET and POST)",
            })
        return envelope(request, exc.status_code, {"code": f"http_{exc.status_code}", "message": str(exc.detail)})

    if config.auth_token:
        open_paths = tuple(p for m in hub.modules.values() for p in m.open_paths())

        @app.middleware("http")
        async def _auth(request: Request, call_next):
            if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") \
                    and not request.url.path.startswith(open_paths or ("\0",)):
                tok = request.headers.get("x-bsp-token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                if tok != config.auth_token:
                    e = Forbidden("missing or wrong token", hint="send 'Authorization: Bearer <token>' or 'X-BSP-Token: <token>'; "
                                  "the token is server.auth_token in the host config (or env BSP_TOKEN)")
                    return envelope(request, e.status, e.to_dict())
            return await call_next(request)

    _core_routes(app, hub)
    for spec in hub.registry.specs():
        _command_routes(app, hub, spec)
    for m in hub.modules.values():
        r = m.router()
        if r is not None:
            app.include_router(r)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # cache-busting: asset URLs carry a hash of the web files, so a redeploy is picked up without Ctrl+F5
    web_version = hashlib.sha1(b"".join(f.read_bytes() for f in sorted(WEB_DIR.iterdir()) if f.is_file())).hexdigest()[:10]
    index_html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__V__", web_version)

    @app.get("/", include_in_schema=False)
    async def index():
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    return app


def _core_routes(app: FastAPI, hub: Hub) -> None:
    @app.get("/api/v1/health", tags=["core"])
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/v1/tools", tags=["core"], summary="Command catalogue / AI tool definitions")
    async def tools(format: Literal["bsp", "anthropic", "openai"] = "bsp"):
        specs = hub.registry.specs()
        if format == "anthropic":
            return [{"name": s.tool_name, "description": _tool_desc(s), "input_schema": s.input_schema()} for s in specs]
        if format == "openai":
            return [{"type": "function", "function": {"name": s.tool_name, "description": _tool_desc(s),
                                                      "parameters": s.input_schema()}} for s in specs]
        return {"ok": True, "tools": [s.to_dict() for s in specs]}

    @app.post("/api/v1/tools/{name}", tags=["core"], summary="Execute a tool / command by name with JSON args")
    async def call_tool(name: str, args: dict[str, Any] = Body(default_factory=dict)):
        result, text = await run_in_threadpool(hub.registry.execute, name, args)
        return {"ok": True, "command": hub.registry.get(name).spec.name, "result": result, "text": text}

    @app.post("/api/v1/cli", tags=["core"], summary="Remote CLI: run one `bsp ...` command line")
    async def cli(body: CliLine):
        code, output, result = await run_in_threadpool(hub.registry.run_line, body.line)
        return {"ok": code == 0, "exit_code": code, "output": output, "result": result}

    @app.get("/api/v1/events", tags=["core"], summary="Server-sent event stream (status monitor)")
    async def events(request: Request, since: Optional[int] = None):
        async def gen():
            yield "retry: 2000\n\n"
            with hub.bus.subscribe(since) as sub:
                while not await request.is_disconnected():
                    ev = await sub.get(timeout=15)
                    if ev is None:
                        yield ": ping\n\n"
                        continue
                    # no `event:` field: topic is in the payload, so EventSource.onmessage sees everything
                    yield f"id: {ev.seq}\ndata: {json.dumps(ev.to_dict(), default=str)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
    async def llms(request: Request):
        return _llms_txt(hub, str(request.base_url).rstrip("/"))


def _tool_desc(s: CommandSpec) -> str:
    return f"{s.summary}. {s.description}".strip() if s.description else s.summary


def _command_routes(app: FastAPI, hub: Hub, spec: CommandSpec) -> None:
    body_params = [p for p in spec.params if p.name != "target"]
    model = None
    if body_params:
        fields = {}
        for p in body_params:
            t = Literal[tuple(p.choices)] if p.choices else _PYTYPES[p.type]  # type: ignore[valid-type]
            fields[p.name] = (Optional[t], Field(default=None, description=p.help or None))
        model = create_model(f"{spec.tool_name}_args", **fields)

    def make(with_body: bool):
        async def endpoint(request: Request, **kw):
            args: dict[str, Any] = dict(request.query_params)
            if kw.get("body") is not None:
                args.update(kw["body"].model_dump(exclude_none=True))
            if "target" in kw:
                args["target"] = kw["target"]
            result, _ = await run_in_threadpool(hub.registry.execute, spec.name, args)
            return {"ok": True, "command": spec.name, "result": result}

        params = [inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request)]
        if spec.has_target:
            params.append(inspect.Parameter("target", inspect.Parameter.KEYWORD_ONLY, annotation=str))
        if with_body and model is not None:
            params.append(inspect.Parameter("body", inspect.Parameter.KEYWORD_ONLY,
                                            annotation=Optional[model], default=Body(None)))
        endpoint.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
        endpoint.__name__ = spec.tool_name
        return endpoint

    doc = f"{spec.description}\n\nCLI: `{spec.cli_usage}`. Args may be sent as JSON body or query string."
    tag = spec.group
    path = spec.http_path.replace("{target}", "{target:path}")  # aliases like DUT3/reset contain '/'
    if not spec.mutating:
        app.add_api_route(path, make(False), methods=["GET"], tags=[tag], summary=spec.summary,
                          description=doc, operation_id=f"{spec.tool_name}_get")
    app.add_api_route(path, make(True), methods=["POST"], tags=[tag], summary=spec.summary,
                      description=doc, operation_id=spec.tool_name)


def _llms_txt(hub: Hub, base: str) -> str:
    lines = [
        f"# bsp hostd · {hub.config.host_id}",
        "",
        f"> {hub.config.description or 'bsp dev-loop host controller'}. HTTP API, CLI and AI tools share one command set.",
        "",
        "## How to call",
        f"- Tool catalogue (JSON schema): GET {base}/api/v1/tools  (?format=anthropic|openai)",
        f"- Execute a tool: POST {base}/api/v1/tools/<tool_name>  body = JSON args",
        f"- REST: POST {base}/api/v1/<group>/<target>/<verb>  e.g. curl -X POST '{base}/api/v1/gpio/P33/set?level=low'",
        f"- Remote CLI line: POST {base}/api/v1/cli  {{\"line\": \"gpio list\"}}",
        f"- Live events (SSE): GET {base}/api/v1/events ; history: GET {base}/api/v1/events/recent",
        f"- OpenAPI: {base}/openapi.json ; docs UI: {base}/docs",
        "- Responses: {ok: true, result: ...} or {ok: false, error: {code, message, hint}}",
        "- Pin targets: 33 | P33 | J40:33 (physical header pin) | GPIO127 (Linux gpio number) | alias such as DUT3/reset",
        "- Aliases: downstream layers bind meaning (gpio_alias_add name=DUT3/reset target=P11) and then call verbs on the alias,",
        "  e.g. POST /api/v1/button/DUT3/reset/click. Delete a DUT's whole tree with gpio_alias_delete name=DUT3 prefix=true.",
        "- Errors always carry code, message, hint (next step) and details (e.g. did_you_mean, valid actions); read the hint first.",
        "",
        "## Tools",
    ]
    for s in hub.registry.specs():
        lines.append(f"- {s.tool_name}: {s.summary}. CLI `{s.cli_usage}`; HTTP {'POST' if s.mutating else 'GET'} {s.http_path}")
    return "\n".join(lines) + "\n"

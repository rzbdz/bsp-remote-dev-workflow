"""POST /api/v1/registry/announce: the announcement needs the address it came from.

No ``from __future__ import annotations`` here: FastAPI resolves the endpoint annotations at runtime.
"""

from typing import Any, Dict

from fastapi import APIRouter, Body, Request
from fastapi.concurrency import run_in_threadpool


def registry_router(mod) -> APIRouter:
    r = APIRouter(tags=["registry"])

    @r.post("/api/v1/registry/announce", summary="A board host announces itself (sent by bsp-hostd)")
    async def announce(request: Request, body: Dict[str, Any] = Body(...)):
        seen = request.client.host if request.client else None
        fwd = request.headers.get("x-forwarded-for")
        if mod.trust_proxy and fwd:
            seen = fwd.split(",")[0].strip()
        return {"ok": True, "result": await run_in_threadpool(mod.announce, body, seen)}

    return r

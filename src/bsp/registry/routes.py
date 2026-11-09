"""POST /api/v1/registry/announce: the announcement needs the address it came from.

No ``from __future__ import annotations`` here: FastAPI resolves the endpoint annotations at runtime.
"""

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse


def registry_router(mod) -> APIRouter:
    r = APIRouter(tags=["registry"])

    @r.post("/api/v1/registry/announce", summary="A board host announces itself (sent by bsp-hostd)")
    async def announce(request: Request, body: Dict[str, Any] = Body(...)):
        seen = request.client.host if request.client else None
        fwd = request.headers.get("x-forwarded-for")
        if mod.trust_proxy and fwd:
            seen = fwd.split(",")[0].strip()
        return {"ok": True, "result": await run_in_threadpool(mod.announce, body, seen)}

    def need_token(request: Request) -> None:
        """Hooks are code hosts run: with a registry token, only its holders get the shared files."""
        want = mod.token
        if not want:
            return
        got = request.headers.get("x-bsp-token") or request.headers.get("authorization", "")
        got = got[7:].strip() if got.startswith("Bearer ") else got.strip()
        if got != want:
            raise HTTPException(403, "missing or wrong token: send 'Authorization: Bearer <registry token>'")

    @r.get("/api/v1/registry/files", summary="SoC profiles and hooks the registry shares (hosts and CLIs layer them)")
    async def files(request: Request):
        need_token(request)
        return {"ok": True, "result": await run_in_threadpool(mod.files.manifest)}

    @r.get("/api/v1/registry/blobs/{sha256}", summary="A shared file or SoC image by its sha256")
    async def blob(sha256: str, request: Request):
        need_token(request)
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
            raise HTTPException(400, "not a sha256")
        p = await run_in_threadpool(mod.files.blob, sha256)
        if not p:
            raise HTTPException(404, "no such file")
        return FileResponse(str(p), media_type="application/octet-stream")

    return r

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .. import files as fileops
from ..uploads import receive_multipart
from ..util import ApiError
from .deps import SSE_HEADERS, Authed, content_disposition, get_ctx, json_body, sse, sse_pump

router = APIRouter(prefix="/api", dependencies=Authed)


async def _receive(request: Request):
    s = get_ctx(request).settings
    return await receive_multipart(request, s.tmp_dir, s.max_upload_bytes, s.max_upload_files)


@router.get("/bots")
async def list_bots(request: Request):
    return {"bots": get_ctx(request).bots.list_json()}


@router.post("/bots")
async def create_bot(request: Request):
    ctx = get_ctx(request)
    staged = await _receive(request)
    try:
        result = await ctx.bots.create(staged)
    finally:
        await asyncio.to_thread(staged.cleanup)
    return JSONResponse(result, status_code=201)


@router.get("/bots/{bot_id}")
async def get_bot(bot_id: str, request: Request):
    return {"bot": get_ctx(request).bots.get_json(bot_id)}


@router.patch("/bots/{bot_id}")
async def patch_bot(bot_id: str, request: Request):
    ctx = get_ctx(request)
    data = await json_body(request)
    return {"bot": ctx.bots.patch(bot_id, data)}


@router.delete("/bots/{bot_id}")
async def delete_bot(bot_id: str, request: Request):
    await get_ctx(request).bots.delete(bot_id)
    return {"ok": True}


@router.post("/bots/{bot_id}/code")
async def upload_code(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    staged = await _receive(request)
    try:
        return await ctx.bots.upload_code(bot_id, staged)
    finally:
        await asyncio.to_thread(staged.cleanup)


@router.post("/bots/{bot_id}/start")
async def start_bot(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    await ctx.supervisor.start(bot_id, manual=True)
    return {"bot": ctx.bots.get_json(bot_id)}


@router.post("/bots/{bot_id}/stop")
async def stop_bot(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    await ctx.supervisor.stop(bot_id)
    return {"bot": ctx.bots.get_json(bot_id)}


@router.post("/bots/{bot_id}/restart")
async def restart_bot(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    await ctx.supervisor.restart(bot_id)
    return {"bot": ctx.bots.get_json(bot_id)}


@router.post("/bots/{bot_id}/reinstall")
async def reinstall_bot(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    await ctx.supervisor.reinstall(bot_id)
    return {"bot": ctx.bots.get_json(bot_id)}


@router.get("/bots/{bot_id}/export")
async def export_bot(bot_id: str, request: Request):
    ctx = get_ctx(request)
    row = ctx.bots.get_row(bot_id)
    ctx.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = ctx.settings.tmp_dir / f"export-{secrets.token_hex(8)}.zip"
    try:
        await asyncio.to_thread(fileops.export_zip, ctx.bots.src(bot_id), tmp)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ApiError(500, f"Export failed: {exc}") from exc
    return FileResponse(
        tmp, media_type="application/zip",
        headers={"Content-Disposition": content_disposition(f"{row['name']}.zip"), "Cache-Control": "no-store"},
        background=BackgroundTask(tmp.unlink, missing_ok=True),
    )


@router.get("/events")
async def events_stream(request: Request):
    ctx = get_ctx(request)
    hub = ctx.events

    def open_stream():
        sub = hub.subscribe()
        return sub, [], lambda: hub.unsubscribe(sub)

    return StreamingResponse(sse_pump(request, open_stream, lambda item: sse(item[0], item[1])),
                             media_type="text/event-stream", headers=SSE_HEADERS)

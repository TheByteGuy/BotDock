from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from .. import files as fileops
from ..uploads import receive_multipart
from ..util import ApiError
from .deps import Authed, content_disposition, get_ctx, json_body, require_str

router = APIRouter(prefix="/api/bots/{bot_id}/files", dependencies=Authed)


@router.get("")
async def list_files(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    entries, truncated = await asyncio.to_thread(fileops.list_entries, ctx.bots.src(bot_id))
    return {"entries": entries, "truncated": truncated}


@router.get("/content")
async def read_file(bot_id: str, request: Request, path: str = ""):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    return await asyncio.to_thread(fileops.read_content, ctx.bots.src(bot_id), path)


@router.put("/content")
async def write_file(bot_id: str, request: Request):
    ctx = get_ctx(request)
    data = await json_body(request)
    path = require_str(data, "path")
    content = data.get("content")
    if not isinstance(content, str):
        raise ApiError(400, "content must be a string")
    return {"entry": await ctx.bots.file_mutation(bot_id, fileops.write_content, path, content)}


@router.get("/download")
async def download_file(bot_id: str, request: Request, path: str = ""):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    rel, full = fileops.file_for_download(ctx.bots.src(bot_id), path)
    return FileResponse(full, media_type="application/octet-stream",
                        headers={"Content-Disposition": content_disposition(rel.rsplit("/", 1)[-1])})


@router.post("/mkdir")
async def make_dir(bot_id: str, request: Request):
    ctx = get_ctx(request)
    data = await json_body(request)
    return {"entry": await ctx.bots.file_mutation(bot_id, fileops.make_dir, require_str(data, "path"))}


@router.post("/move")
async def move(bot_id: str, request: Request):
    ctx = get_ctx(request)
    data = await json_body(request)
    entry = await ctx.bots.file_mutation(bot_id, fileops.move, require_str(data, "from"), require_str(data, "to"))
    return {"entry": entry}


@router.post("/delete")
async def delete(bot_id: str, request: Request):
    ctx = get_ctx(request)
    data = await json_body(request)
    await ctx.bots.file_mutation(bot_id, fileops.delete, require_str(data, "path"))
    return {"ok": True}


@router.post("/upload")
async def upload(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    s = ctx.settings
    staged = await receive_multipart(request, s.tmp_dir, s.max_upload_bytes, s.max_upload_files)
    try:
        return {"entries": await ctx.bots.upload_files(bot_id, staged)}
    finally:
        await asyncio.to_thread(staged.cleanup)

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..github import RateLimited
from .deps import Authed, get_ctx, json_body

router = APIRouter(prefix="/api", dependencies=Authed)


@router.get("/github")
async def github_state(request: Request):
    return get_ctx(request).github.state_json()


@router.put("/github/settings")
async def github_settings(request: Request):
    data = await json_body(request)
    return get_ctx(request).github.update_settings(data)


@router.post("/github/token")
async def github_token(request: Request):
    data = await json_body(request)
    return await get_ctx(request).github.connect_token(data.get("token"))


@router.post("/github/device/start")
async def github_device_start(request: Request):
    return await get_ctx(request).github.device_start()


@router.post("/github/device/poll")
async def github_device_poll(request: Request):
    return await get_ctx(request).github.device_poll()


@router.delete("/github")
async def github_disconnect(request: Request):
    get_ctx(request).github.disconnect()
    return {"ok": True}


@router.get("/github/repos")
async def github_repos(request: Request, q: str = "", page: int = 1):
    return await get_ctx(request).github.list_repos(q.strip()[:100], page)


@router.get("/github/repos/{owner}/{repo}/branches")
async def github_branches(owner: str, repo: str, request: Request):
    return await get_ctx(request).github.branches(owner, repo)


@router.post("/bots/github")
async def create_from_github(request: Request):
    data = await json_body(request)
    result = await get_ctx(request).github.create_bot(data)
    return JSONResponse(result, status_code=201)


@router.put("/bots/{bot_id}/source")
async def set_source(bot_id: str, request: Request):
    data = await json_body(request)
    return {"bot": await get_ctx(request).github.set_source(bot_id, data)}


@router.post("/bots/{bot_id}/source/check")
async def check_source(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    try:
        await ctx.github.check(bot_id)
    except RateLimited:
        pass  # recorded in source.last_error
    return {"bot": ctx.bots.get_json(bot_id)}


@router.post("/bots/{bot_id}/source/sync")
async def sync_source(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    ctx.github.start_sync(bot_id)
    return {"bot": ctx.bots.get_json(bot_id)}

from __future__ import annotations

from fastapi import APIRouter, Request

from ..util import ApiError
from .deps import Authed, get_ctx, json_body

router = APIRouter(prefix="/api/bots/{bot_id}/env", dependencies=Authed)


@router.get("")
async def list_env(bot_id: str, request: Request):
    return {"vars": get_ctx(request).bots.list_env(bot_id)}


@router.post("/import")
async def import_env(bot_id: str, request: Request):
    data = await json_body(request)
    overwrite = data.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ApiError(400, "overwrite must be a boolean")
    return get_ctx(request).bots.import_env(bot_id, data.get("text"), overwrite)


@router.get("/{key}")
async def reveal_env(bot_id: str, key: str, request: Request):
    return get_ctx(request).bots.reveal_env(bot_id, key)


@router.put("/{key}")
async def set_env(bot_id: str, key: str, request: Request):
    data = await json_body(request)
    if "value" not in data:
        raise ApiError(400, "value is required")
    get_ctx(request).bots.set_env(bot_id, key, data["value"])
    return {"ok": True}


@router.delete("/{key}")
async def delete_env(bot_id: str, key: str, request: Request):
    get_ctx(request).bots.delete_env(bot_id, key)
    return {"ok": True}

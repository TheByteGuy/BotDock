from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..logstore import Subscriber
from .deps import SSE_HEADERS, Authed, content_disposition, get_ctx, sse, sse_pump

router = APIRouter(prefix="/api/bots/{bot_id}/logs", dependencies=Authed)
MAX_TAIL = 5000


@router.get("")
async def tail_logs(bot_id: str, request: Request, tail: int = 500):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    tail = max(0, min(tail, MAX_TAIL))
    return {"lines": ctx.supervisor.runtime(bot_id).tail(tail)}


@router.get("/stream")
async def stream_logs(bot_id: str, request: Request, after: int | None = None):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    if after is None:
        last_id = request.headers.get("last-event-id", "")
        after = int(last_id) if last_id.isdigit() else 0
    rt = ctx.supervisor.runtime(bot_id)
    last = [after]

    def render(line: dict) -> str | None:
        if line["seq"] <= last[0]:
            return None
        last[0] = line["seq"]
        return sse(None, line, line["seq"])

    def open_stream():
        sub = Subscriber()
        rt.subscribers.add(sub)
        # Snapshot the backlog in the same tick as subscribing: no gap, no duplicates.
        prelude = [out for line in list(rt.lines) if line["seq"] > after and (out := render(line))]
        return sub, prelude, lambda: rt.subscribers.discard(sub)

    return StreamingResponse(sse_pump(request, open_stream, render), media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.get("/download")
async def download_logs(bot_id: str, request: Request):
    ctx = get_ctx(request)
    row = ctx.bots.get_row(bot_id)
    writer = ctx.supervisor.runtime(bot_id).writer
    return StreamingResponse(
        writer.iter_bytes(), media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": content_disposition(f"{row['name']}.log"), "Cache-Control": "no-store"},
    )


@router.delete("")
async def clear_logs(bot_id: str, request: Request):
    ctx = get_ctx(request)
    ctx.bots.get_row(bot_id)
    ctx.supervisor.runtime(bot_id).clear_logs()
    return {"ok": True}

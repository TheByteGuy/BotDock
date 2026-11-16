from __future__ import annotations

import platform
import shutil
import time

from fastapi import APIRouter, Request

from .. import __version__
from .deps import Authed, get_ctx

router = APIRouter(prefix="/api", dependencies=Authed)


@router.get("/system")
async def system_info(request: Request):
    ctx = get_ctx(request)
    try:
        usage = shutil.disk_usage(ctx.settings.data_dir)
        disk = {"total_bytes": usage.total, "free_bytes": usage.free}
    except OSError:
        disk = {"total_bytes": None, "free_bytes": None}
    return {
        "version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "data_dir": str(ctx.settings.data_dir),
        "bot_count": len(ctx.db.list_bots()),
        "running_count": ctx.supervisor.running_count(),
        "uptime_seconds": int(time.time() - ctx.started_at),
        "disk": disk,
    }

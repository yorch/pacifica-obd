"""Runtime settings routes (design §3.12).

Only two things are settable at runtime, and neither can enable a write:

* the default bus (a read-path convenience), and
* the ``0x27`` strict-seed-reread opt-in (owner decision §9.2), documented with
  its module-lockout caveat.

``read_only`` is a server start flag and is deliberately **not** patchable here:
a page must not be able to arm the write surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from pacifica_obd.web import deps, errors

router = APIRouter()

VALID_BUSES = ("can-c", "can-ihs")


class RuntimeSettingsPatch(BaseModel):
    bus: str | None = None
    strict_seed_reread: bool | None = None


@router.get("/settings", summary="Runtime settings and the safety posture")
async def get_settings(request: Request):
    settings = deps.get_settings(request)
    return {
        "bus": deps.get_worker(request).bus,
        "read_only": bool(settings.read_only),
        "write_enabled": bool(settings.write_enabled),
        "strict_seed_reread": bool(settings.strict_seed_reread),
        "safety": {
            "read_only_default": True,
            "enabled_by": "--allow-writes (server flag only)",
            "sgw": "The Security Gateway usually blocks writes on 2018+ vehicles.",
            "seed_policy": ("held seed under a 30 s lease; no second seed request "
                            "unless strict seed re-read is enabled"),
        },
    }


@router.put("/settings", summary="Patch runtime settings")
async def put_settings(request: Request, body: RuntimeSettingsPatch):
    settings = deps.get_settings(request)
    worker = deps.get_worker(request)
    if body.bus is not None:
        if body.bus not in VALID_BUSES:
            raise errors.validation(f"unknown bus: {body.bus!r}")
        worker.bus = body.bus
    if body.strict_seed_reread is not None:
        settings.strict_seed_reread = bool(body.strict_seed_reread)
    return await get_settings(request)

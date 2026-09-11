"""raanu.api.routes.settings — the runtime-editable trading limits.

Read is free (the passphrase covers it); write needs the trade PIN, because
raising the weekly budget is a money-moving act even though it places no
order itself.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from raanu import settings

log = logging.getLogger("raanu.api.routes.settings")

router = APIRouter()


class SettingUpdate(BaseModel):
    """One or more settings by name. Unknown names are rejected rather than
    ignored — a typo in a UI form must not look like it worked."""

    values: dict[str, float]


@router.get("/api/settings")
async def read_settings():
    """Every setting, its value, and WHERE that value came from.

    ``source`` (store / env / default) is the point: "why is the budget
    20,000" should be answerable without guessing whether a change landed.
    """
    return {"settings": settings.snapshot()}


@router.put("/api/settings")
async def write_settings(body: SettingUpdate):
    """Store one or more settings. Returns what was actually stored.

    Values are CLAMPED to each setting's bounds rather than taken at face
    value, so the response is the authority on what took effect — a UI must
    render what comes back, not what it sent.
    """
    unknown = sorted(set(body.values) - set(settings.SPECS))
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"unknown setting(s): {', '.join(unknown)}")
    applied = {}
    for name, value in body.values.items():
        try:
            applied[name] = settings.put(name, value)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400,
                                detail=f"{name}: {e}") from e
    log.info(f"[settings] updated {applied}")
    return {"applied": applied, "settings": settings.snapshot()}


@router.delete("/api/settings/{name}")
async def clear_setting(name: str):
    """Drop the stored override so the env var or the code default applies."""
    if name not in settings.SPECS:
        raise HTTPException(status_code=400, detail=f"unknown setting: {name}")
    settings.clear(name)
    return {"cleared": name, "settings": settings.snapshot()}

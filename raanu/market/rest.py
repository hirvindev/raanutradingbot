"""
raanu.market.rest — thin Alpaca REST helpers
=============================================
Shared request plumbing for the broker API: auth headers and the four verbs,
each returning parsed JSON and raising HTTPException on a non-2xx so route
handlers do not each re-implement error translation.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from raanu import config

log = logging.getLogger("raanu.market.rest")


# ---------- ALPACA CLIENT ----------
def alpaca_headers() -> dict:
    if not config.alpaca_key():
        raise HTTPException(
            status_code=400,
            detail="ALPACA_API_KEY is not set.",
        )
    return {
        "APCA-API-KEY-ID":     config.alpaca_key(),
        "APCA-API-SECRET-KEY": config.alpaca_secret(),
        "Content-Type":        "application/json",
    }


async def alpaca_get(path: str, params: dict | None = None):
    url = f"{config.broker_base()}{path}"
    log.info(f"GET  {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(url, headers=alpaca_headers(), params=params)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}") from e
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Alpaca rejected the API key. Check ALPACA_API_KEY and ALPACA_SECRET_KEY.")
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="Alpaca rate limit hit.")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"Alpaca error: {r.text}")
    return r.json()


async def alpaca_post(path: str, body: dict):
    url = f"{config.broker_base()}{path}"
    log.info(f"POST {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(url, headers=alpaca_headers(), json=body)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}") from e
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"Alpaca error: {r.text}")
    return r.json()


async def alpaca_delete(path: str):
    url = f"{config.broker_base()}{path}"
    log.info(f"DELETE {url}")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(url, headers=alpaca_headers())
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"cancelled": True}

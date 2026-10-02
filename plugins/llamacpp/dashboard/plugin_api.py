"""Thin Hermes Core Local Models control-plane adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

try:
    from .core_api import CoreApiAdapter
except ImportError:  # standalone dashboard loader
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core_api import CoreApiAdapter


router = APIRouter()


def _forward_headers(request: Request) -> dict[str, str]:
    allowed = {
        "authorization",
        "cookie",
        "x-hermes-profile",
        "x-hermes-session-token",
    }
    return {
        key.lower(): value
        for key, value in request.headers.items()
        if key.lower() in allowed
    }


async def _core_request(
    request: Request,
    method: str,
    path: str,
    body: object = None,
) -> dict[str, Any]:
    query: dict[str, str] = {}
    profile = request.query_params.get("profile")
    if profile:
        query["profile"] = profile
    origin = f"{request.url.scheme}://{request.url.netloc}"
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        response = await client.request(
            method,
            origin + path,
            params=query,
            json=body if body is not None else None,
            headers=_forward_headers(request),
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text or f"Core returned HTTP {response.status_code}"}
    if response.is_error:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(
            status_code=response.status_code,
            detail=detail or "Hermes Core request failed",
        )
    return payload if isinstance(payload, dict) else {"result": payload}


def _core_adapter(request: Request) -> CoreApiAdapter:
    async def send(method: str, path: str, body: object = None) -> dict[str, Any]:
        return await _core_request(request, method, path, body)

    return CoreApiAdapter(send)


@router.get("/core/status")
async def core_status(request: Request):
    return await _core_adapter(request).snapshot()


@router.get("/core/jobs")
async def core_jobs(request: Request):
    return await _core_adapter(request).jobs()


@router.get("/core/jobs/{job_id}")
async def core_job(job_id: str, request: Request):
    return await _core_adapter(request).job(job_id)


@router.put("/core/profiles/{role}")
async def core_profile(role: str, request: Request):
    body = await request.json()
    try:
        return await _core_adapter(request).assign(role, str(body.get("model_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/core/server")
async def core_server(request: Request):
    body = await request.json()
    try:
        return await _core_adapter(request).server(str(body.get("action") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/core/eject")
async def core_eject(request: Request):
    body = await request.json()
    return await _core_adapter(request).eject(str(body.get("model_id") or ""))


@router.post("/core/activate")
async def core_activate(request: Request):
    body = await request.json()
    return await _core_adapter(request).activate(str(body.get("model_id") or ""))
"""DomOS cloud voice gateway, conversation API and wallpaper proxy."""

from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from config import settings
from services.openrouter_voice_service import (
    conversation_store,
    handle_openrouter_voice,
    primary_llm_model,
    primary_llm_provider,
    voice_registry,
)
from services.football_service import football_service
from services.codex_usage_service import CodexUsageError, codex_usage_service

logging.basicConfig(
    level=logging.DEBUG if settings.DOMOS_DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("domos.main")


class DeviceSettingsRequest(BaseModel):
    device_id: str | None = None
    volume: int | None = Field(default=None, ge=0, le=100)
    brightness: int | None = Field(default=None, ge=0, le=100)


def _authorize_device_control(authorization: str | None) -> None:
    expected = settings.BOARD_CONTROL_AUTH_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="Device control authentication is not configured")
    supplied = authorization or ""
    if not hmac.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Invalid device control token")


def _device_tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        raise HTTPException(status_code=502, detail="The board rejected the command")
    content = result.get("content")
    text = content[0].get("text") if isinstance(content, list) and content else None
    if not isinstance(text, str):
        return {}
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return {"message": text}
    return decoded if isinstance(decoded, dict) else {"message": text}


async def _active_device(device_id: str | None = None):
    session = await voice_registry.get(device_id)
    if session is None:
        raise HTTPException(status_code=503, detail="Board is not connected to the cloud gateway")
    return session

@asynccontextmanager
async def lifespan(_: FastAPI):
    await conversation_store.initialize()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="0.5.0",
    description="Dom Voice Protocol v3 with OpenRouter and persistent memory",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check() -> dict:
    return {
        "status": "online",
        "service": settings.APP_NAME,
        "version": "0.5.0",
        "provider": primary_llm_provider(),
        "local_ai": False,
        "active_sessions": voice_registry.count,
        "model": primary_llm_model(),
        "audio_model": settings.OPENROUTER_AUDIO_MODEL,
        "stt_provider": settings.STT_PROVIDER,
        "tts_provider": settings.TTS_PROVIDER,
        "api_key_configured": primary_llm_provider() != "unconfigured",
        "openai_key_configured": bool(settings.OPENAI_API_KEY),
        "openrouter_key_configured": bool(settings.OPENROUTER_API_KEY),
        "memory": "sqlite",
    }


@app.websocket("/api/v1/voice/stream")
async def voice_stream_websocket(websocket: WebSocket) -> None:
    await handle_openrouter_voice(websocket)


@app.get("/api/device/status")
async def device_status(
    device_id: str | None = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize_device_control(authorization)
    session = await _active_device(device_id)
    try:
        result = await session.call_device_tool("device.get_status", {})
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Board status request timed out") from exc
    payload = _device_tool_payload(result)
    return {
        **payload,
        "id": session.device_id,
        "mac": session.device_id,
        "name": "ES3C28P Desk Terminal",
        "board": "ES3C28P",
        "online": True,
        "connection": "cloud",
    }


@app.post("/api/device/settings")
async def update_device_settings(
    request: DeviceSettingsRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize_device_control(authorization)
    if request.volume is None and request.brightness is None:
        raise HTTPException(status_code=422, detail="Provide volume or brightness")
    session = await _active_device(request.device_id)
    applied: dict[str, int] = {}
    commands = (
        ("volume", request.volume, "speaker.set_volume", "volume"),
        ("brightness", request.brightness, "display.set_brightness", "brightness"),
    )
    try:
        for output_key, value, tool_name, argument_name in commands:
            if value is None:
                continue
            result = await session.call_device_tool(tool_name, {argument_name: value})
            _device_tool_payload(result)
            applied[output_key] = value
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Board command timed out") from exc
    return {"ok": True, "device_id": session.device_id, "applied": applied}


@app.get("/api/v1/conversations")
async def list_conversations(device_id: str | None = None, limit: int = 50) -> dict:
    items = await conversation_store.list_turns(device_id=device_id, limit=limit)
    return {"items": items, "count": len(items)}


@app.get("/api/football/manchester-united")
async def manchester_united_schedule(force: bool = False) -> dict:
    try:
        return await football_service.get_schedule(force=force)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/football/manchester-united/background.jpg")
async def manchester_united_background() -> Response:
    try:
        content = await football_service.get_background_jpeg()
    except (RuntimeError, httpx.HTTPError, OSError, ValueError) as exc:
        logger.warning("Manchester United background unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Club background unavailable") from exc
    return Response(content=content, media_type="image/jpeg")


@app.get("/api/codex/usage")
async def codex_usage(force: bool = False) -> dict:
    try:
        return await codex_usage_service.get_usage(force=force)
    except CodexUsageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/codex/usage/sync")
async def sync_codex_usage(
    snapshot: dict, authorization: str | None = Header(default=None)
) -> dict:
    expected = settings.CODEX_USAGE_SYNC_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="Codex usage sync is disabled")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid sync token")
    try:
        stored = await codex_usage_service.store_synced(snapshot)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "updated_at": stored["updated_at"]}


@app.get("/api/wallpapers/slideshow")
async def proxy_wallpapers_slideshow() -> JSONResponse:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.CORE_BACKEND_URL.rstrip('/')}/api/wallpapers/slideshow",
                timeout=5.0,
            )
        data = response.json()
        wallpapers = data.get("data", {}).get("wallpapers", [])
        data.get("data", {})["wallpapers"] = [
            url.replace(":8081", ":8000") for url in wallpapers
        ]
        return JSONResponse(content=data, status_code=response.status_code)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Wallpaper slideshow proxy failed: %s", exc)
        return JSONResponse(content={"error": str(exc)}, status_code=502)


@app.get("/uploads/wallpapers/{filename}")
async def proxy_wallpaper_file(filename: str) -> Response:
    safe_name = Path(filename).name
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.CORE_BACKEND_URL.rstrip('/')}/uploads/wallpapers/{safe_name}",
                timeout=10.0,
            )
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "image/jpeg"),
        )
    except httpx.HTTPError as exc:
        logger.warning("Wallpaper file proxy failed for %s: %s", safe_name, exc)
        return Response(status_code=502)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.HOST, port=settings.PORT)

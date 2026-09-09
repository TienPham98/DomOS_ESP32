"""DomOS cloud voice gateway, conversation API and wallpaper proxy."""

from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from config import settings
from services.openrouter_voice_service import (
    conversation_store,
    effective_wake_stt_timeout,
    handle_openrouter_voice,
    primary_llm_model,
    primary_llm_provider,
    primary_stt_provider,
    primary_wake_stt_provider,
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


class ClockSettingsRequest(BaseModel):
    device_id: str | None = None
    style: Literal["digital", "minimal", "analog", "flip", "word", "binary"]
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    mode: Literal["dark", "light"]


class WallpaperCommandRequest(BaseModel):
    device_id: str | None = None
    action: Literal["set", "sync"] = "set"
    wallpaper_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")


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


async def _call_device_tool(session, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(settings.DEVICE_COMMAND_RETRY_WINDOW_SEC, 5)
    attempted_sessions: set[str] = set()
    current = session
    last_error: BaseException | None = None

    while loop.time() < deadline:
        if current.session_id not in attempted_sessions:
            attempted_sessions.add(current.session_id)
            try:
                result = await current.call_device_tool(name, arguments)
                return _device_tool_payload(result)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                last_error = exc
                logging.getLogger("domos.gateway").warning(
                    "Device tool failed on session %s; waiting for reconnect: %s",
                    current.session_id,
                    name,
                )

        await asyncio.sleep(0.25)
        replacement = await voice_registry.get(current.device_id)
        if replacement is not None and replacement.session_id not in attempted_sessions:
            current = replacement

    raise HTTPException(
        status_code=504,
        detail=f"Device command timed out after reconnect: {name}",
    ) from last_error


async def _core_request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
) -> httpx.Response:
    headers = {"Content-Type": content_type} if content_type else None
    try:
        async with httpx.AsyncClient() as client:
            return await client.request(
                method,
                f"{settings.CORE_BACKEND_URL.rstrip('/')}{path}",
                content=body,
                headers=headers,
                timeout=30.0,
            )
    except httpx.HTTPError as exc:
        logger.warning("Core backend request failed for %s %s: %s", method, path, exc)
        raise HTTPException(status_code=502, detail="Core backend is unavailable") from exc


def _wallpaper_proxy_url(request: Request, raw_url: str) -> str:
    filename = Path(urlsplit(raw_url).path).name
    if not filename:
        return raw_url
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    origin = f"{scheme}://{host}".rstrip("/")
    return f"{origin}/uploads/wallpapers/{quote(filename)}"

@asynccontextmanager
async def lifespan(_: FastAPI):
    await conversation_store.initialize()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="0.6.5",
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
        "version": "0.6.5",
        "provider": primary_llm_provider(),
        "local_ai": False,
        "active_sessions": voice_registry.count,
        "device_command_retry_window_sec": settings.DEVICE_COMMAND_RETRY_WINDOW_SEC,
        "model": primary_llm_model(),
        "audio_model": settings.OPENROUTER_AUDIO_MODEL,
        "stt_provider": primary_stt_provider(),
        "stt_configured_provider": settings.STT_PROVIDER,
        "wake_stt_provider": primary_wake_stt_provider(),
        "wake_stt_configured_provider": settings.WAKE_STT_PROVIDER,
        "wake_stt_timeout_sec": effective_wake_stt_timeout(),
        "wake_stt_configured_timeout_sec": settings.WAKE_STT_TIMEOUT_SEC,
        "wake_stt_openai_fallback": bool(
            settings.WAKE_STT_OPENAI_FALLBACK and settings.OPENAI_API_KEY
        ),
        "wake_stt_openrouter_fallback": bool(
            settings.STT_OPENROUTER_FALLBACK and settings.OPENROUTER_API_KEY
        ),
        "tts_provider": settings.TTS_PROVIDER,
        "api_key_configured": primary_llm_provider() != "unconfigured",
        "openai_key_configured": bool(settings.OPENAI_API_KEY),
        "openrouter_key_configured": bool(settings.OPENROUTER_API_KEY),
        "llm_streaming": settings.LLM_STREAMING_ENABLED,
        "voice_heartbeat_interval_sec": settings.VOICE_HEARTBEAT_INTERVAL_SEC,
        "web_search_provider": settings.WEB_SEARCH_PROVIDER,
        "web_search_configured": bool(settings.WEB_SEARCH_BASE_URL),
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
    payload = await _call_device_tool(session, "device.get_status", {})
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
    for output_key, value, tool_name, argument_name in commands:
        if value is None:
            continue
        await _call_device_tool(session, tool_name, {argument_name: value})
        applied[output_key] = value
    return {"ok": True, "device_id": session.device_id, "applied": applied}


@app.post("/api/device/clock")
async def update_clock_settings(
    request: ClockSettingsRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize_device_control(authorization)
    session = await _active_device(request.device_id)
    applied = request.model_dump(exclude={"device_id"})
    await _call_device_tool(session, "clock.configure", applied)
    return {"ok": True, "device_id": session.device_id, "applied": applied}


@app.post("/api/device/wallpaper")
async def update_device_wallpaper(
    command: WallpaperCommandRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize_device_control(authorization)
    session = await _active_device(command.device_id)
    if command.action == "sync":
        await _call_device_tool(session, "wallpaper.sync", {})
        return {"ok": True, "device_id": session.device_id, "applied": {"action": "sync"}}
    if not command.wallpaper_id:
        raise HTTPException(status_code=422, detail="wallpaper_id is required for set")

    metadata_response = await _core_request("GET", f"/api/wallpaper/{command.wallpaper_id}")
    if metadata_response.status_code == 404:
        raise HTTPException(status_code=404, detail="Wallpaper not found")
    if not metadata_response.is_success:
        raise HTTPException(status_code=502, detail="Unable to load wallpaper metadata")
    metadata = metadata_response.json().get("data", {})
    raw_url = metadata.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        raise HTTPException(status_code=502, detail="Wallpaper metadata has no URL")
    arguments = {
        "url": _wallpaper_proxy_url(request, raw_url),
        "name": str(metadata.get("name") or "wallpaper"),
    }
    await _call_device_tool(session, "wallpaper.set", arguments)
    return {
        "ok": True,
        "device_id": session.device_id,
        "applied": {"action": "set", "wallpaper_id": command.wallpaper_id},
    }


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


@app.get("/api/wallpapers")
async def list_wallpapers(request: Request) -> JSONResponse:
    response = await _core_request("GET", "/api/wallpapers")
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from core backend") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if response.is_success and isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            for field in ("url", "thumbnail_url"):
                raw_url = item.get(field)
                if isinstance(raw_url, str) and raw_url:
                    item[field] = _wallpaper_proxy_url(request, raw_url)
    return JSONResponse(content=payload, status_code=response.status_code)


@app.post("/api/wallpaper")
async def upload_wallpaper(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    _authorize_device_control(authorization)
    response = await _core_request(
        "POST",
        "/api/wallpaper",
        body=await request.body(),
        content_type=request.headers.get("content-type"),
    )
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


@app.delete("/api/wallpaper/{wallpaper_id}")
async def delete_wallpaper(
    wallpaper_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    _authorize_device_control(authorization)
    if not wallpaper_id or len(wallpaper_id) > 64 or not wallpaper_id.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid wallpaper id")
    response = await _core_request("DELETE", f"/api/wallpaper/{wallpaper_id}")
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
    )


@app.get("/api/wallpapers/slideshow")
async def proxy_wallpapers_slideshow(request: Request) -> JSONResponse:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.CORE_BACKEND_URL.rstrip('/')}/api/wallpapers/slideshow",
                timeout=5.0,
            )
        data = response.json()
        wallpapers = data.get("data", {}).get("wallpapers", [])
        data.get("data", {})["wallpapers"] = [
            _wallpaper_proxy_url(request, url) for url in wallpapers
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

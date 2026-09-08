"""End-to-end cloud voice smoke test using a synthetic Vietnamese utterance."""

import asyncio
import json
import os
from urllib.parse import urlsplit, urlunsplit

import websockets

from config import settings
from services.openrouter_voice_service import (
    PCM_FRAME_BYTES,
    _decode_mp3,
    _google_synthesize,
)


def websocket_url(http_url: str) -> str:
    parsed = urlsplit(http_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/api/v1/voice/stream", "", ""))


async def main() -> None:
    base_url = os.getenv(
        "DOMOS_SMOKE_BASE_URL",
        "http://127.0.0.1:8000",
    )
    mode = os.getenv("DOMOS_SMOKE_MODE", "command").strip().lower()
    phrase = os.getenv(
        "DOMOS_SMOKE_PHRASE",
        "Hey Dom" if mode == "wake" else "Bạn là ai",
    )
    mp3 = await asyncio.to_thread(_google_synthesize, phrase)
    pcm = await asyncio.to_thread(_decode_mp3, mp3)
    headers = {"Device-Id": "runtime-e2e", "Protocol-Version": "3"}
    if settings.VOICE_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {settings.VOICE_AUTH_TOKEN}"

    async with websockets.connect(
        websocket_url(base_url),
        additional_headers=headers,
        open_timeout=15,
    ) as websocket:
        await websocket.send(json.dumps({
            "type": "hello",
            "version": 3,
            "audio_params": {
                "codec": "pcm",
                "sample_rate": 16_000,
                "channels": 1,
                "frame_duration": 60,
            },
        }))
        await asyncio.wait_for(websocket.recv(), timeout=10)
        await asyncio.wait_for(websocket.recv(), timeout=10)
        if mode != "wake":
            await websocket.send(json.dumps({"type": "listen", "state": "start"}))

        for offset in range(0, len(pcm), PCM_FRAME_BYTES):
            frame = pcm[offset:offset + PCM_FRAME_BYTES]
            await websocket.send(frame.ljust(PCM_FRAME_BYTES, b"\0"))
            await asyncio.sleep(0.002)
        for _ in range(12):
            await websocket.send(bytes(PCM_FRAME_BYTES))

        states: list[str] = []
        binary_frames = 0
        while True:
            item = await asyncio.wait_for(websocket.recv(), timeout=90)
            if isinstance(item, bytes):
                binary_frames += 1
                continue
            message = json.loads(item)
            state = message.get("state", message.get("emotion", ""))
            states.append(f"{message.get('type', '')}:{state}")
            if (
                mode == "wake"
                and message.get("type") == "listen"
                and message.get("state") == "start"
                and message.get("source") == "wake_word"
            ):
                break
            if message.get("type") == "tts" and message.get("state") == "stop":
                break

    if mode == "wake":
        assert "listen:start" in states
    else:
        assert "tts:start" in states
        assert binary_frames > 0
    print(json.dumps({
        "completed": True,
        "mode": mode,
        "states": states,
        "binary_audio_frames": binary_frames,
    }))


if __name__ == "__main__":
    asyncio.run(main())

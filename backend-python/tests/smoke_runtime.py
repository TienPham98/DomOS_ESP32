"""Runtime smoke test for gateway health, history API and voice handshake."""

import asyncio
import json

import httpx
import websockets


async def main() -> None:
    async with httpx.AsyncClient() as client:
        health = (await client.get("http://127.0.0.1:8000/health", timeout=5)).json()
        history = (await client.get("http://127.0.0.1:8000/api/v1/conversations", timeout=5)).json()
    async with websockets.connect(
        "ws://127.0.0.1:8000/api/v1/voice/stream",
        additional_headers={"Device-Id": "runtime-smoke", "Protocol-Version": "3"},
    ) as websocket:
        await websocket.send(json.dumps({"type": "hello", "version": 3, "audio_params": {"codec": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
        hello = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
    assert health["provider"] == "openai"
    assert health["local_ai"] is False
    assert health["api_key_configured"] is True
    assert hello["provider"] == "openai"
    assert isinstance(history["items"], list)
    print("RUNTIME_SMOKE_OK: OpenAI-first gateway, memory API and voice handshake")


if __name__ == "__main__":
    asyncio.run(main())

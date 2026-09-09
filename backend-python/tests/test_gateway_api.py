import asyncio
import json
import tempfile
import unittest
import warnings
from array import array
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import httpx
from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import main
from config import Settings, settings
from services.conversation_store import ConversationStore
from services.codex_usage_service import CodexUsageService
from services.football_service import FootballService
from services.openrouter_voice_service import (
    NO_SPEECH_RESPONSE,
    OpenAIAPIError,
    PCM_FRAME_BYTES,
    VoiceRegistry,
    VoiceSession,
    validate_dom_hello,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.text_messages: list[dict] = []
        self.binary_messages: list[bytes] = []

    async def send_text(self, payload: str) -> None:
        self.text_messages.append(json.loads(payload))

    async def send_bytes(self, payload: bytes) -> None:
        self.binary_messages.append(payload)


class DeviceCommandRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_retries_on_reconnected_session(self):
        stale = VoiceSession(FakeWebSocket(), "board-a", "session-stale")
        fresh = VoiceSession(FakeWebSocket(), "board-a", "session-fresh")
        stale.call_device_tool = AsyncMock(side_effect=asyncio.TimeoutError)
        fresh.call_device_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "ok"}],
            "isError": False,
        })

        with (
            patch.object(main.voice_registry, "get", AsyncMock(return_value=fresh)),
            patch.object(main.asyncio, "sleep", AsyncMock()),
        ):
            result = await main._call_device_tool(stale, "device.get_status", {})

        self.assertEqual(result, {"message": "ok"})
        stale.call_device_tool.assert_awaited_once()
        fresh.call_device_tool.assert_awaited_once()


class GatewayApiTests(unittest.TestCase):
    def test_external_service_configuration_has_no_code_defaults(self):
        env_only_fields = (
            "HOST",
            "PORT",
            "OPENROUTER_BASE_URL",
            "OPENROUTER_MODEL",
            "OPENROUTER_AUDIO_MODEL",
            "OPENROUTER_TIMEOUT_SEC",
            "OPENROUTER_HTTP_REFERER",
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "OPENAI_STT_MODEL",
            "OPENAI_TIMEOUT_SEC",
            "LLM_PROVIDER_ORDER",
            "STT_PROVIDER",
            "STT_LANGUAGE",
            "STT_OPENROUTER_FALLBACK",
            "TTS_PROVIDER",
            "TTS_VOICE",
            "TTS_TIMEOUT_SEC",
            "CORE_BACKEND_URL",
            "FOOTBALL_DATA_BASE_URL",
            "MANCHESTER_UNITED_BADGE_URL",
            "MQTT_BROKER_HOST",
            "MQTT_BROKER_PORT",
            "MQTT_CLIENT_ID",
        )

        for field_name in env_only_fields:
            with self.subTest(field=field_name):
                self.assertTrue(Settings.model_fields[field_name].is_required())

    def test_health_reports_cloud_only_voice_stack(self):
        with TestClient(main.app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["provider"], "openai")
        self.assertFalse(payload["local_ai"])
        self.assertEqual(payload["memory"], "sqlite")
        self.assertIn("stt_provider", payload)
        self.assertIn("tts_provider", payload)
        self.assertTrue(payload["llm_streaming"])
        self.assertEqual(payload["web_search_provider"], settings.WEB_SEARCH_PROVIDER)

    def test_device_status_requires_control_token(self):
        with patch.object(main.settings, "BOARD_CONTROL_AUTH_TOKEN", "control-secret"):
            with TestClient(main.app) as client:
                response = client.get("/api/device/status")
        self.assertEqual(response.status_code, 401)

    def test_device_status_uses_active_voice_session(self):
        session = VoiceSession(FakeWebSocket(), "board-a", "session-a")
        session.call_device_tool = AsyncMock(return_value={
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "firmware": "0.3.5",
                    "volume": 80,
                    "brightness": 75,
                    "free_heap": 120000,
                }),
            }],
            "isError": False,
        })
        with (
            patch.object(main.settings, "BOARD_CONTROL_AUTH_TOKEN", "control-secret"),
            patch.object(main.voice_registry, "get", AsyncMock(return_value=session)),
        ):
            with TestClient(main.app) as client:
                response = client.get(
                    "/api/device/status",
                    headers={"Authorization": "Bearer control-secret"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["online"])
        self.assertEqual(response.json()["volume"], 80)
        session.call_device_tool.assert_awaited_once_with("device.get_status", {})

    def test_device_settings_send_exact_volume_and_brightness(self):
        session = VoiceSession(FakeWebSocket(), "board-a", "session-a")
        session.call_device_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "ok"}],
            "isError": False,
        })
        with (
            patch.object(main.settings, "BOARD_CONTROL_AUTH_TOKEN", "control-secret"),
            patch.object(main.voice_registry, "get", AsyncMock(return_value=session)),
        ):
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/device/settings",
                    headers={"Authorization": "Bearer control-secret"},
                    json={"volume": 65, "brightness": 40},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["applied"], {"volume": 65, "brightness": 40})
        self.assertEqual(
            session.call_device_tool.await_args_list,
            [
                call("speaker.set_volume", {"volume": 65}),
                call("display.set_brightness", {"brightness": 40}),
            ],
        )

    def test_clock_settings_use_cloud_device_tool(self):
        session = VoiceSession(FakeWebSocket(), "board-a", "session-a")
        session.call_device_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "queued"}],
            "isError": False,
        })
        with (
            patch.object(main.settings, "BOARD_CONTROL_AUTH_TOKEN", "control-secret"),
            patch.object(main.voice_registry, "get", AsyncMock(return_value=session)),
        ):
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/device/clock",
                    headers={"Authorization": "Bearer control-secret"},
                    json={"style": "minimal", "color": "#06b6d4", "mode": "dark"},
                )

        self.assertEqual(response.status_code, 200)
        session.call_device_tool.assert_awaited_once_with(
            "clock.configure",
            {"style": "minimal", "color": "#06b6d4", "mode": "dark"},
        )

    def test_wallpaper_selection_resolves_metadata_server_side(self):
        session = VoiceSession(FakeWebSocket(), "board-a", "session-a")
        session.call_device_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "queued"}],
            "isError": False,
        })
        metadata = httpx.Response(200, json={
            "data": {
                "id": "wp-1",
                "name": "desk.jpg",
                "url": "http://go-core:8080/uploads/wallpapers/bg_wp-1.jpg",
            }
        })
        with (
            patch.object(main.settings, "BOARD_CONTROL_AUTH_TOKEN", "control-secret"),
            patch.object(main.voice_registry, "get", AsyncMock(return_value=session)),
            patch.object(main, "_core_request", AsyncMock(return_value=metadata)),
        ):
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/device/wallpaper",
                    headers={"Authorization": "Bearer control-secret"},
                    json={"action": "set", "wallpaper_id": "wp-1"},
                )

        self.assertEqual(response.status_code, 200)
        arguments = session.call_device_tool.await_args.args[1]
        self.assertEqual(session.call_device_tool.await_args.args[0], "wallpaper.set")
        self.assertEqual(arguments["name"], "desk.jpg")
        self.assertTrue(arguments["url"].endswith("/uploads/wallpapers/bg_wp-1.jpg"))
        self.assertNotIn("go-core", arguments["url"])

    def test_wallpaper_list_rewrites_internal_urls_to_gateway(self):
        core_response = httpx.Response(200, json={
            "success": True,
            "data": [{
                "id": "wp-1",
                "url": "http://go-core:8080/uploads/wallpapers/bg_wp-1.jpg",
                "thumbnail_url": "http://go-core:8080/uploads/wallpapers/thumb_wp-1.jpg",
            }],
        })
        with patch.object(main, "_core_request", AsyncMock(return_value=core_response)):
            with TestClient(main.app) as client:
                response = client.get("/api/wallpapers")

        self.assertEqual(response.status_code, 200)
        wallpaper = response.json()["data"][0]
        self.assertIn("/uploads/wallpapers/bg_wp-1.jpg", wallpaper["url"])
        self.assertNotIn("go-core", wallpaper["url"])

    def test_conversation_endpoint_filters_by_device(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(str(Path(directory) / "api.db"))
            asyncio.run(store.initialize())
            first = asyncio.run(store.create_turn("board-a", "xin chào", "openrouter", "free"))
            asyncio.run(store.complete_turn(first, "Chào bạn!"))
            second = asyncio.run(store.create_turn("board-b", "mấy giờ", "openrouter", "free"))
            asyncio.run(store.complete_turn(second, "Bây giờ là 20 giờ."))

            with patch.object(main, "conversation_store", store):
                with TestClient(main.app) as client:
                    response = client.get(
                        "/api/v1/conversations", params={"device_id": "board-a", "limit": 10}
                    )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["device_id"], "board-a")

    def test_wallpaper_proxy_rejects_path_traversal(self):
        with TestClient(main.app) as client:
            response = client.get("/uploads/wallpapers/%2e%2e%2fsecret.jpg")
        # Starlette may reject the normalized path before it reaches the
        # endpoint (404); the endpoint itself rejects an unsafe filename (400).
        self.assertIn(response.status_code, {400, 404})

    def test_protocol_rejects_wrong_codec_or_version(self):
        invalid_messages = [
            {"type": "hello", "version": 2, "audio_params": {}},
            {
                "type": "hello",
                "version": 3,
                "audio_params": {
                    "codec": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            },
        ]
        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(ValueError):
                validate_dom_hello(message)

    def test_manchester_united_endpoint_returns_next_fixture(self):
        fixture = {
            "team": "Manchester United",
            "stale": False,
            "next_match": {
                "home_team": "Manchester United",
                "away_team": "Arsenal",
                "local_date": "30/08/2026",
                "local_time": "22:30",
            },
        }
        with patch.object(
            main.football_service, "get_schedule", AsyncMock(return_value=fixture)
        ):
            with TestClient(main.app) as client:
                response = client.get("/api/football/manchester-united")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["next_match"]["away_team"], "Arsenal")

    def test_manchester_united_endpoint_reports_provider_failure(self):
        with patch.object(
            main.football_service,
            "get_schedule",
            AsyncMock(side_effect=RuntimeError("unavailable")),
        ):
            with TestClient(main.app) as client:
                response = client.get("/api/football/manchester-united")

        self.assertEqual(response.status_code, 503)

    def test_manchester_united_background_is_jpeg(self):
        with patch.object(
            main.football_service,
            "get_background_jpeg",
            AsyncMock(return_value=b"jpeg-data"),
        ):
            with TestClient(main.app) as client:
                response = client.get("/api/football/manchester-united/background.jpg")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")

    def test_manchester_united_background_matches_lcd_resolution(self):
        source = BytesIO()
        Image.new("RGBA", (32, 32), (218, 41, 28, 255)).save(source, format="PNG")

        rendered = FootballService._render_background(source.getvalue())
        with Image.open(BytesIO(rendered)) as background:
            self.assertEqual(background.format, "JPEG")
            self.assertEqual(background.size, (320, 240))

    def test_codex_usage_endpoint_returns_normalized_snapshot(self):
        snapshot = {
            "five_hour": {"remaining_percent": 82, "resets_label": "03/09 21:13"},
            "weekly": {"remaining_percent": 97, "resets_label": "10/09 16:13"},
            "full_reset": {
                "available": True,
                "title": "Full reset (Weekly + 5 hr)",
                "expires_label": "21/09 05:00 GMT+07:00",
            },
            "updated_at": 1,
        }
        with patch.object(
            main.codex_usage_service, "get_usage", AsyncMock(return_value=snapshot)
        ):
            with TestClient(main.app) as client:
                response = client.get("/api/codex/usage")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["five_hour"]["remaining_percent"], 82)

    def test_codex_usage_sync_requires_bearer_token(self):
        with patch.object(main.settings, "CODEX_USAGE_SYNC_TOKEN", "sync-secret"):
            with TestClient(main.app) as client:
                response = client.post("/api/codex/usage/sync", json={})
        self.assertEqual(response.status_code, 401)

    def test_codex_usage_normalizes_five_hour_weekly_and_reset_credit(self):
        raw = {
            "rateLimitsByLimitId": {
                "codex": {
                    "planType": "plus",
                    "primary": {
                        "usedPercent": 18,
                        "windowDurationMins": 300,
                        "resetsAt": 1788444780,
                    },
                    "secondary": {
                        "usedPercent": 3,
                        "windowDurationMins": 10080,
                        "resetsAt": 1789031580,
                    },
                }
            },
            "rateLimitResetCredits": {
                "credits": [
                    {
                        "status": "available",
                        "title": "Full reset (Weekly + 5 hr)",
                        "expiresAt": 1789941600,
                    }
                ]
            },
        }

        snapshot = CodexUsageService.normalize(raw)

        self.assertEqual(snapshot["five_hour"]["remaining_percent"], 82)
        self.assertEqual(snapshot["weekly"]["remaining_percent"], 97)
        self.assertTrue(snapshot["full_reset"]["available"])
        self.assertEqual(snapshot["full_reset"]["title"], "Full reset (Weekly + 5 hr)")


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_add_remove_is_idempotent(self):
        registry = VoiceRegistry()
        session = VoiceSession(FakeWebSocket(), "board-1", "session-1")
        await registry.add(session)
        await registry.add(session)
        self.assertEqual(registry.count, 1)
        self.assertIs(await registry.get("board-1"), session)
        await registry.remove("session-1")
        self.assertEqual(registry.count, 0)

    async def test_listening_and_wake_states_only_emit_status_messages(self):
        websocket = FakeWebSocket()
        session = VoiceSession(websocket, "board", "session")

        await session.set_listening(notify_board=True)
        self.assertEqual(session.state, "LISTENING")
        self.assertEqual(websocket.text_messages[0]["state"], "start")
        self.assertEqual(websocket.text_messages[1]["emotion"], "listening")

        await session.set_wake_word(notify_board=True)
        self.assertEqual(session.state, "WAKE_WORD")
        self.assertEqual(websocket.text_messages[-2]["state"], "wake")
        self.assertEqual(websocket.text_messages[-1]["emotion"], "idle")

    async def test_vad_ignores_silence_then_starts_after_speech_and_silence(self):
        websocket = FakeWebSocket()
        session = VoiceSession(websocket, "board", "session")
        session.state = "LISTENING"
        loud = array("h", ([-1200, 1200] * (PCM_FRAME_BYTES // 4))).tobytes()
        quiet = bytes(PCM_FRAME_BYTES)
        session.start_pipeline = AsyncMock()

        for _ in range(5):
            await session.consume_audio(quiet)
        self.assertFalse(session.speech_started)
        for _ in range(3):
            await session.consume_audio(loud)
        for _ in range(9):
            await session.consume_audio(quiet)

        self.assertTrue(session.speech_started)
        session.start_pipeline.assert_awaited_once()

    async def test_abort_stops_tts_and_returns_to_wake_mode(self):
        websocket = FakeWebSocket()
        session = VoiceSession(websocket, "board", "session")
        session.state = "SPEAKING"

        await session.abort()

        self.assertEqual(session.state, "WAKE_WORD")
        self.assertEqual(websocket.text_messages[0]["type"], "tts")
        self.assertEqual(websocket.text_messages[0]["state"], "stop")
        self.assertEqual(websocket.text_messages[-2]["state"], "wake")

    async def test_openai_stt_requires_api_key_without_sending_audio(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        with patch.object(settings, "OPENAI_API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                await session._transcribe_openai(bytes(PCM_FRAME_BYTES), "vi-VN")

    async def test_llm_falls_back_only_when_openai_credit_is_exhausted(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._openai = AsyncMock(side_effect=OpenAIAPIError(
            "chat", 429, "insufficient_quota", "insufficient_quota",
            "You have no credits remaining", "req_quota",
        ))
        session._openrouter = AsyncMock(return_value={"choices": [{"message": {"content": "ok"}}]})
        with (
            patch.object(settings, "LLM_PROVIDER_ORDER", "openai,openrouter"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
        ):
            response = await session._llm({"messages": []})

        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertEqual(session._openrouter.await_args.args[0]["model"], settings.OPENROUTER_MODEL)
        self.assertEqual(session.last_llm_provider, "openrouter")
        self.assertEqual(session.last_llm_model, settings.OPENROUTER_MODEL)
        self.assertFalse(session.provider_ready("openai-llm-quota"))

    async def test_llm_does_not_fallback_on_temporary_openai_rate_limit(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        error = OpenAIAPIError(
            "chat", 429, "rate_limit_error", "rate_limit_exceeded",
            "Too many requests", "req_rate",
        )
        session._openai = AsyncMock(side_effect=error)
        session._openrouter = AsyncMock()
        with (
            patch.object(settings, "LLM_PROVIDER_ORDER", "openai,openrouter"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
        ):
            with self.assertRaises(OpenAIAPIError) as raised:
                await session._llm({"messages": []})

        self.assertEqual(raised.exception.request_id, "req_rate")
        session._openrouter.assert_not_awaited()

    async def test_llm_prefers_openai_and_records_actual_model(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._openai = AsyncMock(return_value={"choices": [{"message": {"content": "xin chào"}}]})
        session._openrouter = AsyncMock()
        with (
            patch.object(settings, "LLM_PROVIDER_ORDER", "openai,openrouter"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
        ):
            response = await session._llm({"messages": []})

        self.assertEqual(response["choices"][0]["message"]["content"], "xin chào")
        self.assertEqual(session.last_llm_provider, "openai")
        self.assertEqual(session.last_llm_model, settings.OPENAI_MODEL)
        session._openrouter.assert_not_awaited()

    async def test_confirmed_quota_circuit_avoids_repeating_failed_openai_calls(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session.defer_provider("openai-llm-quota")
        session._openai = AsyncMock()
        session._openrouter = AsyncMock(
            return_value={"choices": [{"message": {"content": "fallback"}}]}
        )
        with (
            patch.object(settings, "LLM_PROVIDER_ORDER", "openai,openrouter"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
        ):
            response = await session._llm({"messages": []})

        self.assertEqual(response["choices"][0]["message"]["content"], "fallback")
        session._openai.assert_not_awaited()
        session._openrouter.assert_awaited_once()

    async def test_openai_stt_failure_does_not_disable_openai_llm(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._transcribe_openai = AsyncMock(side_effect=RuntimeError("rate limited"))
        session._transcribe_openrouter = AsyncMock()
        session._transcribe_google = AsyncMock(return_value="Hey Dom")
        session._openai = AsyncMock(return_value={"choices": [{"message": {"content": "ok"}}]})
        session._openrouter = AsyncMock()
        with (
            patch.object(settings, "STT_PROVIDER", "openai"),
            patch.object(settings, "LLM_PROVIDER_ORDER", "openai,openrouter"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
        ):
            transcript = await session.transcribe(bytes(PCM_FRAME_BYTES))
            await session._llm({"messages": []})

        self.assertEqual(transcript, "Hey Dom")
        session._transcribe_google.assert_awaited_once()
        session._transcribe_openrouter.assert_not_awaited()
        session._openai.assert_awaited_once()
        session._openrouter.assert_not_awaited()

    async def test_openai_stt_is_preferred_when_it_recognizes_speech(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._transcribe_openai = AsyncMock(return_value="tăng âm lượng")
        session._transcribe_google = AsyncMock()
        session._transcribe_openrouter = AsyncMock()
        with (
            patch.object(settings, "STT_PROVIDER", "openai"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
            patch.object(settings, "STT_OPENROUTER_FALLBACK", False),
        ):
            transcript = await session.transcribe(bytes(PCM_FRAME_BYTES))

        self.assertEqual(transcript, "tăng âm lượng")
        session._transcribe_openai.assert_awaited_once()
        session._transcribe_google.assert_not_awaited()
        session._transcribe_openrouter.assert_not_awaited()

    async def test_openai_key_overrides_stale_google_deployment_setting(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._transcribe_openai = AsyncMock(return_value="xin chào")
        session._transcribe_google = AsyncMock()
        with (
            patch.object(settings, "STT_PROVIDER", "google-web"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "STT_OPENROUTER_FALLBACK", False),
        ):
            transcript = await session.transcribe(bytes(PCM_FRAME_BYTES))

        self.assertEqual(transcript, "xin chào")
        session._transcribe_openai.assert_awaited_once()
        session._transcribe_google.assert_not_awaited()

    async def test_empty_openai_transcript_falls_back_to_google(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._transcribe_openai = AsyncMock(return_value="")
        session._transcribe_google = AsyncMock(return_value="mở đồng hồ")
        session._transcribe_openrouter = AsyncMock()
        with (
            patch.object(settings, "STT_PROVIDER", "openai"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
            patch.object(settings, "STT_OPENROUTER_FALLBACK", False),
        ):
            transcript = await session.transcribe(bytes(PCM_FRAME_BYTES))

        self.assertEqual(transcript, "mở đồng hồ")
        session._transcribe_openai.assert_awaited_once()
        session._transcribe_google.assert_awaited_once()
        session._transcribe_openrouter.assert_not_awaited()

    async def test_openai_stt_quota_enables_openrouter_audio_fallback(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._transcribe_openai = AsyncMock(side_effect=OpenAIAPIError(
            "STT", 429, "insufficient_quota", "insufficient_quota",
            "You have no credits remaining", "req-stt-quota",
        ))
        session._transcribe_google = AsyncMock(return_value="")
        session._transcribe_openrouter = AsyncMock(return_value="Hey Dom")
        with (
            patch.object(settings, "STT_PROVIDER", "openai"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
            patch.object(settings, "STT_OPENROUTER_FALLBACK", False),
        ):
            transcript = await session.transcribe(bytes(PCM_FRAME_BYTES))

        self.assertEqual(transcript, "Hey Dom")
        session._transcribe_openai.assert_awaited_once()
        session._transcribe_google.assert_awaited_once()
        session._transcribe_openrouter.assert_awaited_once()
        self.assertFalse(session.provider_ready("openai-stt-quota"))

    async def test_quota_fallback_preserves_learned_bilingual_wake_signature(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")
        session._transcribe_openai = AsyncMock(side_effect=OpenAIAPIError(
            "STT", 429, "insufficient_quota", "insufficient_quota",
            "You have no credits remaining", "req-stt-quota",
        ))

        async def recognize_google(_pcm, language, **_kwargs):
            return "huy động" if language == "vi-VN" else "how you doing"

        session._transcribe_google = AsyncMock(side_effect=recognize_google)
        session._transcribe_openrouter = AsyncMock()
        with (
            patch.object(settings, "STT_PROVIDER", "openai"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "OPENROUTER_API_KEY", "openrouter-test"),
            patch.object(settings, "STT_OPENROUTER_FALLBACK", False),
        ):
            transcript = await session.transcribe(bytes(PCM_FRAME_BYTES), timeout=0.2)

        self.assertEqual(transcript, "Hey Dom")
        session._transcribe_openrouter.assert_not_awaited()

    async def test_wake_stt_timeout_does_not_disable_command_openai_stt(self):
        session = VoiceSession(FakeWebSocket(), "board", "session")

        async def never_finishes(*_args, **_kwargs):
            await asyncio.Event().wait()

        session._transcribe_openai = AsyncMock(side_effect=never_finishes)
        session._transcribe_google = AsyncMock(return_value="Hey Dom")
        with (
            patch.object(settings, "STT_PROVIDER", "openai"),
            patch.object(settings, "OPENAI_API_KEY", "openai-test"),
            patch.object(settings, "STT_OPENROUTER_FALLBACK", False),
        ):
            transcript = await session.transcribe(bytes(PCM_FRAME_BYTES), timeout=0.01)

        self.assertEqual(transcript, "Hey Dom")
        self.assertFalse(session.provider_ready("openai-wake-stt"))
        self.assertTrue(session.provider_ready("openai-stt"))

    async def test_unrecognized_command_is_spoken_and_returns_to_wake_mode(self):
        websocket = FakeWebSocket()
        session = VoiceSession(websocket, "board", "session")
        session.state = "PROCESSING"
        session.transcribe = AsyncMock(return_value="")
        session.speak = AsyncMock()

        await session.run_pipeline(bytes(PCM_FRAME_BYTES))

        session.speak.assert_awaited_once_with(NO_SPEECH_RESPONSE)
        self.assertTrue(
            any(
                message.get("type") == "llm"
                and message.get("emotion") == "sad"
                and message.get("text") == NO_SPEECH_RESPONSE
                for message in websocket.text_messages
            )
        )
        self.assertEqual(session.state, "WAKE_WORD")


if __name__ == "__main__":
    unittest.main()

import time
import json
import unittest
from array import array
from unittest.mock import AsyncMock, patch

from services.openrouter_voice_service import PCM_FRAME_BYTES, VoiceSession, handle_openrouter_voice
from config import settings


class DeviceVadTests(unittest.IsolatedAsyncioTestCase):
    async def test_hello_only_negotiates_boolean_true(self):
        for feature, expected in ((True, True), (False, False), ("true", False), (None, False)):
            with self.subTest(feature=feature):
                ws = AsyncMock()
                ws.headers = {"device-id": "vad-test"}
                ws.receive_text.return_value = json.dumps({
                    "type": "hello", "version": 3, "features": {"local_vad": feature},
                    "audio_params": {"codec": "pcm", "sample_rate": 16000,
                                     "channels": 1, "frame_duration": 60},
                })
                ws.receive.return_value = {"type": "websocket.disconnect"}
                with patch.object(settings, "VOICE_AUTH_TOKEN", ""):
                    await handle_openrouter_voice(ws)
                hello = json.loads(ws.send_text.await_args_list[0].args[0])
                self.assertIs(hello["features"]["local_vad"], expected)

    def session(self, local):
        session = VoiceSession(None, "vad-test", "test")
        session.state = "LISTENING"
        session.local_vad = local
        session.speech_started = True
        session.speech_started_at = time.monotonic()
        session.start_pipeline = AsyncMock()
        return session

    async def feed_utterance(self, session):
        speech = array("h", [800, -800] * (PCM_FRAME_BYTES // 4)).tobytes()
        for _ in range(10):
            await session.consume_audio(speech)
        for _ in range(35):
            await session.consume_audio(bytes(PCM_FRAME_BYTES))

    async def test_negotiated_vad_waits_for_device_stop(self):
        session = self.session(True)
        await self.feed_utterance(session)
        session.start_pipeline.assert_not_awaited()
        self.assertEqual(len(session.audio), 45 * PCM_FRAME_BYTES)

    async def test_old_firmware_retains_gateway_vad(self):
        session = self.session(False)
        await self.feed_utterance(session)
        self.assertGreater(session.start_pipeline.await_count, 0)

    async def test_local_vad_still_has_hard_capture_limit(self):
        session = self.session(True)
        session.speech_frames = 10
        session.speech_started_at -= 21
        await session.consume_audio(bytes(PCM_FRAME_BYTES))
        session.start_pipeline.assert_awaited_once()

    async def test_device_stop_accepts_quiet_neural_vad_speech(self):
        session = VoiceSession(None, "vad-test", "test")
        session.state = "LISTENING"
        session.local_vad = True
        session.audio.extend(array("h", [40, -40] * (3 * PCM_FRAME_BYTES // 4)).tobytes())
        session.send_json = AsyncMock()
        session.run_pipeline = AsyncMock()
        await session.start_pipeline()
        self.assertIsNotNone(session.pipeline_task)
        task = session.pipeline_task
        await task
        session.run_pipeline.assert_awaited_once()

    async def test_empty_stop_never_calls_stt(self):
        session = VoiceSession(None, "vad-test", "test")
        session.state = "LISTENING"
        session.local_vad = True
        await session.start_pipeline()
        self.assertIsNone(session.pipeline_task)

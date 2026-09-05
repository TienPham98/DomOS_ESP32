import asyncio
import json
import unittest
from array import array
from unittest.mock import AsyncMock, patch

from config import settings
from services.openrouter_voice_service import PCM_FRAME_BYTES, VoiceSession, split_wake_word


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_text(self, text):
        self.messages.append(json.loads(text))


class WakeWordTests(unittest.TestCase):
    def test_requested_words_match_at_start_and_keep_command(self):
        for text, command in (
            ("Hey Dom", ""), ("hey!", ""), ("Dom", ""),
            ("Hey, kiểm tra Codex Credit", "kiểm tra Codex Credit"),
            ("Dom, kiểm tra lịch thi đấu bóng đá", "kiểm tra lịch thi đấu bóng đá"),
            ("Hey Dom mở đồng hồ", "mở đồng hồ"),
        ):
            with self.subTest(text=text):
                self.assertEqual(split_wake_word(text), (True, command))

    def test_substrings_mid_sentence_and_old_false_positives_do_not_wake(self):
        for text in ("Heyday", "Domino", "they", "don't", "mình gọi Dom",
                     "Hello", "huy động", "he does", "hành động"):
            with self.subTest(text=text):
                self.assertEqual(split_wake_word(text), (False, ""))


class BackgroundWakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.websocket = FakeWebSocket()
        self.session = VoiceSession(self.websocket, "test-board", "test-session")
        self.session.process_transcript = AsyncMock()
        self.provider_patch = patch.object(settings, "WAKE_STT_PROVIDER", "google-web")
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        self.language_patch = patch.object(settings, "STT_LANGUAGE", "vi-VN")
        self.language_patch.start()
        self.addCleanup(self.language_patch.stop)

    async def asyncTearDown(self):
        await self.session.set_wake_word()

    async def test_each_wake_word_foregrounds_assistant_without_running_command(self):
        for word in ("Hey Dom", "Hey", "Dom"):
            with self.subTest(word=word):
                self.websocket.messages.clear()
                self.session.transcribe_wake_google = AsyncMock(return_value=[("vi-VN", word)])
                await self.session.run_wake_check(bytes(PCM_FRAME_BYTES))
                self.assertEqual(self.websocket.messages[0], {
                    "type": "listen", "state": "start", "source": "wake_word",
                    "session_id": "test-session",
                })
                self.assertEqual(self.session.state, "LISTENING")
                self.session.process_transcript.assert_not_awaited()

    async def test_combined_command_foregrounds_before_tool_pipeline(self):
        self.session.transcribe_wake_google = AsyncMock(return_value=[
            ("vi-VN", "Hey Dom kiểm tra codex credit"),
        ])

        observed = []

        async def check_order(command):
            observed.append((command, self.websocket.messages.copy()))

        self.session.process_transcript.side_effect = check_order
        await self.session.run_wake_check(bytes(PCM_FRAME_BYTES))
        self.session.process_transcript.assert_awaited_once()
        self.assertEqual(observed[0][0], "kiểm tra codex credit")
        self.assertEqual(observed[0][1][0], {
            "type": "listen", "state": "processing", "source": "wake_word",
            "session_id": "test-session",
        })
        self.assertEqual(self.session.state, "WAKE_WORD")

    async def test_rejected_noise_does_not_foreground_assistant(self):
        self.session.transcribe_wake_google = AsyncMock(return_value=[("vi-VN", "huy động")])
        await self.session.run_wake_check(bytes(PCM_FRAME_BYTES))
        self.assertFalse(any(message.get("source") == "wake_word" for message in self.websocket.messages))
        self.session.process_transcript.assert_not_awaited()
        self.assertEqual(self.session.state, "WAKE_WORD")

    async def test_observed_owner_pronunciation_signature_foregrounds_assistant(self):
        self.session.transcribe_wake_google = AsyncMock(return_value=[
            ("vi-VN", "huy động"),
            ("en-US", "how you doing"),
        ])

        await self.session.run_wake_check(bytes(PCM_FRAME_BYTES))

        self.assertEqual(self.websocket.messages[0], {
            "type": "listen", "state": "start", "source": "wake_word",
            "session_id": "test-session",
        })
        self.assertEqual(self.session.state, "LISTENING")
        self.session.process_transcript.assert_not_awaited()

    async def test_manual_listen_and_return_to_wake_do_not_request_foreground(self):
        await self.session.set_listening(notify_board=True)
        await self.session.set_wake_word(notify_board=True)
        self.assertFalse(any("source" in message for message in self.websocket.messages))

    async def test_fast_primary_does_not_wait_for_slow_secondary(self):
        async def recognize(pcm, language, **kwargs):
            if language == "vi-VN":
                return "Hey Dom"
            await asyncio.Event().wait()

        self.session._transcribe_google = AsyncMock(side_effect=recognize)
        results = await asyncio.wait_for(self.session.transcribe_wake_google(b""), 0.5)
        self.assertIn(("vi-VN", "Hey Dom"), results)

    async def test_fast_secondary_has_bounded_primary_grace(self):
        async def recognize(pcm, language, **kwargs):
            if language == "en-US":
                return "Hey Dom"
            await asyncio.Event().wait()

        self.session._transcribe_google = AsyncMock(side_effect=recognize)
        results = await asyncio.wait_for(self.session.transcribe_wake_google(b""), 0.6)
        self.assertIn(("en-US", "Hey Dom"), results)

    async def test_primary_command_wins_within_grace_period(self):
        async def recognize(pcm, language, **kwargs):
            if language == "en-US":
                return "Hey Dom"
            await asyncio.sleep(0.01)
            return "Hey Dom mở Codex"

        self.session._transcribe_google = AsyncMock(side_effect=recognize)
        results = await self.session.transcribe_wake_google(b"")
        self.assertEqual(results[0], ("vi-VN", "Hey Dom mở Codex"))

    async def test_unresponsive_wake_providers_are_bounded(self):
        async def never_returns(*args, **kwargs):
            await asyncio.Event().wait()

        self.session._transcribe_google = AsyncMock(side_effect=never_returns)
        with patch.object(settings, "WAKE_STT_TIMEOUT_SEC", 0.02):
            results = await asyncio.wait_for(self.session.transcribe_wake_google(b""), 0.5)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not text for _, text in results))

    async def test_wake_does_not_wait_for_command_provider_fallbacks(self):
        self.session.transcribe = AsyncMock()
        self.session.transcribe_wake_google = AsyncMock(return_value=[("vi-VN", "Dom")])
        with patch.object(settings, "STT_PROVIDER", "openai"):
            await self.session.run_wake_check(bytes(PCM_FRAME_BYTES))
        self.session.transcribe.assert_not_awaited()

    async def test_configured_wake_provider_remains_selectable(self):
        self.session.transcribe = AsyncMock(return_value="Hey Dom")
        self.session.transcribe_wake_google = AsyncMock()
        with patch.object(settings, "WAKE_STT_PROVIDER", "configured"):
            await self.session.run_wake_check(bytes(PCM_FRAME_BYTES))
        self.session.transcribe.assert_awaited_once()
        self.session.transcribe_wake_google.assert_not_awaited()

    async def test_three_frame_single_word_is_not_discarded(self):
        self.session.state = "WAKE_WORD"
        self.session.start_pipeline = AsyncMock()
        quiet = bytes(PCM_FRAME_BYTES)
        loud = array("h", [-1200, 1200] * (PCM_FRAME_BYTES // 4)).tobytes()
        for _ in range(5):
            await self.session.consume_audio(quiet)
        for _ in range(3):
            await self.session.consume_audio(loud)
        for _ in range(9):
            await self.session.consume_audio(quiet)
        self.session.start_pipeline.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

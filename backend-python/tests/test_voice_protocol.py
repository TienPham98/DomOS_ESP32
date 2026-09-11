import asyncio
import json
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from services.conversation_store import ConversationStore
from services.openrouter_voice_service import LISTENING_VAD_MAX_THRESHOLD, PCM_FRAME_BYTES, TTS_JITTER_BUFFER_FRAMES, TTS_STOP_GRACE_FRAMES, VAD_ENERGY_THRESHOLD, VAD_MAX_PAUSE_SEC, VAD_PRE_ROLL_FRAMES, VAD_SILENCE_FRAMES, WAKE_VAD_MAX_THRESHOLD, VoiceSession, matches_device_wake_signature, normalize_wake_pcm, pcm_rms, pcm_signal_rms, pcm_to_wav, split_wake_word, validate_dom_hello, voice_heartbeat_loop
from services.text_normalization import plain_speech_text


class VoiceProtocolTests(unittest.TestCase):
    def test_model_markdown_is_converted_to_plain_spoken_text(self):
        source = """### Kết quả\n\n***\n- **Âm lượng** đã tăng.\n- Xem [chi tiết](https://example.invalid).\n```text\nKhông còn dấu sao\n```"""

        cleaned = plain_speech_text(source)

        self.assertEqual(
            cleaned,
            "Kết quả\n\nÂm lượng đã tăng.\nXem chi tiết.\nKhông còn dấu sao",
        )
        self.assertNotIn("*", cleaned)
        self.assertNotIn("```", cleaned)
        self.assertNotIn("https://", cleaned)

    def test_model_content_parts_are_joined_and_cleaned(self):
        cleaned = plain_speech_text([
            {"type": "text", "text": "**Xin chào**"},
            {"type": "text", "text": "Dom! ***"},
        ])
        self.assertEqual(cleaned, "Xin chào Dom!")

    def test_dom_protocol_remains_pcm_v3(self):
        validate_dom_hello({"type": "hello", "version": 3, "audio_params": {"codec": "pcm", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})

    def test_dom_protocol_accepts_xiaozhi_style_opus_uplink(self):
        validate_dom_hello({"type": "hello", "version": 3, "audio_params": {"codec": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})

    def test_vad_constants_match_sixty_millisecond_frames(self):
        self.assertEqual(PCM_FRAME_BYTES, 1920)
        self.assertEqual(VAD_ENERGY_THRESHOLD, 180)
        self.assertEqual(VAD_PRE_ROLL_FRAMES, 24)
        self.assertEqual(VAD_MAX_PAUSE_SEC, 2.0)
        self.assertEqual(LISTENING_VAD_MAX_THRESHOLD, 450)
        self.assertEqual(WAKE_VAD_MAX_THRESHOLD, 700)
        self.assertEqual(VAD_SILENCE_FRAMES, 9)
        self.assertEqual(pcm_rms(bytes(PCM_FRAME_BYTES)), 0)

    def test_tts_has_cloud_jitter_and_stop_tail_margin(self):
        self.assertGreaterEqual(TTS_JITTER_BUFFER_FRAMES, 4)
        self.assertGreaterEqual(TTS_STOP_GRACE_FRAMES, 1)

    def test_pcm_is_wrapped_as_standard_wav(self):
        wav = pcm_to_wav(bytes(PCM_FRAME_BYTES))
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")

    def test_quiet_wake_audio_is_normalized_and_padded(self):
        quiet = array("h", ([200, 600] * (PCM_FRAME_BYTES // 4))).tobytes()
        normalized = normalize_wake_pcm(quiet)
        self.assertGreater(pcm_signal_rms(normalized), pcm_signal_rms(quiet))
        self.assertEqual(len(normalized), len(quiet) + 16000)

    def test_signal_rms_ignores_microphone_dc_offset(self):
        dc = array("h", [900] * (PCM_FRAME_BYTES // 2)).tobytes()
        self.assertEqual(pcm_signal_rms(dc), 0)
        self.assertEqual(pcm_rms(dc), 900)

    def test_adaptive_vad_threshold_tracks_background_noise(self):
        session = VoiceSession(None, "board", "session")
        session.noise_samples.extend([220, 230, 240, 250, 260])
        self.assertGreater(session.vad_threshold(), VAD_ENERGY_THRESHOLD)
        self.assertLess(session.vad_threshold(), 500)

    def test_wake_word_accepts_supported_variants_and_extracts_command(self):
        self.assertEqual(split_wake_word("Hey Dom, tăng âm lượng"), (True, "tăng âm lượng"))
        self.assertEqual(split_wake_word("Hây Dom"), (True, ""))
        self.assertEqual(split_wake_word("Hello kể chuyện cười"), (False, ""))
        self.assertEqual(split_wake_word("hello Tom"), (False, ""))
        self.assertEqual(split_wake_word("Hey dog"), (True, "dog"))
        self.assertEqual(split_wake_word("he to"), (False, ""))
        self.assertEqual(split_wake_word("he do"), (False, ""))
        self.assertEqual(split_wake_word("He does"), (False, ""))
        self.assertEqual(split_wake_word("hey don't hey"), (True, "don't hey"))
        self.assertEqual(split_wake_word("Hey Don mở đồng hồ"), (True, "mở đồng hồ"))
        self.assertEqual(split_wake_word("huy động"), (False, ""))
        self.assertEqual(split_wake_word("Dom tăng độ sáng"), (True, "tăng độ sáng"))
        self.assertEqual(split_wake_word("xin chào Dom"), (False, ""))

    def test_bilingual_device_signature_requires_both_recognizers(self):
        self.assertTrue(matches_device_wake_signature("Hey Don", "Hây Dom"))
        self.assertTrue(matches_device_wake_signature("how you doing", "huy động"))
        self.assertTrue(matches_device_wake_signature("how you doing", "hình động"))
        self.assertTrue(matches_device_wake_signature("how you doing", "Hello"))
        self.assertTrue(matches_device_wake_signature("Hazel", "heyzo"))
        self.assertTrue(matches_device_wake_signature("are you down", "Huy Tâm"))
        self.assertFalse(matches_device_wake_signature("how you doing", "xin chào"))
        self.assertFalse(matches_device_wake_signature("good morning", "huy động"))
        self.assertFalse(matches_device_wake_signature("I don't", "hành động"))
        self.assertFalse(matches_device_wake_signature("are you done", "thầy chọn"))
        self.assertFalse(matches_device_wake_signature("good morning", "hình tròn"))


class BatchedPcmTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_websocket_payload_preserves_vad_frame_boundaries(self):
        session = VoiceSession(None, "board", "session")
        frame = bytes([1, 2]) * (PCM_FRAME_BYTES // 2)
        session._consume_audio_frame = AsyncMock()

        await session.consume_audio(frame * 4)

        self.assertEqual(session._consume_audio_frame.await_count, 4)
        self.assertTrue(all(
            call.args == (frame,)
            for call in session._consume_audio_frame.await_args_list
        ))


class ConversationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_and_tool_trace_are_persistent_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(str(Path(directory) / "test.db"))
            await store.initialize()
            turn_id = await store.create_turn("board", "tăng âm lượng", "openrouter", "openrouter/free")
            await store.add_tool_trace(turn_id, "speaker.adjust_volume", {"delta": 10}, {"isError": False}, 56, "success")
            await store.complete_turn(turn_id, "Âm lượng đã tăng rồi nhé!")
            turns = await store.list_turns("board")
            context = await store.recent_context("board")
            self.assertEqual(turns[0]["tool_calls"][0]["duration_ms"], 56)
            self.assertEqual(context[-1]["role"], "assistant")
            self.assertIn("đã tăng", context[-1]["content"])

    async def test_store_cleans_new_and_legacy_assistant_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "test.db")
            store = ConversationStore(path)
            await store.initialize()
            new_turn = await store.create_turn("board", "chào", "openai", "gpt-test")
            await store.complete_turn(new_turn, "*** **Xin chào** bạn nhé! ***")

            # Simulate a row written by an older gateway, then initialize again
            # to exercise the idempotent startup migration.
            legacy_turn = await store.create_turn("board", "cũ", "openrouter", "free")
            store._execute(
                "UPDATE conversation_turns SET assistant_text = ? WHERE id = ?",
                ("### Cũ\n- **Nội dung**", legacy_turn),
            )
            await ConversationStore(path).initialize()

            turns = await store.list_turns("board")
            values = {turn["id"]: turn["assistant_text"] for turn in turns}
            self.assertEqual(values[new_turn], "Xin chào bạn nhé!")
            self.assertEqual(values[legacy_turn], "Cũ\nNội dung")
            self.assertNotIn("*", " ".join(values.values()))


class VoiceHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_runs_independently_of_incoming_audio(self):
        class FakeWebSocket:
            def __init__(self):
                self.sent: list[str] = []
                self.sent_event = asyncio.Event()

            async def send_text(self, value: str):
                self.sent.append(value)
                self.sent_event.set()

        websocket = FakeWebSocket()
        session = VoiceSession(websocket, "board", "session")
        task = asyncio.create_task(voice_heartbeat_loop(session, interval_sec=0.01))

        await asyncio.wait_for(websocket.sent_event.wait(), timeout=0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(json.loads(websocket.sent[0])["type"], "ping")
        self.assertEqual(json.loads(websocket.sent[0])["session_id"], "session")


class TtsStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_first_audio_stops_producer(self):
        session = VoiceSession(None, "board", "cancel-test")
        session.send_json = AsyncMock()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def synthesize(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        session._synthesize_sentence_pcm = synthesize
        queue = asyncio.Queue()
        queue.put_nowait("hello")
        task = asyncio.create_task(session.speak_stream(queue))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        session.send_json.assert_not_awaited()

    async def test_provider_error_before_first_audio_does_not_leave_worker(self):
        session = VoiceSession(None, "board", "failure-test")
        session.send_json = AsyncMock()
        session._synthesize_sentence_pcm = AsyncMock(side_effect=RuntimeError("tts down"))
        queue = asyncio.Queue()
        queue.put_nowait("hello")
        with self.assertRaisesRegex(RuntimeError, "tts down"):
            await asyncio.wait_for(session.speak_stream(queue), 1)
        self.assertFalse(any(t.get_name() == "tts-producer-failure-test" for t in asyncio.all_tasks()))

    async def test_twenty_clauses_keep_constant_buffer_and_preserve_pcm(self):
        session = VoiceSession(None, "board", "clock-test")
        session.send_json = AsyncMock()
        clock = SimpleNamespace(now=100.0)
        sent = []

        async def sleep(delay):
            clock.now += delay

        async def send(frame):
            sent.append((clock.now, frame))

        session.send_bytes = send
        deadline = None
        frame = bytes([1, 2]) * (PCM_FRAME_BYTES // 2)
        with patch("services.openrouter_voice_service.time", SimpleNamespace(monotonic=lambda: clock.now)), \
             patch("services.openrouter_voice_service.asyncio.sleep", sleep):
            for _ in range(20):
                deadline = await session._send_synthesized_sentence("clause", frame * 8, deadline)
            await session._wait_for_tts_tail(deadline)
        self.assertEqual(len(sent), 160)
        self.assertTrue(all(data == frame for _, data in sent))
        for index, (at, _) in enumerate(sent):
            buffered_seconds = (index + 1) * 0.06 - (at - 100.0)
            self.assertLessEqual(buffered_seconds, TTS_JITTER_BUFFER_FRAMES * 0.06 + 0.001)
            self.assertGreater(buffered_seconds, 0)
        self.assertGreaterEqual(clock.now, 100.0 + 160 * 0.06)

    async def test_next_clause_is_synthesized_while_current_audio_plays(self):
        session = VoiceSession(None, "board", "session")
        session.send_json = AsyncMock()
        second_synthesis_started = asyncio.Event()

        async def synthesize(sentence: str):
            if sentence == "second":
                second_synthesis_started.set()
            return sentence, bytes(PCM_FRAME_BYTES)

        async def play(sentence: str, pcm: bytes, previous_deadline=None):
            if sentence == "first":
                await asyncio.wait_for(second_synthesis_started.wait(), timeout=0.1)

        session._synthesize_sentence_pcm = AsyncMock(side_effect=synthesize)
        session._send_synthesized_sentence = AsyncMock(side_effect=play)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        await queue.put("first")
        await queue.put("second")
        await queue.put(None)

        self.assertTrue(await session.speak_stream(queue))
        self.assertEqual(session._synthesize_sentence_pcm.await_count, 2)
        self.assertEqual(session._send_synthesized_sentence.await_count, 2)


if __name__ == "__main__":
    unittest.main()

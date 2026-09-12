import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from config import settings
from services.assistant_prompt import build_system_prompt
from services.conversation_store import ConversationStore
from services.openrouter_voice_service import VoiceSession, _gold_quote_answer
from services.text_normalization import (
    PunctuationChunker,
    filter_asr_transcript,
    normalize_tts_text,
)
from services.web_search_service import WebSearchService, _DuckDuckGoParser, needs_web_search


class FakeWebSocket:
    def __init__(self) -> None:
        self.text_messages = []
        self.binary_messages = []

    async def send_text(self, payload: str) -> None:
        self.text_messages.append(json.loads(payload))

    async def send_bytes(self, payload: bytes) -> None:
        self.binary_messages.append(payload)


class FakeStreamResponse:
    is_error = False

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class FakeStreamClient:
    def __init__(self, lines, captured):
        self.lines = lines
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, **kwargs):
        self.captured.update({"method": method, "url": url, **kwargs})
        return FakeStreamResponse(self.lines)


class FakeSearchClient:
    def __init__(self, captured):
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.captured.update({"url": url, **kwargs})

        class Response:
            text = (
                '<a class="result__a" href="https://example.com/gold">Giá vàng SJC</a>'
                '<a class="result__snippet">Mua 143 triệu, bán 146 triệu.</a>'
            )

            def raise_for_status(self):
                return None

        return Response()

    async def get(self, url, **kwargs):
        self.captured.update({"detail_url": url, "detail_kwargs": kwargs})

        class Response:
            text = "<main>Giá mua 143 triệu. Giá bán 146 triệu đồng một lượng.</main>"
            headers = {"content-type": "text/html; charset=utf-8"}

            def raise_for_status(self):
                return None

        return Response()


class VoiceIntelligenceUnitTests(unittest.TestCase):
    def test_gold_quote_uses_latest_complete_source(self):
        result = _gold_quote_answer([{
            "title": "Giá vàng SJC",
            "content": (
                "Cập nhật lúc 16:48 ngày 12/09/2026. "
                "Giá mua 143.000 nghìn đ/lượng. Giá bán 146.000 nghìn đ/lượng."
            ),
        }], datetime(2026, 9, 12, 17, 0, tzinfo=ZoneInfo("Asia/Bangkok")))

        self.assertEqual(
            result,
            "Hôm nay là ngày 12 tháng 9 năm 2026. Giá vàng miếng SJC mua vào "
            "143 triệu đồng và bán ra 146 triệu đồng một lượng, cập nhật lúc 16 giờ 48 phút.",
        )

    def test_dynamic_prompt_contains_context_and_voice_rules(self):
        now = datetime(2026, 9, 9, 6, 40, tzinfo=ZoneInfo("Asia/Bangkok"))
        with patch.object(settings, "ASSISTANT_LOCATION", "Hà Nội"):
            prompt = build_system_prompt(now)
        self.assertIn("06 giờ 40 phút", prompt)
        self.assertIn("09 tháng 09 năm 2026", prompt)
        self.assertIn("tại Hà Nội", prompt)
        self.assertIn("dưới 40 từ", prompt)
        self.assertIn("phải dùng web_search", prompt)

    def test_tts_normalizes_units_versions_loanwords_and_markup(self):
        normalized = normalize_tts_text(
            '**v1.0.2** chạy lúc 10h30, 25°C, 80%, 50km/h, 10W, 100k. '
            'WiFi reset 😊 (xong)...'
        )
        self.assertEqual(
            normalized,
            "phiên bản 1 chấm 0 chấm 2 chạy lúc 10 giờ 30 phút, 25 độ C, "
            "80 phần trăm, 50 ki-lô-mét trên giờ, 10 oát, 100 nghìn. "
            "oai-phai ri-xét xong.",
        )

    def test_asr_hallucinations_are_dropped_but_short_wake_words_survive(self):
        for text in (
            "Cảm ơn các bạn đã theo dõi",
            "Subtitles by cộng đồng",
            "ừm",
            "hello hello hello hello",
        ):
            with self.subTest(text=text):
                self.assertEqual(filter_asr_transcript(text), "")
        self.assertEqual(filter_asr_transcript("Hey"), "Hey")
        self.assertEqual(filter_asr_transcript("Dom"), "Dom")

    def test_punctuation_chunking_waits_for_boundaries_and_preserves_versions(self):
        chunker = PunctuationChunker()
        self.assertEqual(chunker.feed("Phiên bản v1.0.2 chạy ổn,"), [])
        self.assertEqual(
            chunker.feed(" phản hồi nhanh. Câu"),
            ["Phiên bản v1.0.2 chạy ổn,", "phản hồi nhanh."],
        )
        self.assertEqual(chunker.feed(" cuối"), [])
        self.assertEqual(chunker.flush(), "Câu cuối")

    def test_realtime_questions_are_routed_to_search(self):
        self.assertTrue(needs_web_search("Giá vàng hôm nay bao nhiêu?"))
        self.assertTrue(needs_web_search("Kết quả Manchester United tối qua"))
        self.assertFalse(needs_web_search("Giải thích giao thức MQTT"))

    def test_duckduckgo_html_parser_keeps_three_clean_fields(self):
        parser = _DuckDuckGoParser()
        parser.feed(
            '<div><a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">Tin mới</a>'
            '<a class="result__snippet">Tóm tắt kết quả.</a></div>'
        )
        self.assertEqual(parser.results, [{
            "title": "Tin mới",
            "url": "https://example.com",
            "snippet": "Tóm tắt kết quả.",
        }])


class VoiceIntelligenceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = VoiceSession(FakeWebSocket(), "board", "session")
        self.trace_patch = patch(
            "services.openrouter_voice_service.conversation_store.add_tool_trace",
            new_callable=AsyncMock,
        )
        self.trace = self.trace_patch.start()
        self.addCleanup(self.trace_patch.stop)

    async def test_time_and_hardware_fast_paths_do_not_call_llm(self):
        self.session._llm = AsyncMock()
        self.session.call_device_tool = AsyncMock(return_value={
            "content": [{"type": "text", "text": "speaker volume set to 80"}],
            "isError": False,
        })
        time_answer = await self.session.chat([], "Mấy giờ rồi", "turn-time")
        volume_answer = await self.session.chat([], "tăng âm lượng 10", "turn-volume")
        self.assertIn("giờ", time_answer)
        self.assertEqual(volume_answer, "Âm lượng hiện tại là 80.")
        self.session.call_device_tool.assert_awaited_once_with(
            "speaker.adjust_volume", {"delta": 10}
        )
        self.session._llm.assert_not_awaited()

    async def test_social_fast_paths_are_immediate_and_natural(self):
        self.session._llm = AsyncMock()

        greeting = await self.session.chat([], "Xin chào Dom", "turn-greeting")
        identity = await self.session.chat([], "Bạn là ai", "turn-identity")
        thanks = await self.session.chat([], "Cảm ơn Dom nhé", "turn-thanks")

        self.assertIn("Dom đây", greeting)
        self.assertIn("trợ lý giọng nói", identity)
        self.assertIn("Không có gì", thanks)
        self.session._llm.assert_not_awaited()

    async def test_general_chat_omits_tool_schemas_but_keeps_short_output_limit(self):
        self.session._llm = AsyncMock(return_value={
            "choices": [{"message": {"content": "Mình nghĩ đây là một ý hay."}}]
        })

        answer = await self.session.chat([], "Bạn nghĩ sao về việc đọc sách", "turn")

        self.assertEqual(answer, "Mình nghĩ đây là một ý hay.")
        payload = self.session._llm.await_args.args[0]
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(payload["max_tokens"], 180)

    async def test_word_fragment_does_not_trigger_device_tool_schema(self):
        self.session._llm = AsyncMock(return_value={
            "choices": [{"message": {"content": "Cá heo là loài động vật có vú."}}]
        })

        await self.session.chat([], "Cá heo là loài gì", "turn")

        self.assertNotIn("tools", self.session._llm.await_args.args[0])

    async def test_device_question_keeps_device_tools_available(self):
        self.session._llm = AsyncMock(return_value={
            "choices": [{"message": {"content": "Thiết bị đang hoạt động."}}]
        })

        await self.session.chat([], "Độ sáng hiện tại thế nào", "turn")

        payload = self.session._llm.await_args.args[0]
        names = {tool["function"]["name"] for tool in payload["tools"]}
        self.assertIn("display.set_brightness", names)
        self.assertIn("device.get_status", names)

    async def test_realtime_tool_executes_on_server_not_on_device(self):
        self.session.call_device_tool = AsyncMock()
        self.session._llm = AsyncMock(return_value={
            "choices": [{"message": {"content": "Hà Nội hôm nay có mưa nhẹ."}}]
        })
        search_result = {
            "query": "thời tiết Hà Nội hôm nay",
            "provider": "duckduckgo",
            "results": [{"title": "Dự báo", "url": "https://example.com", "snippet": "Mưa nhẹ"}],
        }
        with patch(
            "services.openrouter_voice_service.web_search_service.search",
            AsyncMock(return_value=search_result),
        ) as search:
            answer = await self.session.chat([], "Thời tiết Hà Nội hôm nay", "turn")
        self.assertEqual(answer, "Hà Nội hôm nay có mưa nhẹ.")
        search.assert_awaited_once()
        self.session.call_device_tool.assert_not_awaited()
        first_payload = self.session._llm.await_args_list[0].args[0]
        self.assertNotIn("tools", first_payload)
        self.assertIn("Mưa nhẹ", first_payload["messages"][-2]["content"])
        self.assertEqual(self.trace.await_args.args[-1], "success")

    async def test_duckduckgo_uses_html_form_post_instead_of_blocked_get(self):
        captured = {}
        with (
            patch.object(settings, "WEB_SEARCH_PROVIDER", "duckduckgo"),
            patch.object(settings, "WEB_SEARCH_BASE_URL", "https://example.invalid/search"),
            patch(
                "services.web_search_service.httpx.AsyncClient",
                return_value=FakeSearchClient(captured),
            ),
            patch("services.web_search_service.socket.getaddrinfo", return_value=[
                (None, None, None, None, ("93.184.216.34", 0)),
            ]),
        ):
            result = await WebSearchService().search("giá vàng hôm nay")

        self.assertEqual(captured["data"]["q"], "giá vàng hôm nay")
        self.assertEqual(captured["data"]["kl"], "vn-vi")
        self.assertEqual(result["results"][0]["snippet"], "Mua 143 triệu, bán 146 triệu.")
        self.assertIn("Giá mua 143 triệu", result["results"][0]["content"])

    async def test_realtime_answer_buffers_and_retries_spoken_tool_syntax(self):
        streamed = AsyncMock()
        self.session._llm = AsyncMock(side_effect=[
            {"choices": [{"message": {"content": "websearch query giá vàng hôm nay"}}]},
            {"choices": [{"message": {"content": "Vàng SJC mua 143 triệu, bán 146 triệu một lượng."}}]},
        ])
        search_result = {
            "query": "giá vàng",
            "provider": "duckduckgo",
            "results": [{
                "title": "Giá vàng SJC",
                "url": "https://example.com",
                "snippet": "Mua 143 triệu, bán 146 triệu đồng một lượng.",
            }],
        }
        with patch(
            "services.openrouter_voice_service.web_search_service.search",
            AsyncMock(return_value=search_result),
        ):
            answer = await self.session.chat(
                [], "Giá vàng hôm nay thế nào", "turn", on_text_delta=streamed
            )

        self.assertEqual(answer, "Vàng SJC mua 143 triệu, bán 146 triệu một lượng.")
        self.assertEqual(self.session._llm.await_count, 2)
        for invocation in self.session._llm.await_args_list:
            self.assertIsNone(invocation.kwargs["on_text_delta"])
        streamed.assert_not_awaited()

    async def test_pipeline_queues_first_clause_before_llm_finishes(self):
        released = asyncio.Event()
        received: list[str] = []

        async def fake_llm(_payload, on_text_delta=None):
            await on_text_delta("Câu đầu, ")
            await asyncio.wait_for(released.wait(), 1)
            await on_text_delta("câu thứ hai.")
            return {"choices": [{"message": {"content": "Câu đầu, câu thứ hai."}}]}

        async def consume(queue):
            first = await asyncio.wait_for(queue.get(), 1)
            received.append(first)
            released.set()
            while await queue.get() is not None:
                received.append("next")
            return True

        self.session._llm = AsyncMock(side_effect=fake_llm)
        self.session.speak_stream = AsyncMock(side_effect=consume)
        self.session.speak = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(str(Path(directory) / "stream.db"))
            await store.initialize()
            with patch("services.openrouter_voice_service.conversation_store", store):
                await self.session.process_transcript("kể ngắn gọn")
            turns = await store.list_turns("board")
        self.assertEqual(received[0], "Câu đầu,")
        self.session.speak.assert_not_awaited()
        self.assertEqual(turns[0]["assistant_text"], "Câu đầu, câu thứ hai.")

    async def test_sse_stream_reassembles_text_and_tool_argument_fragments(self):
        events = [
            {"choices": [{"delta": {"content": "Xin chào, "}}]},
            {"choices": [{"delta": {"content": "bạn."}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "speaker.", "arguments": '{"delta":'},
            }]}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"name": "adjust_volume", "arguments": "10}"},
            }]}, "finish_reason": "tool_calls"}]},
        ]
        lines = [f"data: {json.dumps(event)}" for event in events] + ["data: [DONE]"]
        captured = {}
        deltas = []

        async def collect(delta):
            deltas.append(delta)

        with patch(
            "services.openrouter_voice_service.httpx.AsyncClient",
            return_value=FakeStreamClient(lines, captured),
        ):
            response = await self.session._stream_completion(
                url="https://example.invalid/chat/completions",
                headers={"Authorization": "Bearer test"},
                payload={"messages": []},
                timeout=5,
                provider="openai",
                on_text_delta=collect,
            )
        message = response["choices"][0]["message"]
        self.assertEqual(message["content"], "Xin chào, bạn.")
        self.assertEqual(deltas, ["Xin chào, ", "bạn."])
        self.assertEqual(message["tool_calls"][0]["function"], {
            "name": "speaker.adjust_volume", "arguments": '{"delta":10}',
        })
        self.assertTrue(captured["json"]["stream"])
        self.assertEqual(captured["timeout"], 5)


if __name__ == "__main__":
    unittest.main()

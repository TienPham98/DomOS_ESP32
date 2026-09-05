import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.app_commands import (
    APP_COMMAND_HELP,
    APP_CONFIRMATIONS,
    APP_LAUNCH_FAILED,
    claims_app_launch,
    requested_app,
)
from services.conversation_store import ConversationStore
from services.openrouter_voice_service import VoiceSession


class AppCommandTests(unittest.TestCase):
    def test_check_shortcuts_open_requested_apps(self):
        for phrase, app in (
            ("kiểm tra lịch thi đấu bóng đá", "man-utd"),
            ("kiem tra lich thi dau bong da", "man-utd"),
            ("Hey Dom, kiểm tra lịch thi đấu bóng đá!", "man-utd"),
            ("kiểm tra lịch thi đấu bóng đá giúp mình nhé", "man-utd"),
            ("kiểm tra codex credit", "codex-credit"),
            ("kiem tra codex credit", "codex-credit"),
            ("Hey Dom, kiểm tra Codex Credit!", "codex-credit"),
            ("kiểm tra Codex Credit cho tôi", "codex-credit"),
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(requested_app(phrase), app)

    def test_codex_names_and_recorded_stt_miss(self):
        for phrase in (
            "mở ứng dụng codex credit", "MỞ APP CODEX CREDIT!",
            "mo ung dung codex credit", "mở Codex", "Codex Credit",
            "Codex usage", "mở app co dex credit", "code credit checking",
            "Tracking topic Credit", "mở Codex được không",
            "Hey Dom, mở Codex Credit", "chuyển sang app codex-credit",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(requested_app(phrase), "codex-credit")

    def test_existing_apps_remain_available(self):
        for phrase, app in (
            ("mở Manchester United", "man-utd"),
            ("xem MU thi đấu", "man-utd"),
            ("mở hình nền", "wallpaper"),
            ("app wallpaper", "wallpaper"),
            ("bật đồng hồ", "clock"),
            ("open clock", "clock"),
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(requested_app(phrase), app)

    def test_discussion_negation_and_ambiguous_words_do_not_launch(self):
        for phrase in (
            "Codex Credit là gì?", "đừng mở Codex Credit",
            "không mở ứng dụng Codex", "do not open codex credit",
            "hướng dẫn cách mở Codex Credit", "tại sao không mở được Codex?",
            "Manchester United thi đấu hôm nào?", "mở credit card",
            "Tracking Connect us", "tracking credit", "mở ứng dụng Connect",
            "mở đồng hồ và Codex Credit", "how to open codex credit",
            "đừng kiểm tra Codex Credit", "không kiểm tra Codex Credit",
            "đừng kiểm tra lịch thi đấu bóng đá",
            "kiểm tra lịch thi đấu bóng đá Liverpool",
            "lịch thi đấu bóng đá là gì?",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(requested_app(phrase))

    def test_detects_fabricated_app_confirmations_only(self):
        for answer in (
            "Đã mở ứng dụng codex-credit cho bạn!",
            "Mình đã mở Codex Credit rồi nha!",
            "Ứng dụng đồng hồ đã được mở.", "I opened the Codex app.",
        ):
            with self.subTest(answer=answer):
                self.assertTrue(claims_app_launch(answer))
        for answer in (
            "Codex Credit hiển thị hạn mức.", "Dom chưa mở được ứng dụng.",
            "Đã bật loa rồi nhé!", "Âm lượng đã tăng 10 rồi nhé!",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(claims_app_launch(answer))


class McpWebSocket:
    def __init__(self):
        self.messages = []
        self.request_sent = asyncio.Event()

    async def send_text(self, payload):
        message = json.loads(payload)
        self.messages.append(message)
        if message.get("type") == "mcp":
            self.request_sent.set()


class AppLaunchSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.websocket = McpWebSocket()
        self.session = VoiceSession(self.websocket, "test-board", "test-session")
        self.session._llm = AsyncMock()
        self.trace_patch = patch(
            "services.openrouter_voice_service.conversation_store.add_tool_trace",
            new_callable=AsyncMock,
        )
        self.trace = self.trace_patch.start()
        self.addCleanup(self.trace_patch.stop)

    async def test_known_commands_send_app_launch_without_llm(self):
        for phrase in ("mở ứng dụng codex credit", "Tracking topic Credit", "mở Codex"):
            with self.subTest(phrase=phrase):
                self.session.call_device_tool = AsyncMock(return_value={"isError": False})
                answer = await self.session.chat([], phrase, "turn")
                self.session.call_device_tool.assert_awaited_once_with(
                    "app.launch", {"app": "codex-credit"},
                )
                self.assertEqual(answer, APP_CONFIRMATIONS["codex-credit"])
                self.assertEqual(self.trace.await_args.args[-1], "success")
        self.session._llm.assert_not_awaited()

    async def test_check_commands_send_correct_app_launch_without_llm(self):
        for phrase, app in (
            ("kiểm tra lịch thi đấu bóng đá", "man-utd"),
            ("kiem tra lich thi dau bong da", "man-utd"),
            ("kiểm tra codex credit", "codex-credit"),
            ("kiem tra codex credit", "codex-credit"),
        ):
            with self.subTest(phrase=phrase):
                self.session.call_device_tool = AsyncMock(return_value={"isError": False})
                answer = await self.session.chat([], phrase, "turn")
                self.session.call_device_tool.assert_awaited_once_with(
                    "app.launch", {"app": app},
                )
                self.assertEqual(answer, APP_CONFIRMATIONS[app])
                self.assertEqual(self.trace.await_args.args[1:3], ("app.launch", {"app": app}))
                self.assertEqual(self.trace.await_args.args[-1], "success")
        self.session._llm.assert_not_awaited()

    async def test_direct_failure_never_confirms_launch(self):
        for failure in ({"isError": True}, {"error": "device offline"}, {}, None):
            with self.subTest(failure=failure):
                self.session.call_device_tool = AsyncMock(return_value=failure)
                answer = await self.session.chat([], "mở Codex", "turn")
                self.assertNotIn("Đã mở", answer)
                self.assertEqual(self.trace.await_args.args[-1], "error")
        self.session._llm.assert_not_awaited()

    async def test_timeout_does_not_claim_success(self):
        self.session.call_device_tool = AsyncMock(side_effect=TimeoutError("no ACK"))
        answer = await self.session.chat([], "mở Codex", "turn")
        self.assertNotIn("Đã mở", answer)
        self.assertEqual(self.trace.await_args.args[-1], "error")

    async def test_unknown_app_asks_for_name_without_claiming_success(self):
        self.session.call_device_tool = AsyncMock()
        answer = await self.session.chat([], "mở ứng dụng connect", "turn")
        self.assertEqual(answer, APP_COMMAND_HELP)
        self.session.call_device_tool.assert_not_awaited()
        self.session._llm.assert_not_awaited()

    async def test_no_tool_fabricated_success_is_suppressed(self):
        self.session._llm.return_value = {"choices": [{"message": {
            "content": "Đã mở ứng dụng codex-credit cho bạn!",
        }}]}
        self.session.call_device_tool = AsyncMock()
        answer = await self.session.chat([], "Tracking Connect us", "turn")
        self.assertEqual(answer, APP_LAUNCH_FAILED)
        self.session.call_device_tool.assert_not_awaited()
        self.trace.assert_not_awaited()

    async def test_normal_conversation_is_not_replaced(self):
        self.session._llm.return_value = {"choices": [{"message": {
            "content": "Codex Credit hiển thị hạn mức sử dụng.",
        }}]}
        self.session.call_device_tool = AsyncMock()
        answer = await self.session.chat([], "Codex Credit là gì?", "turn")
        self.assertEqual(answer, "Codex Credit hiển thị hạn mức sử dụng.")
        self.session.call_device_tool.assert_not_awaited()

    async def test_model_tool_launch_uses_device_result_not_model_confirmation(self):
        for result, expected in (
            ({"isError": False}, APP_CONFIRMATIONS["codex-credit"]),
            ({"isError": True}, APP_LAUNCH_FAILED),
        ):
            with self.subTest(result=result):
                self.session._llm.reset_mock()
                self.session._llm.return_value = {"choices": [{"message": {
                    "tool_calls": [{"id": "call1", "type": "function", "function": {
                        "name": "app.launch", "arguments": '{"app":"codex-credit"}',
                    }}],
                }}]}
                self.session.call_device_tool = AsyncMock(return_value=result)
                answer = await self.session.chat([], "cho mình xem hạn mức", "turn")
                self.assertEqual(answer, expected)
                self.session.call_device_tool.assert_awaited_once_with(
                    "app.launch", {"app": "codex-credit"},
                )
                self.session._llm.assert_awaited_once()

    async def test_malformed_model_arguments_do_not_reach_device(self):
        self.session._llm.return_value = {"choices": [{"message": {
            "tool_calls": [{"id": "call1", "type": "function", "function": {
                "name": "app.launch", "arguments": "[]",
            }}],
        }}]}
        self.session.call_device_tool = AsyncMock()
        answer = await self.session.chat([], "cho mình xem hạn mức", "turn")
        self.assertEqual(answer, APP_LAUNCH_FAILED)
        self.session.call_device_tool.assert_not_awaited()
        self.assertEqual(self.trace.await_args.args[-1], "error")

    async def test_volume_and_brightness_controls_keep_working(self):
        for phrase, tool in (
            ("tăng âm lượng", "speaker.adjust_volume"),
            ("tăng độ sáng", "display.adjust_brightness"),
        ):
            with self.subTest(phrase=phrase):
                self.session.call_device_tool = AsyncMock(return_value={"isError": False})
                answer = await self.session.chat([], phrase, "turn")
                self.session.call_device_tool.assert_awaited_once_with(tool, {"delta": 10})
                self.assertIn("10", answer)
        self.session._llm.assert_not_awaited()

    async def test_mcp_ack_required_before_speech_and_history_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(str(Path(directory) / "test.db"))
            await store.initialize()
            self.session.speak = AsyncMock()
            with patch("services.openrouter_voice_service.conversation_store", store):
                task = asyncio.create_task(self.session.process_transcript("Tracking topic Credit"))
                try:
                    await asyncio.wait_for(self.websocket.request_sent.wait(), 2)
                    self.assertFalse(task.done())
                    self.session.speak.assert_not_awaited()
                    request = self.websocket.messages[-1]["payload"]
                    self.assertEqual(request["method"], "tools/call")
                    self.assertEqual(request["params"], {
                        "name": "app.launch", "arguments": {"app": "codex-credit"},
                    })
                    self.session.resolve_mcp({
                        "jsonrpc": "2.0", "id": request["id"],
                        "result": {"content": [{"type": "text", "text": "launched app codex-credit"}],
                                   "isError": False},
                    })
                    await asyncio.wait_for(task, 2)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.session.speak.assert_awaited_once_with(APP_CONFIRMATIONS["codex-credit"])
            self.assertEqual(self.session.pending_mcp, {})
            turns = await store.list_turns("test-board")
            self.assertEqual(turns[0]["tool_calls"][0]["name"], "app.launch")
            self.assertEqual(turns[0]["tool_calls"][0]["status"], "success")
            self.assertEqual(turns[0]["assistant_text"], APP_CONFIRMATIONS["codex-credit"])

    async def test_json_rpc_error_does_not_confirm_and_cleans_pending_request(self):
        task = asyncio.create_task(self.session.chat([], "mở Codex", "turn"))
        try:
            await asyncio.wait_for(self.websocket.request_sent.wait(), 2)
            request = self.websocket.messages[-1]["payload"]
            self.session.resolve_mcp({
                "jsonrpc": "2.0", "id": request["id"],
                "error": {"code": -32603, "message": "app manager is not ready"},
            })
            answer = await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn("Đã mở", answer)
        self.assertEqual(self.session.pending_mcp, {})
        self.assertEqual(self.trace.await_args.args[-1], "error")


if __name__ == "__main__":
    unittest.main()

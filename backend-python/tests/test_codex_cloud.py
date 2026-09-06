import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cryptography.fernet import Fernet
import psycopg

from config import settings
from services.codex_auth_store import (
    CodexAuthError, checked_auth, cli_environment, codex_environment, write_private_auth,
)
from services.codex_usage_service import CodexUsageError, CodexUsageService


def auth_payload(token="test-access-token"):
    return json.dumps({"tokens": {"access_token": token, "refresh_token": "test-refresh"}}).encode()


class CodexEnvironmentTests(unittest.TestCase):
    def test_only_required_environment_reaches_cli(self):
        with patch.dict(os.environ, {"PATH": "bin", "OPENAI_API_KEY": "secret",
                                    "DATABASE_URL": "secret", "MQTT_PASSWORD": "secret"}, clear=True), \
             patch.object(settings, "CODEX_USAGE_AUTH_DIR", ""):
            self.assertEqual(cli_environment(), {"PATH": "bin"})

    def test_auth_requires_chatgpt_tokens(self):
        for payload in (b"{}", b"[]", b"bad-json", b'{"OPENAI_API_KEY":"secret"}', b"x" * 65537):
            with self.subTest(payload=payload[:20]), self.assertRaises(CodexAuthError):
                checked_auth(payload)
        self.assertEqual(checked_auth(auth_payload()), auth_payload())

    def test_private_auth_write_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            write_private_auth(path, auth_payload())
            self.assertEqual(path.read_bytes(), auth_payload())
            self.assertFalse(path.with_suffix(".tmp").exists())


class CodexAuthPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.cipher = Fernet(Fernet.generate_key())
        self.key = Fernet.generate_key()
        self.cipher = Fernet(self.key)
        self.auth = Path(self.directory) / "auth.json"
        for name, value in {
            "CODEX_USAGE_AUTH_DIR": self.directory,
            "CODEX_USAGE_AUTH_ENCRYPTION_KEY": self.key.decode(),
            "DATABASE_URL": "postgresql://test.invalid/test",
        }.items():
            self.stack.enter_context(patch.object(settings, name, value))
        self.cursor = Mock(fetchone=AsyncMock(return_value=None))
        self.connection = Mock(execute=AsyncMock(return_value=self.cursor),
                               commit=AsyncMock(), close=AsyncMock())
        self.connect = self.stack.enter_context(patch(
            "psycopg.AsyncConnection.connect", AsyncMock(return_value=self.connection)))

    def stored_ciphertext(self):
        return next(call.args[1][0] for call in self.connection.execute.call_args_list
                    if call.args[0].startswith("INSERT INTO"))

    async def test_new_login_is_encrypted_and_isolated(self):
        previous = os.environ.get("CODEX_HOME")
        async with codex_environment() as env:
            self.assertEqual(Path(env["CODEX_HOME"]), Path(self.directory))
            write_private_auth(self.auth, auth_payload())
        encrypted = self.stored_ciphertext()
        self.assertNotIn(b"test-access-token", encrypted)
        self.assertEqual(self.cipher.decrypt(encrypted), auth_payload())
        self.assertEqual(os.environ.get("CODEX_HOME"), previous)
        queries = [call.args[0] for call in self.connection.execute.call_args_list]
        self.assertLess(next(i for i, q in enumerate(queries) if "pg_advisory_xact_lock" in q),
                        next(i for i, q in enumerate(queries) if q.startswith("SELECT ciphertext")))
        self.connection.close.assert_awaited_once()

    async def test_restores_and_persists_rotated_token_after_failed_read(self):
        self.cursor.fetchone.return_value = (self.cipher.encrypt(auth_payload()),)
        with self.assertRaisesRegex(CodexUsageError, "quota unavailable"):
            async with codex_environment():
                self.assertEqual(self.auth.read_bytes(), auth_payload())
                write_private_auth(self.auth, auth_payload("rotated"))
                raise CodexUsageError("quota unavailable")
        self.assertEqual(self.cipher.decrypt(self.stored_ciphertext()), auth_payload("rotated"))
        self.assertEqual(self.connection.commit.await_count, 2)

    async def test_logout_deletes_only_single_auth_row(self):
        self.cursor.fetchone.return_value = (self.cipher.encrypt(auth_payload()),)
        async with codex_environment():
            self.auth.unlink()
        self.connection.execute.assert_any_await("DELETE FROM domos_codex_auth WHERE id = 1")

    async def test_missing_database_row_cannot_resurrect_stale_pod_login(self):
        write_private_auth(self.auth, auth_payload())
        async with codex_environment():
            self.assertFalse(self.auth.exists())
        self.assertFalse(any(call.args[0].startswith("INSERT INTO")
                             for call in self.connection.execute.call_args_list))

    async def test_wrong_encryption_key_fails_closed_without_overwriting(self):
        self.cursor.fetchone.return_value = (Fernet(Fernet.generate_key()).encrypt(auth_payload()),)
        with self.assertRaisesRegex(CodexAuthError, "storage is unavailable"):
            async with codex_environment():
                self.fail("Must not invoke CLI with undecryptable credentials")
        self.assertFalse(self.auth.exists())
        self.connection.close.assert_awaited_once()

    async def test_database_errors_are_redacted(self):
        self.connect.side_effect = psycopg.OperationalError("password=do-not-print")
        with self.assertRaises(CodexAuthError) as caught:
            async with codex_environment():
                self.fail("Database failure must stop the CLI")
        self.assertNotIn("do-not-print", str(caught.exception))

    async def test_cloud_requires_key_before_accessing_database(self):
        with patch.object(settings, "CODEX_USAGE_AUTH_ENCRYPTION_KEY", ""):
            with self.assertRaises(CodexAuthError):
                async with codex_environment():
                    self.fail("Missing key")
        self.connect.assert_not_awaited()


class CodexProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_only_initializes_and_reads_limits(self):
        service = CodexUsageService()
        process = Mock(returncode=0, wait=AsyncMock())
        responses = [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {
            "primary": {"windowDurationMins": 300, "usedPercent": 69},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 3},
        }}}]
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn, \
             patch.object(service, "_send_request", AsyncMock()) as send, \
             patch.object(service, "_read_response", AsyncMock(side_effect=responses)):
            snapshot = await service._collect_cli({"PATH": "bin"})
        self.assertEqual([call.args[1]["method"] for call in send.call_args_list],
                         ["initialize", "initialized", "account/rateLimits/read"])
        self.assertEqual(spawn.call_args.kwargs["env"], {"PATH": "bin"})
        self.assertEqual(spawn.call_args.kwargs["stderr"], asyncio.subprocess.DEVNULL)
        self.assertEqual(snapshot["five_hour"]["remaining_percent"], 31)
        self.assertEqual(snapshot["weekly"]["remaining_percent"], 97)

    async def test_rpc_error_does_not_expose_provider_details(self):
        service = CodexUsageService()
        process = Mock(returncode=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
             patch.object(service, "_send_request", AsyncMock()), \
             patch.object(service, "_read_response", AsyncMock(side_effect=[
                 {"result": {}}, {"error": {"message": "sensitive-provider-details"}},
             ])):
            with self.assertRaises(CodexUsageError) as caught:
                await service._collect_cli({})
        self.assertNotIn("sensitive-provider-details", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

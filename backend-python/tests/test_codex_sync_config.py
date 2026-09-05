import unittest
from unittest.mock import patch

from scripts.sync_codex_usage import parse_args, settings


class CodexSyncConfigTests(unittest.TestCase):
    def test_sync_uses_shared_dotenv_settings(self):
        with patch.object(settings, "CODEX_USAGE_SYNC_URL", "https://voice.example.com"), \
             patch.object(settings, "CODEX_USAGE_SYNC_TOKEN", "test-token"):
            args = parse_args([])
        self.assertEqual(args.url, "https://voice.example.com")
        self.assertEqual(args.token, "test-token")

    def test_explicit_cli_options_override_settings(self):
        args = parse_args(["--url", "https://example.com", "--token", "explicit-token"])
        self.assertEqual(args.url, "https://example.com")
        self.assertEqual(args.token, "explicit-token")

    def test_missing_url_does_not_silently_use_localhost(self):
        with patch.object(settings, "CODEX_USAGE_SYNC_URL", ""), \
             patch.object(settings, "CODEX_USAGE_SYNC_TOKEN", "test-token"):
            with self.assertRaisesRegex(SystemExit, "CODEX_USAGE_SYNC_URL is required"):
                parse_args([])

    def test_missing_token_is_rejected_before_collecting_usage(self):
        with patch.object(settings, "CODEX_USAGE_SYNC_TOKEN", ""):
            with self.assertRaisesRegex(SystemExit, "CODEX_USAGE_SYNC_TOKEN is required"):
                parse_args(["--url", "https://example.com"])


if __name__ == "__main__":
    unittest.main()

"""Regression tests for cloud-to-board MCP controls."""

import unittest
from pathlib import Path


SERVICE = (
    Path(__file__).resolve().parents[1]
    / "main"
    / "services"
    / "assistant"
    / "assistant_service.cpp"
).read_text(encoding="utf-8")
APP_MANAGER = (
    Path(__file__).resolve().parents[1]
    / "main"
    / "app"
    / "launcher"
    / "app_manager.cpp"
).read_text(encoding="utf-8")


class DeviceCloudControlTests(unittest.TestCase):
    def test_status_reports_values_required_by_dashboard(self):
        for field in (
            "assistant_state",
            "firmware",
            "free_heap",
            "storage_used",
            "storage_total",
            "volume",
            "brightness",
        ):
            with self.subTest(field=field):
                self.assertIn(f'\\"{field}\\"', SERVICE)

    def test_exact_brightness_tool_is_declared_and_handled(self):
        self.assertGreaterEqual(SERVICE.count('"display.set_brightness"'), 2)
        self.assertIn("board_->SetBrightness(static_cast<uint8_t>(brightness_j->valueint))", SERVICE)
        self.assertIn('"brightness must be an integer from 0 to 100"', SERVICE)

    def test_clock_and_wallpaper_tools_are_declared_and_handled(self):
        for tool in ("clock.configure", "wallpaper.set", "wallpaper.sync"):
            with self.subTest(tool=tool):
                self.assertGreaterEqual(SERVICE.count(f'"{tool}"'), 2)
        self.assertIn("RequestClockSettings(style, color_hex, mode)", SERVICE)
        self.assertIn("RequestWallpaperUrl(url, wallpaper_name)", SERVICE)
        self.assertIn("RequestWallpaperSync()", SERVICE)

    def test_cloud_wallpaper_download_does_not_block_websocket_callback(self):
        self.assertIn('xTaskCreatePinnedToCore(ProcessWallpaperCommand, "wallpaper_set"', APP_MANAGER)
        self.assertIn('xTaskCreatePinnedToCore(ProcessWallpaperCommand, "wallpaper_sync"', APP_MANAGER)
        self.assertIn('RequestLaunch("wallpaper")', APP_MANAGER)


if __name__ == "__main__":
    unittest.main()

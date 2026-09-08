"""Regression contracts for unobtrusive in-app navigation controls."""

import unittest
from pathlib import Path


FIRMWARE = Path(__file__).resolve().parents[1]
APP_SOURCE = (FIRMWARE / "main/app/launcher/app_manager.cpp").read_text(encoding="utf-8")
DISPLAY_SOURCE = (FIRMWARE / "main/board/es3c28p/display.cpp").read_text(encoding="utf-8")


class AutoHideControlsTests(unittest.TestCase):
    def test_controls_hide_after_two_seconds(self):
        self.assertIn("kControlsVisibleMs = 2000", APP_SOURCE)
        self.assertIn("lv_obj_add_flag(app->controls_[i], LV_OBJ_FLAG_HIDDEN)", APP_SOURCE)
        self.assertIn("}, kControlsVisibleMs, this)", APP_SOURCE)

    def test_back_and_tracking_refresh_buttons_are_registered(self):
        self.assertIn("AddAutoHideControl(top_btn)", APP_SOURCE)
        self.assertGreaterEqual(APP_SOURCE.count("AddAutoHideControl(refresh)"), 2)
        self.assertIn("AddAutoHideControl(back_btn)", APP_SOURCE)

    def test_any_new_touch_reveals_current_app_controls(self):
        self.assertIn("s_touch_activity_handler(s_touch_activity_context)", DISPLAY_SOURCE)
        self.assertIn("manager->current_->OnUserInteraction()", APP_SOURCE)
        self.assertIn("man_utd_.OnUserInteraction()", APP_SOURCE)
        self.assertIn("codex_credit_.OnUserInteraction()", APP_SOURCE)


if __name__ == "__main__":
    unittest.main()

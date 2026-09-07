"""Regression contracts for the global bottom-swipe menu gesture."""

import unittest
from pathlib import Path


FIRMWARE = Path(__file__).resolve().parents[1]
DISPLAY_SOURCE = (FIRMWARE / "main/board/es3c28p/display.cpp").read_text(encoding="utf-8")
APP_SOURCE = (FIRMWARE / "main/app/launcher/app_manager.cpp").read_text(encoding="utf-8")


class BottomSwipeMenuTests(unittest.TestCase):
    def test_gesture_starts_at_bottom_and_reaches_screen_middle(self):
        self.assertIn("MENU_SWIPE_START_Y = 200", DISPLAY_SOURCE)
        self.assertIn("MENU_SWIPE_END_Y = 120", DISPLAY_SOURCE)
        self.assertIn("MENU_SWIPE_MIN_TRAVEL = 80", DISPLAY_SOURCE)
        self.assertIn("point.y >= MENU_SWIPE_START_Y", DISPLAY_SOURCE)
        self.assertIn("s_menu_swipe.last_y <= MENU_SWIPE_END_Y", DISPLAY_SOURCE)

    def test_horizontal_drag_is_rejected(self):
        self.assertIn("horizontal <= rise", DISPLAY_SOURCE)

    def test_release_requests_launcher_through_app_queue(self):
        self.assertIn("SetMenuSwipeHandler", APP_SOURCE)
        self.assertIn('manager->RequestLaunch("launcher")', APP_SOURCE)


if __name__ == "__main__":
    unittest.main()

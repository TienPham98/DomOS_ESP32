"""Regression contract for accessible back-button hit targets."""

import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "main"
    / "app"
    / "launcher"
    / "app_manager.cpp"
).read_text(encoding="utf-8")


class BackButtonSizeTests(unittest.TestCase):
    def test_all_back_buttons_use_thirty_percent_scale_helper(self):
        self.assertIn("kBackButtonScalePercent = 130", SOURCE)
        for size in (
            "BackButtonSize(48), BackButtonSize(24)",
            "BackButtonSize(40), BackButtonSize(28)",
            "BackButtonSize(42), BackButtonSize(24)",
        ):
            with self.subTest(size=size):
                self.assertIn(size, SOURCE)

    def test_back_glyph_uses_available_larger_font(self):
        self.assertGreaterEqual(SOURCE.count("lv_font_montserrat_16"), 5)


if __name__ == "__main__":
    unittest.main()

"""Regression contracts for wallpaper cache and empty-state rendering."""

import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "main"
    / "app"
    / "launcher"
    / "app_manager.cpp"
).read_text(encoding="utf-8")


class WallpaperEmptyStateTests(unittest.TestCase):
    def test_all_cached_slots_are_checked_before_empty_state(self):
        fallback = SOURCE.index("for (size_t offset = 1; offset < NUM_WALLPAPER_SLOTS; ++offset)")
        empty_state = SOURCE.index("lv_obj_clear_flag(empty_container_, LV_OBJ_FLAG_HIDDEN)", fallback)
        self.assertLess(fallback, empty_state)
        self.assertIn("s_current_slot_idx = static_cast<int>(cached_slot)", SOURCE[fallback:empty_state])

    def test_valid_image_hides_empty_state(self):
        self.assertIn(
            "if (empty_container_ != nullptr) lv_obj_add_flag(empty_container_, LV_OBJ_FLAG_HIDDEN)",
            SOURCE,
        )


if __name__ == "__main__":
    unittest.main()

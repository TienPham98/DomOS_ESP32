"""Regression contracts for the merged Tracking Status firmware app."""

import unittest
from pathlib import Path


FIRMWARE = Path(__file__).resolve().parents[1]
APP_SOURCE = (FIRMWARE / "main/app/launcher/app_manager.cpp").read_text(encoding="utf-8")
ASSISTANT_SOURCE = (
    FIRMWARE / "main/services/assistant/assistant_service.cpp"
).read_text(encoding="utf-8")


class TrackingStatusAppTests(unittest.TestCase):
    def test_launcher_exposes_one_merged_tracking_app(self):
        self.assertIn('Button(screen_, "Tracking Status"', APP_SOURCE)
        self.assertIn('static TrackingStatusApp tracking_status(*this)', APP_SOURCE)
        self.assertNotIn('static ManchesterUnitedApp man_utd(*this)', APP_SOURCE)
        self.assertNotIn('static CodexCreditApp codex_credit(*this)', APP_SOURCE)

    def test_views_rotate_every_ten_seconds(self):
        self.assertIn("kRotationPeriodMs = 10U * 1000U", APP_SOURCE)
        self.assertIn("app->ShowNextView()", APP_SOURCE)
        self.assertIn("man_utd_.ShowCached()", APP_SOURCE)
        self.assertIn("codex_credit_.ShowCached()", APP_SOURCE)

    def test_old_voice_app_ids_route_to_merged_app(self):
        self.assertIn(
            '(app == "man-utd" || app == "codex-credit") ? "tracking-status" : app',
            APP_SOURCE,
        )
        self.assertIn('cJSON_CreateString("tracking-status")', ASSISTANT_SOURCE)


if __name__ == "__main__":
    unittest.main()

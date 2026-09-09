import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MqttCloudSafetyTests(unittest.TestCase):
    def test_secure_broker_without_credentials_fails_fast(self):
        source = (ROOT / "main/services/mqtt/mqtt_service.cpp").read_text(encoding="utf-8")

        self.assertIn("const bool secure_cloud", source)
        self.assertIn("CONFIG_DOMOS_MQTT_USERNAME[0] == '\\0'", source)
        self.assertIn("CONFIG_DOMOS_MQTT_PASSWORD[0] == '\\0'", source)
        self.assertIn("broker disabled to protect voice connectivity", source)
        self.assertLess(source.index("broker disabled to protect voice connectivity"),
                        source.index("esp_mqtt_client_init"))

    def test_voice_transport_enables_tcp_keepalive_and_text_diagnostics(self):
        source = (ROOT / "main/services/assistant/ws_client.cpp").read_text(encoding="utf-8")

        self.assertIn("ws_cfg.keep_alive_enable    = true", source)
        self.assertIn("Text frame send failed", source)


if __name__ == "__main__":
    unittest.main()

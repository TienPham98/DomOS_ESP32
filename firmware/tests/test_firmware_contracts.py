"""Host-side regression tests for ESP32-S3 hardware and protocol contracts."""

import re
import unittest
from pathlib import Path


FIRMWARE = Path(__file__).resolve().parents[1]
MAIN = FIRMWARE / "main"


def read(relative_path: str) -> str:
    return (FIRMWARE / relative_path).read_text(encoding="utf-8")


class BoardContractTests(unittest.TestCase):
    def test_es3c28p_pin_map_is_unchanged(self):
        config = read("main/board/es3c28p/board_config.h")
        expected = {
            "TFT_MOSI": 11, "TFT_MISO": 13, "TFT_SCLK": 12, "TFT_CS": 10,
            "TFT_DC": 46, "TFT_BL": 45, "TOUCH_SDA": 16, "TOUCH_SCL": 15,
            "TOUCH_RST": 18, "TOUCH_INT": 17, "AUDIO_MCLK": 4,
            "AUDIO_BCLK": 5, "AUDIO_DIN": 6, "AUDIO_WS": 7,
            "AUDIO_DOUT": 8, "AUDIO_PA": 1,
        }
        for name, gpio in expected.items():
            with self.subTest(name=name):
                self.assertRegex(config, rf"#define\s+{name}\s+GPIO_NUM_{gpio}\b")

    def test_codec_reset_and_audio_format_match_current_board(self):
        codec = read("main/board/es3c28p/es8311.cpp")
        self.assertIn("write_reg(ctrl_if_, 0x00, 1, &reset_value, 1)", codec)
        self.assertRegex(codec, r"reset_value\s*=\s*0x1F")
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(5))", codec)
        self.assertIn("sample_info.bits_per_sample = 16", codec)
        self.assertIn("sample_info.channel = 1", codec)
        self.assertIn("std::clamp(gain_db, 0, 42)", codec)


class IntegrationContractTests(unittest.TestCase):
    def test_voice_starts_after_wifi_without_opening_assistant_screen(self):
        main = read("main/main.cpp")
        startup = main[main.index("void StartNetworkClientsWhenWifiReady"):main.index("} // namespace")]
        self.assertLess(startup.index("while (!services->wifi->Connected())"),
                        startup.index("services->assistant->OpenAudioChannel()"))
        self.assertNotIn('Launch("assistant")', startup)
        manager = read("main/app/launcher/app_manager.cpp")
        assistant_app = manager[manager.index("class AssistantApp"):manager.index("class OtaApp")]
        self.assertNotIn("CloseAudioChannel", assistant_app)

    def test_wake_and_tool_launch_share_event_bus_fifo(self):
        service = read("main/services/assistant/assistant_service.cpp")
        listen = service[service.index("void AssistantService::HandleListen"):service.index("void AssistantService::HandleStt")]
        self.assertIn('strcmp(source_j->valuestring, "wake_word") == 0', listen)
        self.assertIn('events_->Publish(EventType::AppLaunchRequested, TAG, "assistant")', listen)
        self.assertIn("events_->Publish(EventType::AppLaunchRequested, TAG, app)", service)
        self.assertNotIn('apps_->RequestLaunch("assistant")', service)
        self.assertIn("events.Subscribe(EventType::AppLaunchRequested", read("main/main.cpp"))

    def test_app_launch_queue_does_not_touch_lvgl_from_websocket(self):
        manager = read("main/app/launcher/app_manager.cpp")
        request = manager[manager.index("void AppManager::RequestLaunch"):manager.index("void AppManager::CloseCurrent")]
        self.assertIn("xQueueSend", request)
        self.assertNotIn("lv_async_call", request)
        self.assertNotIn("new ", request)
        self.assertIn("xQueueReceive", manager)

    def test_opening_screen_does_not_recreate_reconnecting_websocket(self):
        service = read("main/services/assistant/assistant_service.cpp")
        self.assertIn("ws_.IsStarted() || GetState() != AssistantState::Idle", service)
        self.assertIn("channel_lock(channel_mutex_)", service)

    def test_widget_fetches_share_internal_ram_budget_with_background_voice(self):
        self.assertIn("CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP=y", read("sdkconfig.defaults"))
        manager = read("main/app/launcher/app_manager.cpp")
        self.assertIn("std::atomic<bool> s_widget_fetch_busy{false}", manager)
        self.assertEqual(manager.count("s_widget_fetch_busy.compare_exchange_strong"), 2)
        self.assertEqual(manager.count("s_widget_fetch_busy = false;"), 4)
        self.assertEqual(manager.count("deferred_force_ = deferred_force_ || force"), 2)
        self.assertGreaterEqual(manager.count("lv_timer_set_period(refresh_timer_, 1000)"), 4)

    def test_private_network_endpoints_are_injected(self):
        defaults = read("sdkconfig.defaults")
        wifi = read("main/services/wifi/wifi_service.cpp")
        self.assertIn('CONFIG_DOMOS_MQTT_URI=""', defaults)
        self.assertIn('CONFIG_DOMOS_AI_WS_URI=""', defaults)
        self.assertIn("CONFIG_DOMOS_DEVICE_IP", wifi)
        self.assertIn('std::strcmp(ssid, "Dom_12") != 0', wifi)
        self.assertIn("esp_netif_dhcpc_start(s_netif)", wifi)
        self.assertIn("stop_wifi_result != ESP_ERR_WIFI_NOT_STARTED", wifi)
        self.assertNotIn("Rejected SSID", wifi)
        self.assertIn("const esp_err_t connect_result = esp_wifi_connect()", wifi)

    def test_cloud_connections_verify_tls_and_use_psram(self):
        defaults = read("sdkconfig.defaults")
        self.assertIn("CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y", defaults)
        self.assertIn("CONFIG_MBEDTLS_EXTERNAL_MEM_ALLOC=y", defaults)
        manager = read("main/app/launcher/app_manager.cpp")
        self.assertIn("kWidgetFetchStackBytes = 8192", manager)
        self.assertIn("uxTaskGetStackHighWaterMark(nullptr)", manager)
        for path in ("app/launcher/app_manager.cpp", "services/mqtt/mqtt_service.cpp",
                     "services/assistant/ws_client.cpp"):
            with self.subTest(path=path):
                source = read(f"main/{path}")
                self.assertIn("esp_crt_bundle_attach", source)
                self.assertNotIn("skip_cert_common_name_check = true", source)
        mqtt = read("main/services/mqtt/mqtt_service.cpp")
        self.assertIn("CONFIG_DOMOS_MQTT_USERNAME", mqtt)
        self.assertIn("CONFIG_DOMOS_MQTT_PASSWORD", mqtt)

    def test_status_reports_actual_firmware_build(self):
        server = read("main/services/filesystem/upload_server.cpp")
        self.assertIn("esp_app_get_description()", server)
        self.assertIn("firmware_build", server)

    def test_voice_reconnects_when_cloud_closes_socket_for_deployment(self):
        source = read("main/services/assistant/ws_client.cpp")
        self.assertIn("ws_cfg.enable_close_reconnect = true", source)
        self.assertRegex(source, r"case WEBSOCKET_EVENT_DISCONNECTED:\s*"
                                r"case WEBSOCKET_EVENT_CLOSED:\s*"
                                r"self->connected_\.store\(false\)")

    def test_wifi_config_accepts_arbitrary_ssid_and_keeps_keyboard_open(self):
        manager = read("main/app/launcher/app_manager.cpp")
        server = read("main/services/filesystem/upload_server.cpp")
        self.assertIn("manager_.Wifi()->ConnectTo", manager)
        self.assertIn("Connection failed (reason %d)", manager)
        self.assertIn("lv_obj_add_flag(app->kb_, LV_OBJ_FLAG_HIDDEN)", manager)
        self.assertIn('app = "wifi-setup"', server)

    def test_audio_tasks_keep_realtime_core_and_priority_contract(self):
        pipeline = read("main/services/assistant/audio_pipeline.cpp")
        self.assertRegex(
            pipeline,
            r'xTaskCreatePinnedToCore\(MicTask,\s*"mic_capture",\s*4096,\s*this,\s*7,\s*&mic_handle,\s*1\)',
        )
        self.assertRegex(
            pipeline,
            r'xTaskCreatePinnedToCore\(OutputTask,\s*"audio_out",\s*4096,\s*this,\s*7,\s*&output_handle,\s*0\)',
        )

    def test_speaker_amplifier_is_only_controlled_by_assistant(self):
        callers = []
        for source in MAIN.rglob("*.cpp"):
            if source.as_posix().endswith("board/es3c28p/audio.cpp"):
                continue
            if "SetPAEnabled(" in source.read_text(encoding="utf-8"):
                callers.append(source.relative_to(MAIN).as_posix())
        self.assertEqual(callers, ["services/assistant/assistant_service.cpp"])

    def test_state_changes_publish_through_set_state(self):
        service = read("main/services/assistant/assistant_service.cpp")
        self.assertIn("state_.exchange(static_cast<uint8_t>(s))", service)
        self.assertIn("events_->Publish(EventType::AssistantState", service)
        outside_setter = service[:service.index("void AssistantService::SetState")]
        self.assertNotRegex(outside_setter, r"state_\s*(?:=|\.store\s*\()")

    def test_device_http_contract_is_registered(self):
        server = read("main/services/filesystem/upload_server.cpp")
        for route in ("/upload", "/api/status", "/api/logs", "/api/wallpaper"):
            with self.subTest(route=route):
                self.assertIn(f'.uri = "{route}"', server)
        self.assertIn("s_apps->RequestLaunch(app)", server)
        self.assertNotIn("s_apps->Launch(app)", server)

    def test_manchester_united_app_keeps_network_work_off_realtime_tasks(self):
        manager = read("main/app/launcher/app_manager.cpp")
        assistant = read("main/services/assistant/assistant_service.cpp")
        upload_server = read("main/services/filesystem/upload_server.cpp")

        self.assertIn("class ManchesterUnitedApp", manager)
        self.assertIn('return "man-utd";', manager)
        self.assertIn('"/api/football/manchester-united"', manager)
        self.assertIn('"/api/football/manchester-united/background.jpg"', manager)
        self.assertRegex(
            manager,
            r'xTaskCreatePinnedToCore\(\s*FetchTask,\s*"manutd_fetch",\s*kWidgetFetchStackBytes,\s*context,\s*3,\s*nullptr,\s*0\)',
        )
        self.assertNotRegex(manager, r'xTaskCreatePinnedToCoreWithCaps\([^;]*"manutd_fetch"')
        self.assertIn("60U * 60U * 1000U", manager)
        self.assertIn('cJSON_CreateString("man-utd")', assistant)
        self.assertIn('strcmp(app, "man-utd") != 0', assistant)
        self.assertIn('app = "man-utd"', upload_server)

    def test_codex_credit_app_uses_gateway_and_background_fetch_task(self):
        manager = read("main/app/launcher/app_manager.cpp")
        assistant = read("main/services/assistant/assistant_service.cpp")
        upload_server = read("main/services/filesystem/upload_server.cpp")

        self.assertIn("class CodexCreditApp", manager)
        self.assertIn('return "codex-credit";', manager)
        self.assertIn('"/api/codex/usage"', manager)
        self.assertRegex(
            manager,
            r'percent\s*=\s*Label\(card,\s*"--% LEFT"[\s\S]*?&lv_font_montserrat_14\)',
        )
        self.assertRegex(
            manager,
            r'xTaskCreatePinnedToCore\(\s*FetchTask,\s*"codex_fetch",\s*kWidgetFetchStackBytes,\s*context,\s*3,\s*nullptr,\s*0\)',
        )
        self.assertNotRegex(manager, r'xTaskCreatePinnedToCoreWithCaps\([^;]*"codex_fetch"')
        self.assertIn("60U * 1000U", manager)
        self.assertIn('cJSON_CreateString("codex-credit")', assistant)
        self.assertIn('strcmp(app, "codex-credit") != 0', assistant)
        self.assertIn('app = "codex-credit"', upload_server)


if __name__ == "__main__":
    unittest.main()

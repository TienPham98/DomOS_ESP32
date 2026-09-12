# DomOS Firmware — ES3C28P

C++17 firmware for ESP32-S3 using ESP-IDF, FreeRTOS, LVGL, ESP-SR, LittleFS, MQTT, HTTP, and Dom Voice Protocol v3.

## Hardware

| Component | Configuration |
|---|---|
| LCD | ILI9341, SPI2, landscape 320 × 240 |
| Touch | FT6336G, I2C0 |
| Audio codec | ES8311, I2S0, 16 kHz |
| Speaker amplifier | GPIO1, active low |
| Flash / PSRAM | 16 MB / 8 MB |

Pin definitions are authoritative in `main/board/es3c28p/board_config.h`. Do not duplicate or change them for ordinary feature work.

## Runtime architecture

```text
app_main
  |-- board: display, touch, audio, storage
  |-- kernel: EventBus, tasks, Wi-Fi
  |-- services: assistant, MQTT, HTTP, media, OTA
  `-- AppManager: launcher and screens

Microphone -> ESP-SR AFE/VAD/MultiNet -> AssistantService -> Voice Gateway
Voice Gateway -> PCM/TTS -> AudioPipeline -> ES8311 speaker
```

Assistant states:

```text
Idle -> Connecting -> Armed -> Listening -> Processing -> Speaking -> Armed
```

- `Hey Dom`, `Hey`, or `Dom` activates the Assistant while another app is open.
- Touch can start listening, submit speech, or cancel a response.
- A bottom-to-middle upward swipe returns to the launcher.
- The display shows assistant state only; full transcripts remain in dashboard history.
- `AssistantService` is the only owner of speaker-amplifier control.

## Applications

- Launcher
- Clock
- Wallpaper and slideshow
- Tracking Status
- Dashboard status
- Settings and Wi-Fi setup
- Smart Home
- Assistant
- OTA

`Tracking Status` contains the Manchester United and Codex views. It rotates every 15 seconds, keeps back/refresh controls hidden during automatic rotation, and refreshes each data source through the existing Voice Gateway WebSocket session. Cached data remains available during temporary outages.

## Configuration

Private deployment values are injected from `.env` into the ignored build configuration or set through `idf.py menuconfig`.

| Area | Kconfig symbols |
|---|---|
| Wi-Fi | `CONFIG_DOMOS_WIFI_SSID`, `CONFIG_DOMOS_WIFI_PASSWORD` |
| Device network | `CONFIG_DOMOS_DEVICE_IP`, `CONFIG_DOMOS_GATEWAY_IP`, `CONFIG_DOMOS_NETMASK` |
| MQTT | `CONFIG_DOMOS_MQTT_URI`, `CONFIG_DOMOS_MQTT_USERNAME`, `CONFIG_DOMOS_MQTT_PASSWORD` |
| Voice | `CONFIG_DOMOS_AI_HTTP_BASE`, `CONFIG_DOMOS_AI_WS_URI`, `CONFIG_DOMOS_AI_AUTH_TOKEN` |
| Dashboard/OTA | `CONFIG_DOMOS_DASHBOARD_URL`, `CONFIG_DOMOS_OTA_FIRMWARE_URL` |
| ESP-SR | `CONFIG_DOMOS_ESP_SR`, `CONFIG_DOMOS_WAKE_THRESHOLD`, `CONFIG_DOMOS_SR_*` |

Do not commit real addresses, Wi-Fi credentials, tokens, generated `sdkconfig`, NVS data, or flash backups. The dedicated network may use its private static configuration; any other selected SSID uses DHCP.

## Build and flash

From an ESP-IDF shell:

```powershell
idf.py menuconfig
idf.py build
idf.py -p <SERIAL_PORT> -b <FLASH_BAUD> flash monitor
```

Project helpers are also available:

```powershell
.\build.bat
.\flash.bat
.\monitor.bat
```

The ESP-SR build produces both the application image and model partition image. Follow `ESP_SR.md` before changing the partition layout or migrating an older board.

## Storage layout

| Partition | Size | Purpose |
|---|---:|---|
| NVS | 64 KiB | Runtime configuration |
| OTA data | 8 KiB | Active slot selection |
| OTA slot A | 4 MiB | Application |
| OTA slot B | 4 MiB | Application |
| LittleFS | 5 MiB | Images, caches, user data |
| ESP-SR model | 2.8125 MiB | AFE/VAD/MultiNet models |
| Coredump | 64 KiB | Crash diagnostics |

## Device interfaces

The firmware exposes status, logs, app launch, clock, wallpaper, slideshow, and upload compatibility routes under the board HTTP server. Brightness, volume, status, and app launch are available through bidirectional MCP over the voice WebSocket.

Supported MCP tools include:

- `device.get_status`
- `speaker.set_volume`
- `speaker.adjust_volume`
- `display.adjust_brightness`
- `app.launch`

All state changes must be acknowledged before the gateway announces success.

## Stability rules

- Never block microphone or output tasks with HTTP, UI, STT, or storage work.
- Never update LVGL objects from WebSocket or MQTT callbacks.
- Use EventBus for assistant/app notifications and mutexes for shared state.
- Keep DMA buffers in DMA-capable internal memory; use PSRAM for large image buffers.
- Preserve NVS and both OTA slots during flash operations unless recovery explicitly requires otherwise.

## Test

```powershell
python -m unittest discover -s tests -v
idf.py build
```

See `DISPLAY_CONFIG.md` for LCD/touch details and `ESP_SR.md` for local speech processing.

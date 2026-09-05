# DomOS Firmware — ES3C28P

Trong tài liệu, thay `<HOST_IP>` và `<DEVICE_IP>` bằng địa chỉ của môi trường triển khai. Giá trị firmware thực tế vẫn được cấu hình riêng khi build.

Firmware C++17 cho ESP32-S3, build bằng ESP-IDF 5.3.1. Firmware quản lý LCD/touch, audio ES8311, launcher/app, Wi-Fi, MQTT, HTTP API, LittleFS, OTA metadata và Dom Voice Protocol v3.

## Phần cứng cố định

| Thành phần | GPIO/cấu hình |
|---|---|
| LCD ILI9341 | MOSI 11, MISO 13, SCLK 12, CS 10, DC 46, BL 45; SPI2 40 MHz |
| Touch FT6336G | SDA 16, SCL 15, RST 18, INT 17; I2C0 400 kHz, addr `0x38` |
| ES8311 | MCLK 4, BCLK 5, WS 7, DIN 6, DOUT 8; I2S0 16 kHz |
| Speaker PA | GPIO1 active-low: LOW bật, HIGH mute |
| RGB LED | GPIO42 |
| Boot button | GPIO0 |
| Flash | 16 MB QIO/80 MHz |
| PSRAM | 8 MB Octal/80 MHz |
| Serial | COM5, flash baud 460800 |

Không thay đổi pin trong `main/board/es3c28p/board_config.h` khi chỉ sửa tính năng assistant/app.

## Cấu trúc

```text
firmware/
├── main/
│   ├── main.cpp                         app_main và boot order
│   ├── Kconfig.projbuild                menu DomOS
│   ├── board/es3c28p/
│   │   ├── board_config.h               pin và kích thước
│   │   ├── board_es3c28p.*              board facade
│   │   ├── display.cpp                  ILI9341 + LVGL
│   │   ├── touch.cpp                    FT6336G
│   │   ├── audio.cpp                    I2S và PA
│   │   └── es8311.*                     codec wrapper/reset/gain
│   ├── app/launcher/app_manager.*       app registry/UI
│   ├── kernel/                           EventBus, TaskManager, storage
│   └── services/
│       ├── assistant/                    WebSocket, state, audio pipeline, MCP
│       ├── wifi/                         STA/static IP
│       ├── mqtt/                         broker client
│       ├── filesystem/                   HTTP API, upload, logs
│       ├── media/
│       └── ota/
├── partitions.csv
├── sdkconfig.defaults
├── build.bat
├── flash.bat
└── monitor.bat
```

## Boot sequence

`app_main()` thực hiện:

1. Khởi tạo NVS; erase/re-init nếu schema NVS cũ.
2. Khởi tạo `ES3C28PBoard`: display, touch, audio, storage.
3. Khởi tạo `EventBus` và layout LittleFS.
4. Start Wi-Fi, OTA, media và HTTP server.
5. Start audio pipeline của `AssistantService` với Voice Gateway cố định.
6. Start `AppManager`, tạo hàng đợi mở app và đăng ký callback EventBus.
7. Chạy task dispatch event sau khi đăng ký listener.
8. Đợi Wi-Fi có IP rồi start MQTT và tự mở voice session nghe nền.
9. Start LVGL task.

Không cần vào màn hình Assistant lần đầu để bật nghe nền. Wake được nhận sẽ
đưa Assistant lên trước bằng `AppLaunchRequested`; lệnh mở app tiếp theo dùng
cùng EventBus FIFO để giữ thứ tự. `RequestLaunch` chỉ gửi hàng đợi FreeRTOS,
timer LVGL xử lý chuyển màn hình; task WebSocket không tạo timer/sửa UI.
Client WebSocket đang reconnect không bị tạo lại khi người dùng mở Assistant.

Nghe nền vẫn cần gateway/cloud STT, không phải wake offline trên ESP32. Chi tiết
và phần cá nhân hóa giọng chưa hoàn tất ở [WAKE_WORD.md](../docs/WAKE_WORD.md).

MQTT được trì hoãn tới `IP_EVENT_GOT_IP` để tránh lwIP gửi dữ liệu khi Wi-Fi còn association và để AppManager cài message handler trước khi broker deliver command.

## Cấu hình mạng

- Board trên `Dom_12`: `<DEVICE_IP>`.
- SSID mặc định: `Dom_12`; có thể đổi trong app Wi-Fi Config.
- SSID khác dùng DHCP để nhận IP, gateway và DNS tự động.
- SSID/password được lưu trong NVS và dùng lại sau reboot.
- Host backend: `<HOST_IP>`.
- MQTT: `mqtt://<HOST_IP>:1883`.
- Voice: `ws://<HOST_IP>:8000/api/v1/voice/stream`.
- HTTP board: port 80.

Các địa chỉ triển khai chỉ được khai báo trong `.env` ở thư mục gốc. Khi CMake cấu hình firmware, các giá trị này được ghi vào `build/sdkconfig` (đã bị Git bỏ qua). `sdkconfig` không còn là file mã nguồn.

Trong `idf.py menuconfig` → `DomOS`:

| Kconfig | Mặc định | Ý nghĩa |
|---|---|---|
| `CONFIG_DOMOS_WIFI_SSID` | `Dom_12` | SSID mặc định khi NVS chưa có credential |
| `CONFIG_DOMOS_WIFI_PASSWORD` | rỗng | WPA2 password, compile vào firmware development |
| `CONFIG_DOMOS_WIFI_MAX_RETRY` | `10` | Số lần reconnect |
| `CONFIG_DOMOS_MQTT_URI` | từ `.env` | MQTT broker |
| `CONFIG_DOMOS_OTA_FIRMWARE_URL` | rỗng | URL firmware; OTA disabled khi rỗng |
| `CONFIG_DOMOS_AI_WS_URI` | từ `.env` | Voice WebSocket |
| `CONFIG_DOMOS_AI_AUTH_TOKEN` | rỗng | Optional Bearer token |

## Build, flash và monitor

Môi trường đã khóa trong batch script:

```text
IDF_PATH=C:\Espressif\frameworks\esp-idf-v5.3.1
IDF_PYTHON_ENV_PATH=C:\Espressif\python_env\idf5.3_py3.11_env
```

Build sạch:

```powershell
cd "D:\Work space\DomOS\firmware"
.\build.bat
```

Flash và monitor:

```powershell
.\flash.bat
```

Lệnh thủ công trong ESP-IDF shell:

```powershell
idf.py menuconfig
idf.py build
idf.py -p COM5 -b 460800 flash monitor
```

Thoát monitor bằng `Ctrl+]`.

## Partition table

| Partition | Offset | Size | Vai trò |
|---|---:|---:|---|
| `nvs` | `0x9000` | 64 KB | config/NVS |
| `otadata` | `0x19000` | 8 KB | OTA selection |
| `phy_init` | `0x1B000` | 4 KB | PHY data |
| `ota_0` | `0x20000` | 4 MB | app slot A |
| `ota_1` | tự động | 4 MB | app slot B |
| `littlefs` | tự động | 7 MB | wallpaper/config/cache |
| `coredump` | tự động | 64 KB | crash dump |

## App Manager

App được đăng ký khi boot:

- `launcher`
- `clock`
- `wallpaper`
- `man-utd`
- `codex-credit`
- `dashboard`
- `settings`
- `smart-home`
- `assistant`
- `ota`

Launcher mặc định mở trước. HTTP `/api/launch` có thể mở các app trên; MCP `app.launch` cho phép `wallpaper`, `clock`, `man-utd` và `codex-credit`.

## Assistant state machine

```text
Idle -> Connecting -> Armed -> Listening -> Processing -> Speaking -> Armed
```

| State | LCD | Hành vi |
|---|---|---|
| `Idle` | Offline | Chưa có Voice session |
| `Connecting` | Connecting | WebSocket handshake |
| `Armed` | Waiting for Hey Dom | Mic uplink cho wake word |
| `Listening` | Listening | Thu câu lệnh; VAD gateway tự chốt |
| `Processing` | Thinking | STT/LLM/MCP đang chạy |
| `Speaking` | Responding | Nhận binary PCM và phát loa |

Mọi transition đi qua `AssistantService::SetState()`. `EventBus` phát `AssistantState` để UI/subsystem khác đồng bộ.

### Touch/cancel

- Chạm màn hình ở `Armed`: bắt đầu `Listening`.
- Chạm ở `Listening`: manual submit câu nói; VAD vẫn là đường chính.
- Nút X góc trên ở `Processing`/`Speaking`: gửi `abort`, flush output, tắt PA và trở về `Armed`.
- LCD chỉ hiện trạng thái, không hiện transcript/answer. Nội dung đầy đủ nằm trên Dashboard.

## Audio

- PCM: 16 kHz, signed 16-bit, mono.
- Mic chunk: 960 sample = 60 ms = 1920 byte.
- I2S physical bus dùng two-slot Philips stereo như ES8311/Xiaozhi; `esp_codec_dev` đưa ra stream mono.
- ES8311 digital block được software reset (`reg 0x00 = 0x1F`, delay 5 ms) trước init.
- Mic analog gain của board hiện đặt 42 dB vì unit thực tế có mức tín hiệu thấp ở 30 dB.
- PA GPIO1 active-low và chỉ `AssistantService` được bật/tắt PA.
- Không có AEC; mic chỉ upload trong `Armed`/`Listening`, không upload khi loa đang nói.

### FreeRTOS task

| Task | Core | Priority | Vai trò |
|---|---:|---:|---|
| `mic_capture` | 1 | 7 | Đọc ES8311 mic |
| `audio_out` | 0 | 7 | Phát PCM loa |
| `audio_uplink` | theo scheduler | 6 | Gửi PCM qua WebSocket |
| `lvgl` | 0 | 6 | UI tick/render |

Mic/output task không được block bởi HTTP, STT hay network operation dài.

## Dom Voice Protocol v3

Endpoint: `/api/v1/voice/stream`.

- `hello`: version/features/audio negotiation.
- binary uplink: PCM mic.
- `listen`: start/stop/wake/processing.
- `stt`: transcript từ gateway.
- `llm`: text/emotion/state.
- `tts`: start/sentence_start/stop.
- binary downlink: PCM TTS.
- `abort`: user cancel.
- `mcp`: JSON-RPC 2.0 hai chiều.

WebSocket callback chạy trong task của ESP client. Shared state dùng atomic/mutex; không gọi thao tác block dài trong callback.

## MCP tools trên board

- `device.get_status`
- `speaker.set_volume`
- `speaker.adjust_volume`
- `display.adjust_brightness`
- `app.launch` (`wallpaper`, `clock`, `man-utd`, `codex-credit`)

Firmware hỗ trợ MCP `initialize`, `tools/list`, `tools/call` và trả JSON-RPC result/error.

### Manchester United fixture app

App `man-utd` hiển thị ảnh nền đỏ với logo Manchester United, giải đấu, cặp đấu, ngày và giờ địa phương của trận kế tiếp. Board chỉ gọi Voice Gateway qua `CONFIG_DOMOS_AI_HTTP_BASE`; API key của nhà cung cấp không được đưa vào firmware.

- Lịch: `GET /api/football/manchester-united`.
- Nền LCD: `GET /api/football/manchester-united/background.jpg` (JPEG 320×240).
- Dữ liệu và ảnh được lưu trong LittleFS; download lỗi không ghi đè bản cache tốt.
- HTTP chạy ở task `manutd_fetch`, core 0, priority 3; không chặn `mic_capture`/`audio_out` priority 7.
- Mở từ launcher, HTTP `/api/launch` với `{"app":"man-utd"}`, hoặc nói “mở lịch Manchester United”.

### Codex usage app

App `codex-credit` hiển thị phần trăm còn lại của hạn mức 5 giờ và tuần, thời điểm reset của từng cửa sổ, cùng trạng thái full reset. Board tải JSON từ `GET /api/codex/usage` của Voice Gateway trong task `codex_fetch` trên core 0, priority 3 và giữ cache LittleFS khi mất mạng.

- Mở bằng nút `Codex` trong launcher.
- HTTP `/api/launch` với `{"app":"codex-credit"}`.
- Hoặc nói “mở hạn mức Codex”.
- Nút refresh ở góc dưới phải gọi API với `force=true`.

## HTTP API của board

Base URL: `http://<DEVICE_IP>`.

| Method | Path | Trạng thái hiện tại |
|---|---|---|
| `GET` | `/api/status` | MAC, firmware 0.3.5, Wi-Fi, heap, PSRAM, storage |
| `GET` | `/api/logs` | Ring buffer tối đa 50 log |
| `WS` | `/ws` | Realtime log/connection |
| `POST` | `/api/launch` | Mở app theo JSON body |
| `POST/OPTIONS` | `/api/clock` | Style/color/mode/wallpaper clock |
| `POST/OPTIONS` | `/api/wallpaper` | Chọn URL hoặc yêu cầu sync wallpaper |
| `POST/OPTIONS` | `/api/slideshow` | Cấu hình slideshow |
| `POST` | `/upload` | Compatibility acknowledgement |
| `POST` | `/api/upload` | Compatibility acknowledgement; chưa flash OTA binary |

Không có `/api/brightness` trong HTTP route table hiện tại. Brightness dùng MCP.

Ví dụ mở Assistant:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://<DEVICE_IP>/api/launch" `
  -ContentType "application/json" `
  -Body '{"app":"assistant"}'
```

## Wallpaper

Firmware tải slideshow list từ:

```text
http://<HOST_IP>:8000/api/wallpapers/slideshow
```

Gateway proxy URL/file từ Go Backend. Firmware cache tối đa các slot `wp_0.jpg` đến `wp_9.jpg` trong `/littlefs/wallpapers/` và decode JPEG vào PSRAM RGB565 buffer 320×240.

## Quy tắc ổn định

- Chỉ `AssistantService` điều khiển PA.
- Không sửa `state_` trực tiếp.
- Không block mic/output task.
- Dùng mutex/atomic trong WebSocket callback.
- Dùng EventBus cho state notification.
- HTTP handler không đặt buffer lớn trên stack.
- LVGL chỉ được gọi trong context an toàn của UI/task.
- DMA buffer phải nằm trong internal DMA-capable RAM.
- Không đổi IP cố định hoặc pin board.

Xem thêm [DISPLAY_CONFIG.md](DISPLAY_CONFIG.md).

Chạy host-side contract test trước khi build/flash:

```powershell
python -m unittest discover -s tests -v
```

Test bảo vệ pin map ES3C28P, địa chỉ mạng cố định, cấu hình PCM/ES8311,
core/priority audio task, state transition, quyền điều khiển PA và HTTP route.

## Debug

```powershell
Invoke-RestMethod http://<DEVICE_IP>/api/status
Invoke-RestMethod http://<DEVICE_IP>/api/logs
```

Trong serial log cần thấy:

- ES8311 ready và mic gain;
- Wi-Fi IP `<DEVICE_IP>`;
- MQTT connected;
- WebSocket connected và hello;
- `Wake-word mode armed`;
- state transition `armed/listening/processing/speaking`.

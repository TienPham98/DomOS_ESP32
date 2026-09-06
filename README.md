# DomOS

[English](#english) · [Tiếng Việt](#tieng-viet)

<a id="english"></a>

## English

DomOS is a smart-home UI and voice-assistant ecosystem for the ES3C28P board (ESP32-S3). This repository contains the embedded firmware, Python Voice Gateway, Go Core Backend, Next.js Dashboard, and shared MQTT infrastructure.

The firmware includes a Manchester United fixture app. The Gateway refreshes the next match daily, converts it to the `Asia/Bangkok` timezone, retains a last-known-good cache during provider outages, and renders the result over the club background.

The `Codex Usage` app displays General usage limits: the remaining percentage and reset time for the five-hour and weekly windows. The Gateway reads these through Codex CLI's `account/rateLimits/read`, without starting an AI turn, and returns only a normalized snapshot to the ESP32.

On Northflank, the gateway image includes a pinned Codex CLI. Set `CODEX_USAGE_LOCAL_ENABLED=true`, `CODEX_CLI_PATH=/usr/local/bin/codex`, and `CODEX_USAGE_AUTH_DIR=/data/codex-auth`. Link the existing PostgreSQL `DATABASE_URL` (a `postgresql://` URI), and generate a Fernet key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` on a trusted administrator machine. Store that key as `CODEX_USAGE_AUTH_ENCRYPTION_KEY` in a secret group restricted to the gateway; never commit it or rotate it without migrating the encrypted session. In the gateway's private console, run `python scripts/cloud_codex_login.py login` and complete the displayed ChatGPT device-code login yourself. Use `status` to check login or `logout` to remove it. The helper stores the session encrypted in PostgreSQL and restores it after redeployment; a database lock serializes token refreshes. The authentication file grants account access: protect both the database and encryption key. No PC auth file is uploaded, no public login/command endpoint is added, and no extra service is needed.

Usage refreshes on request when the cache is at least 60 seconds old; failed reads retain the last snapshot and mark it stale after 900 seconds. The PC snapshot sync script remains an optional alternative (`CODEX_USAGE_LOCAL_ENABLED=false` on the cloud). Full-reset credits are not displayed on the board.

### Active architecture

```text
Browser
   ├── HTTP/WS ──> Next.js Dashboard :3000
   │                    ├── REST/WS ──> Go Backend :8081
   │                    ├── REST ─────> Voice Gateway :8000
   │                    └── HTTP ─────> ESP32 :80
   │
ESP32-S3 <DEVICE_IP>
   ├── PCM/WebSocket v3 ──────────────> Python Voice Gateway :8000
   ├── MQTT ──────────────────────────> Broker :1883
   └── HTTP API/WebSocket logs ───────> Dashboard

Python Voice Gateway
   ├── STT: OpenAI, with Google Web Speech fallback
   ├── Wake STT: Google Web Speech (vi-VN + en-US)
   ├── LLM: OpenAI first; OpenRouter only after confirmed quota exhaustion
   ├── TTS: Google TTS; Edge TTS is optional
   ├── MCP JSON-RPC: volume, brightness, and app control
   └── SQLite: backend-python/data/conversations.db
```

### Components

| Component | Directory | Technology | Fixed address |
|---|---|---|---|
| Firmware | `firmware/` | C++17, ESP-IDF 5.3.1, FreeRTOS, LVGL 8.4 | `<DEVICE_IP>:80` |
| Voice Gateway | `backend-python/` | Python, FastAPI, WebSocket, SQLite | `<HOST_IP>:8000` |
| Core Backend | `backend-go/` | Go, Fiber v2, GORM, MQTT | `<HOST_IP>:8081` |
| Dashboard | `dashboard-next/` | Next.js 16, React 19, Tailwind CSS 4 | `<HOST_IP>:3000` |
| MQTT broker | `backend-python/run_broker.py` or Mosquitto | AMQTT/MQTT | `<HOST_IP>:1883` |

### Required network configuration

The following addresses are fixed system configuration and must remain consistent:

| Node | IP/URL |
|---|---|
| Host running all backends | `<HOST_IP>` |
| ESP32-S3 | `<DEVICE_IP>` |
| Wi-Fi | SSID selected on screen; `Dom_12` is the default |
| Voice WebSocket | `ws://<HOST_IP>:8000/api/v1/voice/stream` |
| Go API | `http://<HOST_IP>:8081` |
| Dashboard | `http://<HOST_IP>:3000` |
| MQTT | `mqtt://<HOST_IP>:1883` |

Replace `<HOST_IP>` and `<DEVICE_IP>` with real LAN addresses during deployment. On `Dom_12`, the board keeps its current static address. When another SSID is selected in Wi-Fi Config, the board uses DHCP and the backend must be reachable from that network. Never commit real deployment addresses to public documentation.

### Environment requirements

- Windows PowerShell.
- Python and a virtual environment at `backend-python/.venv`.
- A Go version compatible with `backend-go/go.mod` (currently Go 1.25).
- Node.js and npm.
- ESP-IDF 5.3.1 at `C:\Espressif\frameworks\esp-idf-v5.3.1`.
- ESP-IDF Python environment at `C:\Espressif\python_env\idf5.3_py3.11_env`.
- Board connected on `COM5`, flash baud `460800`.
- PyAV handles TTS decoding and resampling; the current pipeline does not require a separate FFmpeg process.

### First-time setup

#### Voice Gateway and MQTT broker

```powershell
cd "D:\Work space\DomOS\backend-python"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install amqtt
```

Create `D:\Work space\DomOS\.env` or `backend-python\.env`:

```dotenv
OPENROUTER_API_KEY=replace_with_your_key
OPENROUTER_MODEL=openrouter/free
STT_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
OPENAI_STT_MODEL=gpt-4o-mini-transcribe
LLM_PROVIDER_ORDER=openai,openrouter
STT_LANGUAGE=vi-VN
TTS_PROVIDER=google
CONVERSATION_DB_PATH=data/conversations.db
MQTT_BROKER_HOST=<HOST_IP>
MQTT_BROKER_PORT=1883
VOICE_SESSION_TIMEOUT_SEC=30
FOOTBALL_DATA_API_KEY=replace_with_your_football_data_key
FOOTBALL_DATA_BASE_URL=<FOOTBALL_DATA_API_BASE_URL>
MANCHESTER_UNITED_BADGE_URL=<MANCHESTER_UNITED_BADGE_IMAGE_URL>
```

The Gateway prioritizes OpenAI for STT and conversation. OpenRouter is used for LLM responses only when OpenAI explicitly reports exhausted credit or quota. An OpenAI STT failure falls back to Google Web Speech and does not disable the OpenAI LLM path.

`FOOTBALL_DATA_API_KEY` refreshes Manchester United fixtures. Keep the real provider key and URL only in `.env`; never commit them to source code or documentation.

#### Go Backend

```powershell
cd "D:\Work space\DomOS\backend-go"
Copy-Item .env.example .env
```

LAN deployment values:

```dotenv
APP_ENV=development
APP_PORT=8081
MQTT_BROKER=tcp://<HOST_IP>:1883
MQTT_CLIENT_ID=domos-backend
UPLOAD_DIR=./uploads
JWT_SECRET=replace_with_a_long_random_value
```

Go tries PostgreSQL first. If the database is unavailable, it falls back to `backend-go/domos.db` SQLite and keeps the API online.

#### Dashboard

```powershell
cd "D:\Work space\DomOS\dashboard-next"
npm install
```

Create `.env.local`:

```dotenv
NEXT_PUBLIC_API_URL=http://<HOST_IP>:8081
NEXT_PUBLIC_AI_GATEWAY_URL=http://<HOST_IP>:8000
NEXT_PUBLIC_DEVICE_IP=<DEVICE_IP>
```

#### Firmware

```powershell
cd "D:\Work space\DomOS\firmware"
.\build.bat
.\flash.bat
```

`flash.bat` flashes `COM5` and opens the serial monitor. Network addresses are injected from the private root `.env`; the Wi-Fi password and Voice Gateway token can also be configured through `idf.py menuconfig` under `DomOS`.

### Starting the complete system

#### Automatic startup after Windows sign-in

Docker Desktop, the Python virtual environment, the Go binary, and a Dashboard production build (`npm run build`) are required. Run once from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
Start-ScheduledTask -TaskName "DomOS Servers"
```

The hidden scheduled task starts PostgreSQL and Mosquitto through Docker, the native Go backend, Python Gateway, and production Dashboard. Do not start `run_broker.py` or the Go backend container at the same time. Startup logs are written to `.runtime-logs/startup.log`.

Startup validates the HTTP health endpoints and verifies that `DOMOS_HOST_IP` from `.env` is assigned to the PC. If the fixed address is missing after reboot, localhost may still return HTTP 200 while the ESP32 reports `ESP_ERR_HTTP_CONNECT`. The task exits with code 1 so Task Scheduler records the failure and retries it.

A DHCP reservation for `DOMOS_HOST_IP` on the router is recommended. The PC can also keep its ordinary DHCP address while carrying a secondary DomOS address:

```powershell
# Check only; does not modify the network
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\repair-domos-network.ps1

# Apply from an Administrator PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\repair-domos-network.ps1 -Apply
```

The repair script reads the address from `.env`, checks for LAN conflicts through ARP, and moves only the secondary DomOS address from an inactive adapter to the active physical adapter. It enables Windows [DHCP/static IP coexistence](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/netsh-interface), uses `SkipAsSource=true`, and does not replace DHCP, DNS, or the default gateway. It validates those settings after the change and rolls back the DomOS alias if validation fails. The router must still reserve this address so it is not assigned to another device.

Run startup regression checks with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\tests\startup.tests.ps1
```

#### Manual development startup

If the scheduled task is not used, open four terminals in this order.

#### Terminal 1 — MQTT

```powershell
cd "D:\Work space\DomOS\backend-python"
.\.venv\Scripts\python.exe run_broker.py
```

#### Terminal 2 — Go Backend

```powershell
cd "D:\Work space\DomOS\backend-go"
go run .\cmd\server
```

The prebuilt binary can be started with `./server.exe`.

#### Terminal 3 — Voice Gateway

```powershell
cd "D:\Work space\DomOS\backend-python"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

#### Terminal 4 — Dashboard

```powershell
cd "D:\Work space\DomOS\dashboard-next"
npm run dev
```

After the board boots, open Assistant on the display or call:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://<DEVICE_IP>/api/launch" `
  -ContentType "application/json" `
  -Body '{"app":"assistant"}'
```

### Quick checks

```powershell
Invoke-RestMethod http://<HOST_IP>:8000/health
Invoke-RestMethod http://<HOST_IP>:8081/healthz
Invoke-WebRequest -UseBasicParsing http://<HOST_IP>:3000
Invoke-RestMethod http://<DEVICE_IP>/api/status
Get-NetTCPConnection -State Listen |
  Where-Object LocalPort -In 1883,3000,8000,8081
```

Run all Gateway, Go Backend, Dashboard, and firmware tests, lint checks, and production builds:

```powershell
.\test-all.ps1
```

For a faster test cycle without builds:

```powershell
.\test-all.ps1 -SkipBuild
```

Firmware contract tests cover ES3C28P pins, fixed addresses, audio tasks, the assistant state machine, speaker-amplifier ownership, and HTTP routes. The complete command also builds firmware with ESP-IDF 5.3.1 when the toolchain exists at the project path.

The Voice Gateway should report `active_sessions: 1` when the Assistant is connected through WebSocket.

### Voice-assistant flow

1. The board remains `Armed` and streams microphone PCM for wake-word checks.
2. The user says “Hey Dom” or touches the Assistant screen.
3. Wake STT runs `en-US` and `vi-VN` in parallel; the matcher includes transcript signatures observed from the current board and owner pronunciation.
4. The board enters `Listening`. VAD ends the utterance after 9 silent frames inside a 12-frame window.
5. The Gateway sends `listen.processing`; the LCD shows `Thinking`.
6. Vietnamese STT feeds deterministic commands/MCP or the configured LLM pipeline, followed by TTS.
7. The LCD shows `Responding`; 16 kHz PCM is played through the ES8311.
8. After TTS, the system returns to `Armed`.

A successful wake word only enables command listening. The next utterance is the command; during stress tests, do not repeat “Hey Dom” after the LCD already shows `Listening`.

### AI-controllable tools

- `device.get_status`
- `speaker.set_volume` (`0..100`)
- `speaker.adjust_volume` (`-100..100`)
- `display.adjust_brightness` (`-100..100`)
- `app.launch`: `wallpaper`, `clock`, `man-utd`, or `codex-credit`

The Gateway stores user utterances, assistant responses, actual provider/model metadata, timestamps, and tool traces in SQLite. Dashboard `/assistant` reads this history from `/api/v1/conversations`.

### Component documentation

- [Voice Gateway](backend-python/README.md)
- [Go Backend](backend-go/README.md)
- [Dashboard](dashboard-next/README.md)
- [Firmware](firmware/README.md)
- [Display and touch](firmware/DISPLAY_CONFIG.md)

---

<a id="tieng-viet"></a>

## Tiếng Việt

DomOS là hệ sinh thái trợ lý giọng nói và giao diện nhà thông minh chạy trên board ES3C28P (ESP32-S3). Repository gồm firmware nhúng, Voice Gateway Python, Core Backend Go, Dashboard Next.js và MQTT broker dùng chung.

Firmware còn có app lịch Manchester United: dữ liệu trận kế tiếp được Gateway cập nhật hằng ngày, chuyển sang giờ `Asia/Bangkok`, cache để chịu lỗi mạng và hiển thị trên nền logo đội bóng.

App `Codex Usage` chỉ hiển thị General usage limits: phần trăm còn lại và thời gian reset của hạn mức 5 giờ và hàng tuần. Gateway đọc qua `account/rateLimits/read` của Codex CLI, không khởi chạy lượt AI, và chỉ trả snapshot đã chuẩn hóa cho ESP32.

Trên Northflank, image gateway đã tích hợp Codex CLI với phiên bản cố định. Đặt `CODEX_USAGE_LOCAL_ENABLED=true`, `CODEX_CLI_PATH=/usr/local/bin/codex`, `CODEX_USAGE_AUTH_DIR=/data/codex-auth`; liên kết `DATABASE_URL` của PostgreSQL hiện có (URI `postgresql://`). Tạo khóa Fernet trên máy quản trị tin cậy bằng `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, lưu vào secret `CODEX_USAGE_AUTH_ENCRYPTION_KEY` chỉ cấp cho gateway. Không commit hoặc thay khóa khi chưa chuyển đổi phiên đã mã hóa. Trong console riêng của gateway, chạy `python scripts/cloud_codex_login.py login` rồi tự hoàn tất đăng nhập ChatGPT bằng mã thiết bị; dùng `status` để kiểm tra hoặc `logout` để đăng xuất. Phiên được mã hóa trong PostgreSQL, khôi phục sau redeploy và khóa khi làm mới token. File đăng nhập có quyền truy cập tài khoản: cần bảo vệ cả database và khóa mã hóa. Không tải auth từ PC, không mở API đăng nhập/chạy lệnh công khai, không cần thêm service.

Usage được làm mới khi có yêu cầu và cache đã cũ ít nhất 60 giây; khi lỗi sẽ giữ snapshot gần nhất và đánh dấu stale sau 900 giây. Script đồng bộ từ PC vẫn là lựa chọn phụ (`CODEX_USAGE_LOCAL_ENABLED=false` trên cloud). Board không hiển thị full-reset credit.

### Kiến trúc đang hoạt động

```text
Trình duyệt
   ├── HTTP/WS ──> Dashboard Next.js :3000
   │                    ├── REST/WS ──> Go Backend :8081
   │                    ├── REST ─────> Voice Gateway :8000
   │                    └── HTTP ─────> ESP32 :80
   │
ESP32-S3 <DEVICE_IP>
   ├── PCM/WebSocket v3 ──────────────> Python Voice Gateway :8000
   ├── MQTT ──────────────────────────> Broker :1883
   └── HTTP API/WebSocket log ────────> Dashboard

Python Voice Gateway
   ├── STT: OpenAI, dự phòng bằng Google Web Speech
   ├── Wake STT: Google Web Speech (vi-VN + en-US)
   ├── LLM: ưu tiên OpenAI; chỉ dùng OpenRouter khi xác nhận hết quota
   ├── TTS: Google TTS; Edge TTS là lựa chọn bổ sung
   ├── MCP JSON-RPC: điều khiển volume, brightness và app
   └── SQLite: backend-python/data/conversations.db
```

### Thành phần

| Thành phần | Thư mục | Công nghệ | Địa chỉ cố định |
|---|---|---|---|
| Firmware | `firmware/` | C++17, ESP-IDF 5.3.1, FreeRTOS, LVGL 8.4 | `<DEVICE_IP>:80` |
| Voice Gateway | `backend-python/` | Python, FastAPI, WebSocket, SQLite | `<HOST_IP>:8000` |
| Core Backend | `backend-go/` | Go, Fiber v2, GORM, MQTT | `<HOST_IP>:8081` |
| Dashboard | `dashboard-next/` | Next.js 16, React 19, Tailwind CSS 4 | `<HOST_IP>:3000` |
| MQTT broker | `backend-python/run_broker.py` hoặc Mosquitto | AMQTT/MQTT | `<HOST_IP>:1883` |

### Cấu hình mạng bắt buộc

Các địa chỉ sau là cấu hình cố định của hệ thống và phải được giữ nguyên:

| Node | IP/URL |
|---|---|
| Host chạy tất cả backend | `<HOST_IP>` |
| ESP32-S3 | `<DEVICE_IP>` |
| Wi-Fi | SSID cấu hình trên màn hình; `Dom_12` là mặc định |
| Voice WebSocket | `ws://<HOST_IP>:8000/api/v1/voice/stream` |
| Go API | `http://<HOST_IP>:8081` |
| Dashboard | `http://<HOST_IP>:3000` |
| MQTT | `mqtt://<HOST_IP>:1883` |

Thay `<HOST_IP>` và `<DEVICE_IP>` bằng địa chỉ LAN thật khi triển khai. Với `Dom_12`, board giữ địa chỉ tĩnh hiện tại. Khi chọn SSID khác trong Wi-Fi Config, board tự dùng DHCP; backend phải truy cập được từ mạng mới. Không commit địa chỉ triển khai thật vào tài liệu public.

### Yêu cầu môi trường

- Windows PowerShell.
- Python và virtual environment tại `backend-python/.venv`.
- Go phù hợp với `backend-go/go.mod` (hiện khai báo Go 1.25).
- Node.js và npm.
- ESP-IDF 5.3.1 tại `C:\Espressif\frameworks\esp-idf-v5.3.1`.
- ESP-IDF Python env tại `C:\Espressif\python_env\idf5.3_py3.11_env`.
- Board nối tại `COM5`, baud flash `460800`.
- PyAV xử lý decode/resample TTS; pipeline hiện tại không yêu cầu tiến trình FFmpeg riêng.

### Setup lần đầu

#### Voice Gateway và MQTT broker

```powershell
cd "D:\Work space\DomOS\backend-python"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install amqtt
```

Tạo `D:\Work space\DomOS\.env` hoặc `backend-python\.env`:

```dotenv
OPENROUTER_API_KEY=thay_bang_key_cua_ban
OPENROUTER_MODEL=openrouter/free
STT_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
OPENAI_STT_MODEL=gpt-4o-mini-transcribe
LLM_PROVIDER_ORDER=openai,openrouter
STT_LANGUAGE=vi-VN
TTS_PROVIDER=google
CONVERSATION_DB_PATH=data/conversations.db
MQTT_BROKER_HOST=<HOST_IP>
MQTT_BROKER_PORT=1883
VOICE_SESSION_TIMEOUT_SEC=30
FOOTBALL_DATA_API_KEY=thay_bang_football_data_key
FOOTBALL_DATA_BASE_URL=<FOOTBALL_DATA_API_BASE_URL>
MANCHESTER_UNITED_BADGE_URL=<MANCHESTER_UNITED_BADGE_IMAGE_URL>
```

Gateway ưu tiên OpenAI cho STT và hội thoại. LLM chỉ chuyển sang OpenRouter khi
OpenAI trả lỗi xác nhận hết credit hoặc quota. Nếu OpenAI STT lỗi, âm thanh được
chuyển sang Google Web Speech mà không vô hiệu hóa nhánh OpenAI LLM.

`FOOTBALL_DATA_API_KEY` dùng để cập nhật lịch Manchester United theo thời gian thực. Chỉ lưu key và URL nhà cung cấp thật trong `.env`; không commit chúng vào mã nguồn hoặc tài liệu.

#### Go Backend

```powershell
cd "D:\Work space\DomOS\backend-go"
Copy-Item .env.example .env
```

Giá trị triển khai LAN cần dùng:

```dotenv
APP_ENV=development
APP_PORT=8081
MQTT_BROKER=tcp://<HOST_IP>:1883
MQTT_CLIENT_ID=domos-backend
UPLOAD_DIR=./uploads
JWT_SECRET=thay_bang_chuoi_ngau_nhien_dai
```

Go thử PostgreSQL trước; nếu không kết nối được, code tự chuyển sang SQLite `backend-go/domos.db` và vẫn chạy API.

#### Dashboard

```powershell
cd "D:\Work space\DomOS\dashboard-next"
npm install
```

Tạo `.env.local`:

```dotenv
NEXT_PUBLIC_API_URL=http://<HOST_IP>:8081
NEXT_PUBLIC_AI_GATEWAY_URL=http://<HOST_IP>:8000
NEXT_PUBLIC_DEVICE_IP=<DEVICE_IP>
```

#### Firmware

```powershell
cd "D:\Work space\DomOS\firmware"
.\build.bat
.\flash.bat
```

`flash.bat` flash COM5 và mở monitor. Địa chỉ mạng được nạp từ `.env` gốc vào cấu hình build riêng tư; Wi-Fi password và token Voice Gateway vẫn có thể đặt bằng `idf.py menuconfig` trong menu `DomOS`.

### Khởi động toàn hệ thống

#### Tự khởi động khi đăng nhập Windows

Cần Docker Desktop, Python virtualenv, binary Go và dashboard đã chạy `npm run build`.
Tại thư mục gốc, chạy một lần:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
Start-ScheduledTask -TaskName "DomOS Servers"
```

Tác vụ chạy ẩn sau khi đăng nhập Windows: bật PostgreSQL và Mosquitto bằng Docker,
Go native, Python Gateway và dashboard production. Không chạy thêm `run_broker.py`
hoặc Go container khi đang dùng tác vụ này. Log nằm trong `.runtime-logs/startup.log`.

Startup kiểm tra HTTP health và địa chỉ `DOMOS_HOST_IP` trong `.env` có thực sự được
gán cho PC. Nếu địa chỉ bị DHCP thay đổi sau reboot, API vẫn có thể trả HTTP 200 ở
`localhost` trong khi ESP32 báo `ESP_ERR_HTTP_CONNECT`. Startup trả exit code 1
trong trường hợp này để Task Scheduler thử lại và ghi nhận lỗi chính xác.

Nên giữ địa chỉ `DOMOS_HOST_IP` bằng DHCP reservation trên router. Trên PC có thể
kiểm tra, rồi bổ sung địa chỉ DomOS cố định trên cùng card mạng:

```powershell
# Chỉ kiểm tra, không thay đổi mạng
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\repair-domos-network.ps1

# Chạy từ PowerShell Administrator để áp dụng
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\repair-domos-network.ps1 -Apply
```

Script đọc địa chỉ từ `.env`, kiểm tra xung đột bằng ARP và chỉ di chuyển địa chỉ
DomOS phụ từ card mạng không hoạt động sang card vật lý đang hoạt động. Nó dùng
[DHCP/static IP coexistence của Windows](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/netsh-interface),
đặt `SkipAsSource=true` và không thay thế DHCP, DNS hoặc default gateway. Sau khi
thay đổi, script kiểm tra lại các cấu hình này và rollback alias DomOS nếu có sai
lệch. Router vẫn cần reserve địa chỉ để không cấp nó cho thiết bị khác.

Chạy kiểm tra hồi quy startup bằng
`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\tests\startup.tests.ps1`.

#### Khởi động thủ công khi phát triển

Nếu không dùng tác vụ trên, mở bốn terminal theo thứ tự sau.

#### Terminal 1 — MQTT

```powershell
cd "D:\Work space\DomOS\backend-python"
.\.venv\Scripts\python.exe run_broker.py
```

#### Terminal 2 — Go Backend

```powershell
cd "D:\Work space\DomOS\backend-go"
go run .\cmd\server
```

Có thể dùng binary đã build: `./server.exe`.

#### Terminal 3 — Voice Gateway

```powershell
cd "D:\Work space\DomOS\backend-python"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

#### Terminal 4 — Dashboard

```powershell
cd "D:\Work space\DomOS\dashboard-next"
npm run dev
```

Sau khi board boot, mở Assistant trên màn hình hoặc gọi:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://<DEVICE_IP>/api/launch" `
  -ContentType "application/json" `
  -Body '{"app":"assistant"}'
```

### Kiểm tra nhanh

```powershell
Invoke-RestMethod http://<HOST_IP>:8000/health
Invoke-RestMethod http://<HOST_IP>:8081/healthz
Invoke-WebRequest -UseBasicParsing http://<HOST_IP>:3000
Invoke-RestMethod http://<DEVICE_IP>/api/status
Get-NetTCPConnection -State Listen |
  Where-Object LocalPort -In 1883,3000,8000,8081
```

Chạy toàn bộ test, lint và production build của Gateway, Go Backend,
Dashboard và firmware:

```powershell
.\test-all.ps1
```

Khi chỉ cần vòng test nhanh, bỏ qua các bước build:

```powershell
.\test-all.ps1 -SkipBuild
```

Firmware contract test kiểm tra pin ES3C28P, IP cố định, task audio, state
machine, quyền điều khiển PA và HTTP route. Lệnh đầy đủ còn build firmware
bằng ESP-IDF 5.3.1 nếu toolchain tồn tại tại đường dẫn chuẩn của dự án.

Voice Gateway phải báo `active_sessions: 1` khi app Assistant đã mở và WebSocket kết nối.

### Luồng trợ lý giọng nói

1. Board ở trạng thái `Armed`, gửi PCM mic để kiểm tra wake word.
2. Người dùng nói “Hey Dom” hoặc chạm màn hình Assistant.
3. Wake STT chạy song song `en-US` và `vi-VN`; matcher hỗ trợ các transcript gần âm thanh thực tế của board.
4. Board chuyển `Listening`. VAD chốt câu sau 9 frame im lặng trong cửa sổ 12 frame.
5. Gateway gửi `listen.processing`; LCD hiện `Thinking`.
6. STT tiếng Việt → lệnh trực tiếp/MCP hoặc OpenAI; OpenRouter chỉ dự phòng khi hết quota → TTS.
7. LCD hiện `Responding`; PCM 16 kHz được phát qua ES8311.
8. Kết thúc TTS, hệ thống trở về `Armed`.

Wake word thành công chỉ kích hoạt chế độ nghe. Câu nói kế tiếp là lệnh; khi stress test không nên lặp “Hey Dom” ngay sau khi LCD đã hiện `Listening`.

### Công cụ AI có thể điều khiển

- `device.get_status`
- `speaker.set_volume` (`0..100`)
- `speaker.adjust_volume` (`-100..100`)
- `display.adjust_brightness` (`-100..100`)
- `app.launch`: `wallpaper`, `clock`, `man-utd` hoặc `codex-credit`

Gateway lưu câu người dùng, câu trả lời, model, thời gian và trace tool vào SQLite. Dashboard `/assistant` đọc lịch sử từ `/api/v1/conversations`.

### Tài liệu thành phần

- [Voice Gateway](backend-python/README.md)
- [Go Backend](backend-go/README.md)
- [Dashboard](dashboard-next/README.md)
- [Firmware](firmware/README.md)
- [Display và touch](firmware/DISPLAY_CONFIG.md)

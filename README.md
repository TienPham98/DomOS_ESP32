# DomOS

[English](#english) · [Tiếng Việt](#tiếng-việt)

## English

DomOS is a voice-first smart display for the ES3C28P board based on ESP32-S3. The repository contains the embedded firmware, cloud voice gateway, core backend, web dashboard, MQTT integration, and automated tests.

### Architecture

```text
User voice/touch
       |
       v
ESP32-S3 firmware <---- WebSocket Voice Protocol v3 ----> Python Voice Gateway
       |                                                   |-- STT
       |                                                   |-- intent/tools
       |                                                   |-- OpenAI -> OpenRouter fallback
       |                                                   |-- real-time web search
       |                                                   `-- Vietnamese TTS
       |
       +---------------- MQTT ----------------------------> Go Core Backend
                                                            |
Browser <---------------- Next.js Dashboard ----------------+
```

| Component | Directory | Main responsibility |
|---|---|---|
| Firmware | `firmware/` | Display, touch, audio, local wake word, apps, device tools |
| Voice Gateway | `backend-python/` | Voice sessions, STT/LLM/TTS, web search, history, tracking data |
| Core Backend | `backend-go/` | Users, devices, themes, wallpapers, OTA, MQTT |
| Dashboard | `dashboard-next/` | Monitoring, conversation history, board control, content management |

The assistant uses local ESP-SR processing for wake detection and endpointing. Vietnamese speech recognition, reasoning, web search, and speech synthesis run through the gateway. Hardware commands use deterministic intents and require a device acknowledgement before success is announced.

The `Tracking Status` app alternates every 15 seconds between Manchester United fixtures and Codex usage. Navigation controls stay hidden during automatic rotation and reappear only after user interaction.

### Quick start

Copy the environment template and keep the real file private:

```powershell
Copy-Item .env.example .env
```

Start each component in a separate terminal:

```powershell
# Python Voice Gateway
cd backend-python
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000

# Go Core Backend
cd ..\backend-go
go run .\cmd\server

# Next.js Dashboard
cd ..\dashboard-next
npm install
npm run dev
```

Build and flash the firmware from an ESP-IDF shell:

```powershell
cd firmware
idf.py build
idf.py -p <SERIAL_PORT> -b <FLASH_BAUD> flash monitor
```

Run the complete verification suite:

```powershell
.\test-all.ps1
```

### Configuration and security

Runtime addresses and credentials are supplied only through private environment files, deployment secrets, or the ignored firmware build configuration. Public documentation uses variable names and placeholders only.

Never commit:

- `.env`, `.env.local`, platform secret exports, or generated `sdkconfig` files;
- AI, search, football, MQTT, JWT, board-control, voice, or synchronization tokens;
- real service domains, broker addresses, device addresses, Wi-Fi credentials, or MAC addresses;
- Codex authentication data, databases, logs, flash backups, firmware dumps, or user content.

Variables prefixed with `NEXT_PUBLIC_` are visible in the browser and must never contain credentials. Rotate a credential immediately if it is exposed.

See the component READMEs for implementation and test details.

## Tiếng Việt

DomOS là hệ điều hành màn hình thông minh điều khiển bằng giọng nói cho board ES3C28P sử dụng ESP32-S3. Kho mã gồm firmware nhúng, voice gateway trên cloud, core backend, dashboard web, MQTT và bộ kiểm thử tự động.

### Kiến trúc

```text
Giọng nói/chạm
      |
      v
Firmware ESP32-S3 <---- WebSocket Voice Protocol v3 ----> Python Voice Gateway
      |                                                   |-- STT
      |                                                   |-- intent/tool
      |                                                   |-- OpenAI -> OpenRouter dự phòng
      |                                                   |-- tìm kiếm thời gian thực
      |                                                   `-- TTS tiếng Việt
      |
      +---------------- MQTT ----------------------------> Go Core Backend
                                                           |
Trình duyệt <------------- Next.js Dashboard --------------+
```

| Thành phần | Thư mục | Trách nhiệm chính |
|---|---|---|
| Firmware | `firmware/` | Màn hình, cảm ứng, âm thanh, wake word cục bộ, ứng dụng, device tool |
| Voice Gateway | `backend-python/` | Voice session, STT/LLM/TTS, web search, lịch sử, dữ liệu tracking |
| Core Backend | `backend-go/` | Người dùng, thiết bị, theme, wallpaper, OTA, MQTT |
| Dashboard | `dashboard-next/` | Giám sát, lịch sử hội thoại, điều khiển board, quản lý nội dung |

ESP-SR xử lý wake word và điểm kết thúc câu nói ngay trên board. Nhận dạng tiếng Việt, suy luận, tra cứu web và tổng hợp giọng nói chạy qua gateway. Lệnh phần cứng đi theo intent xác định và chỉ báo thành công sau khi board phản hồi.

Ứng dụng `Tracking Status` luân phiên mỗi 15 giây giữa lịch Manchester United và Codex usage. Nút điều hướng không xuất hiện lại khi tự chuyển màn hình, chỉ hiện khi người dùng tương tác.

### Khởi động nhanh

Sao chép file cấu hình mẫu và chỉ điền giá trị thật trong file riêng tư:

```powershell
Copy-Item .env.example .env
```

Khởi động từng thành phần ở terminal riêng:

```powershell
# Python Voice Gateway
cd backend-python
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000

# Go Core Backend
cd ..\backend-go
go run .\cmd\server

# Next.js Dashboard
cd ..\dashboard-next
npm install
npm run dev
```

Build và flash firmware trong ESP-IDF shell:

```powershell
cd firmware
idf.py build
idf.py -p <SERIAL_PORT> -b <FLASH_BAUD> flash monitor
```

Chạy toàn bộ kiểm thử:

```powershell
.\test-all.ps1
```

### Cấu hình và bảo mật

Địa chỉ runtime và thông tin xác thực chỉ được lưu trong file môi trường riêng tư, secret của nền tảng deploy hoặc cấu hình firmware đã bị Git bỏ qua. Tài liệu công khai chỉ dùng tên biến và placeholder.

Không được commit:

- `.env`, `.env.local`, bản export secret hoặc `sdkconfig` được sinh khi build;
- key/token AI, search, football, MQTT, JWT, điều khiển board, voice hoặc đồng bộ;
- domain dịch vụ thật, địa chỉ broker/thiết bị, Wi-Fi hoặc MAC;
- dữ liệu đăng nhập Codex, database, log, flash backup, firmware dump hoặc nội dung người dùng.

Biến bắt đầu bằng `NEXT_PUBLIC_` sẽ xuất hiện trong bundle trình duyệt nên tuyệt đối không chứa secret. Nếu một credential bị lộ, phải thu hồi và tạo lại ngay.

Đọc README trong từng thành phần để biết chi tiết triển khai và kiểm thử.

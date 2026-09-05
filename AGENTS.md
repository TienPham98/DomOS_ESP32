# AGENTS.md — DomOS

## Project

DomOS is an end-to-end Smart Home OS and Voice AI ecosystem running on ES3C28P hardware (ESP32-S3). It consists of four subsystems that work together:

- **Firmware** (`firmware/`) — C++17 / ESP-IDF 5.3+, FreeRTOS, LVGL 8.4, ES3C28P board
- **Python AI Gateway** (`backend-python/`) — FastAPI, WebSocket Voice Protocol v3, STT/LLM/TTS pipeline
- **Go Core Backend** (`backend-go/`) — Fiber v2, GORM, device registry, MQTT dispatcher
- **Next.js Dashboard** (`dashboard-next/`) — Next.js 15, React 19, Tailwind CSS v4

## Dom AI Assistant Architecture

The voice assistant is the central feature. It spans **firmware <-> Python gateway** over a full-duplex WebSocket.

### Wire Protocol (Dom Voice Protocol v3)

```
ESP32 --hello--> Gateway   handshake, audio_params negotiation
ESP32 <-hello-- Gateway    session_id, features ack
ESP32 --[PCM]--> Gateway   continuous 60ms audio chunks (16kHz, 16-bit, mono)
ESP32 <-stt--   Gateway    transcript text for LCD subtitle
ESP32 <-llm--   Gateway    assistant text + emotion tag for avatar UI
ESP32 <-tts--   Gateway    state: start / sentence_start / stop
ESP32 <-[PCM]-- Gateway    synthesised speech audio (raw PCM or MP3)
ESP32 --listen-> Gateway   explicit start/stop control
ESP32 --abort--> Gateway   interrupt ongoing TTS
ESP32 <->mcp--  Gateway    JSON-RPC 2.0 tool bridge (bidirectional)
```

### Firmware State Machine (AssistantService)

```
Idle -> Connecting -> Listening -> Processing -> Speaking -> Listening (loop)
```

Key files:
- `firmware/main/services/assistant/assistant_service.h` — interface + state enum
- `firmware/main/services/assistant/assistant_service.cpp` — full state machine + protocol handlers
- `firmware/main/services/assistant/ws_client.h/cpp` — esp_websocket_client wrapper
- `firmware/main/services/assistant/audio_pipeline.h/cpp` — FreeRTOS mic/speaker tasks
- `firmware/main/main.cpp` — boot entry, AssistantConfig injection

### Python AI Gateway Pipeline (_run_pipeline)

```
VAD trigger -> STT (faster-whisper / Whisper API)
            -> MQTT smart home dispatch
            -> LLM (GPT-4o / Claude / Gemini / Ollama / offline)
            -> Emotion detect -> send llm message
            -> TTS sentence streaming (Edge-TTS -> PCM via ffmpeg)
            -> Auto return to LISTENING (continuous dialogue)
```

Key files:
- `backend-python/main.py` — FastAPI app, /api/v1/voice/stream WS endpoint
- `backend-python/services/session_manager.py` — VoiceSession, VAD (RMS), SessionManager
- `backend-python/services/stt_service.py` — faster-whisper + OpenAI Whisper fallback
- `backend-python/services/llm_service.py` — LLM router, Dom persona system prompt
- `backend-python/services/tts_service.py` — Edge-TTS streaming, ffmpeg PCM conversion
- `backend-python/services/mcp_service.py` — MCP JSON-RPC 2.0 bridge
- `backend-python/config.py` — Pydantic Settings (env vars)

## Required Rules

### Firmware
- Do NOT change the existing board structure (`board/es3c28p/`). Hardware pins are defined in `board_config.h`.
- `AssistantService` is the only component that calls `board_->SetPAEnabled()` for the speaker amplifier.
- State transitions MUST go through `AssistantService::SetState()` — never modify `state_` directly.
- Audio pipeline tasks run on dedicated cores (mic=core1, output=core0) at priority 7. Do not block these tasks.
- WebSocket callbacks (`OnWsText`, `OnWsBinary`, `OnWsConnect`) run in the ESP WebSocket client task — use mutex for shared state.
- The `EventBus` must be used to notify other subsystems (AppManager, UI) of assistant state changes.
- Do not add board-specific includes to `assistant_service.cpp` beyond `board/es3c28p/board_es3c28p.h`.
- Wi-Fi credentials are user-configurable: `Dom_12` keeps the dedicated fixed
  IP configuration, while any other SSID must use DHCP.

### Python AI Gateway
- The WebSocket endpoint path is `/api/v1/voice/stream` — do NOT change this path.
- Session state machine: `IDLE -> LISTENING -> PROCESSING -> SPEAKING -> LISTENING`.
- VAD parameters (`VAD_ENERGY_THRESHOLD=180`, `VAD_SILENCE_FRAMES=9`) are tuned for 60ms PCM chunks at 16kHz. Do not change frame assumptions.
- TTS must send both `tts.sentence_start` JSON and binary audio per sentence for LCD subtitle sync.
- After TTS completes, always return session to `LISTENING` and send `{"type":"llm","emotion":"idle","text":""}`.
- MCP bridge is bidirectional — gateway can send `tools/call` to device and device can send `tools/list` to gateway.
- All LLM providers must fall back gracefully to `_call_ollama()` then to offline rule-based responses.

### Configuration
- Firmware: set `CONFIG_DOMOS_AI_WS_URI` and optionally `CONFIG_DOMOS_AI_AUTH_TOKEN` via `idf.py menuconfig`.
- Backend: copy `.env.example` to `.env` and fill in API keys. `TTS_VOICE` defaults to `vi-VN-HoaiMyNeural`.

## Commands

### Firmware
```sh
# Configure (set Wi-Fi SSID, WS URI, etc.)
idf.py menuconfig

# Build
idf.py build

# Flash + monitor (COM5, 460800 baud)
idf.py -p COM5 -b 460800 flash monitor
```

### Python AI Gateway
```sh
cd backend-python
pip install -r requirements.txt
copy .env.example .env   # then edit API keys
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Permanent Fixed Network Configuration (DO NOT CHANGE)

The entire DomOS ecosystem operates on a fixed, dedicated IP configuration:

| Node / Service | Fixed IP & Port | Role / Endpoint |
|---|---|---|
| **Host PC (All Backends)** | `<HOST_IP>` | Server host machine running all backend services |
| ├── Python AI Voice Gateway | `http://<HOST_IP>:8000` | WS Voice Protocol: `ws://<HOST_IP>:8000/api/v1/voice/stream` |
| ├── Go Core Backend | `http://<HOST_IP>:8081` | Device registry, REST API, MQTT (`:8081`) |
| └── Next.js Dashboard | `http://<HOST_IP>:3000` | Web UI Control (`http://localhost:3000`) |
| **ESP32-S3 Hardware Board** | `<DEVICE_IP>` | Client terminal on `Dom_12`; other SSIDs use DHCP |
| └── ESP32 HTTP Server | `http://<DEVICE_IP>:80` | Status (`/api/status`), Wallpaper (`/api/wallpaper`), OTA |

> **STRICT RULE FOR ALL AGENTS:**
> - NEVER change or overwrite these fixed IP addresses (`<HOST_IP>` for Host Server, `<DEVICE_IP>` for ESP32 device).
> - All configs (`.env`, `.env.local`, `sdkconfig.defaults`, `main.cpp`, `hooks`, etc.) must remain locked to these fixed addresses.

## Hardware — ES3C28P Board

| Component | GPIO | Notes |
|-----------|------|-------|
| LCD ILI9341 | MOSI=11, MISO=13, SCLK=12, CS=10, DC=46, BL=45 | SPI2, 40MHz |
| Touch FT6336G | SDA=16, SCL=15, RST=18, INT=17 | I2C0 @ 400kHz, addr 0x38 |
| Audio ES8311 | MCLK=4, BCLK=5, WS=7, DIN=6, DOUT=8, PA=1 | I2S0, 16kHz, 16-bit mono |
| Flash | COM5, baud 460800 | Device IP: <DEVICE_IP> (Wi-Fi SSID: Dom_12) |

Audio note: PA pin (GPIO1) is active-low — set LOW to enable speaker, HIGH to mute.

## Authoritative Documentation

- System overview: `README.md`
- AI architecture: `docs/`
- Agent rules: this file (AGENTS.md)

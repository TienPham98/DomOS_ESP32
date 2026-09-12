# DomOS Python Voice Gateway

FastAPI service that connects ESP32-S3 audio and device tools to cloud AI providers. It also supplies conversation history, wallpapers, Manchester United fixtures, Codex usage, and server-side board control.

## Main flow

```text
ESP32 PCM
  -> wake/VAD event
  -> Vietnamese STT
  -> ASR noise filter
  -> deterministic intent or LLM
  -> optional real-time web search
  -> MCP device acknowledgement
  -> text normalization
  -> sentence-chunked Vietnamese TTS
  -> ESP32 PCM
```

- OpenAI is the primary STT and LLM provider.
- OpenRouter is used only after confirmed OpenAI quota exhaustion.
- Google Web Speech is an STT fallback; Google TTS is the default speech output.
- No local LLM or Ollama path is active.
- Recent conversation turns and tool traces are persisted for follow-up questions and the dashboard.
- Time-sensitive questions are searched by the gateway. Structured fast paths return exact values when available and suppress provider-generated tool syntax.

## Structure

```text
backend-python/
|-- main.py                         FastAPI routes and startup
|-- config.py                       environment settings
|-- run_broker.py                   local MQTT broker
|-- cloud-entrypoint.sh             gateway and broker container entrypoint
|-- services/
|   |-- openrouter_voice_service.py voice protocol and AI pipeline
|   |-- assistant_prompt.py         Dom persona and current context
|   |-- app_commands.py             deterministic app intents
|   |-- text_normalization.py       ASR/TTS cleanup and chunking
|   |-- web_search_service.py       real-time search adapter
|   |-- conversation_store.py       history and tool traces
|   |-- football_service.py         fixture data and image cache
|   |-- codex_usage_service.py      normalized usage snapshot
|   `-- codex_auth_store.py         encrypted cloud CLI session
|-- scripts/                         smoke test and usage helpers
`-- tests/                           unit, protocol, and API tests
```

## Private configuration

`config.py` reads the root `.env` and then `backend-python/.env`. Start from `.env.example`; never place real values in this README.

Important variable groups:

| Area | Variables |
|---|---|
| Runtime | `HOST`, `PORT`, `DOMOS_DEBUG` |
| OpenAI | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_STT_MODEL` |
| OpenRouter | `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, `OPENROUTER_AUDIO_MODEL` |
| Routing | `LLM_PROVIDER_ORDER`, `STT_PROVIDER`, `STT_LANGUAGE`, `STT_OPENROUTER_FALLBACK` |
| Wake | `WAKE_STT_PROVIDER`, `WAKE_STT_TIMEOUT_SEC`, `WAKE_STT_OPENAI_FALLBACK` |
| TTS | `TTS_PROVIDER`, `TTS_VOICE`, `TTS_TIMEOUT_SEC` |
| Search | `WEB_SEARCH_PROVIDER`, `WEB_SEARCH_BASE_URL`, `TAVILY_API_KEY`, `SERPER_API_KEY` |
| Device | `VOICE_AUTH_TOKEN`, `BOARD_CONTROL_AUTH_TOKEN`, `DEVICE_COMMAND_RETRY_WINDOW_SEC` |
| MQTT | `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` |
| Storage | `CONVERSATION_DB_PATH`, `DATABASE_URL` |
| Tracking | `FOOTBALL_DATA_API_KEY`, `FOOTBALL_DATA_BASE_URL`, `MANCHESTER_UNITED_BADGE_URL` |
| Codex | `CODEX_USAGE_*`, `CODEX_CLI_PATH`, `CODEX_USAGE_AUTH_ENCRYPTION_KEY` |

All variables ending in `KEY`, `TOKEN`, `PASSWORD`, `SECRET`, authentication data, and every real service address are private deployment values.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Use `--reload` only for local development. `run_broker.py` is optional when an external MQTT broker is already available.

## Public route surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Runtime/provider status without credential values |
| `WS` | `/api/v1/voice/stream` | Dom Voice Protocol v3 |
| `GET/POST` | `/api/device/*` | Cloud board status and authenticated control |
| `GET` | `/api/v1/conversations` | Conversation and tool history |
| `GET` | `/api/football/manchester-united` | Cached fixture snapshot |
| `GET` | `/api/codex/usage` | Normalized usage windows |
| `GET/POST/DELETE` | `/api/wallpaper*` | Wallpaper proxy and management |

Do not expose the voice, board-control, usage-sync, or MQTT credentials in route responses or logs.

## Protocol summary

Protocol v3 uses 16 kHz, signed 16-bit mono PCM in 60 ms frames. JSON messages include `hello`, `listen`, `stt`, `llm`, `tts`, `abort`, and bidirectional JSON-RPC `mcp`. Each TTS sentence sends metadata before binary audio so the firmware can synchronize state and playback.

## Test

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe -m compileall -q main.py config.py services
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

The cloud smoke script validates handshake, STT, response generation, TTS states, and binary audio. Supply its target and authorization only through private environment variables.

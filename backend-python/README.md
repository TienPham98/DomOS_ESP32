# DomOS Python Voice Gateway

Trong tài liệu, thay `<HOST_IP>` và `<DEVICE_IP>` bằng địa chỉ của môi trường triển khai; không commit địa chỉ thật vào README public.

FastAPI gateway kết nối ESP32-S3 với các dịch vụ AI cloud. Service nhận PCM qua WebSocket, phát hiện wake word/VAD, nhận dạng tiếng Việt bằng OpenAI trước, gọi ChatGPT, thực thi MCP trên board, tổng hợp giọng nói và lưu lịch sử hội thoại.

## Trạng thái triển khai

- HTTP: `http://<HOST_IP>:8000`
- Voice WebSocket: `ws://<HOST_IP>:8000/api/v1/voice/stream`
- AI chính: OpenAI `gpt-4o-mini`
- LLM dự phòng: OpenRouter chỉ khi OpenAI trả lỗi hết credit/quota
- Local AI: tắt hoàn toàn
- STT mặc định: OpenAI `gpt-4o-mini-transcribe`; Google Web Speech dự phòng
- TTS mặc định: Google TTS; có Edge TTS
- Memory: SQLite `data/conversations.db`
- Protocol: Dom Voice Protocol v3, PCM 16 kHz, 16-bit, mono, frame 60 ms/1920 byte

## Cấu trúc

```text
backend-python/
├── main.py                              FastAPI app và route
├── config.py                            Pydantic Settings
├── run_broker.py                        MQTT broker development :1883
├── requirements.txt
├── services/
│   ├── openrouter_voice_service.py      session, VAD, STT, LLM, MCP, TTS
│   ├── app_commands.py                  nhận diện tên/lệnh mở app, xác nhận MCP
│   ├── football_service.py              lịch thi đấu MU, cache và ảnh nền LCD
│   ├── codex_usage_service.py           đọc/cache hạn mức Codex đã chuẩn hóa
│   └── conversation_store.py            SQLite conversation/tool trace
├── scripts/sync_codex_usage.py          đồng bộ usage từ PC lên gateway cloud
├── tests/test_voice_protocol.py
└── data/conversations.db                 tạo tự động
```

Các service AI cục bộ cũ (`llm_service.py`, `stt_service.py`, `tts_service.py`, `session_manager.py`) không còn thuộc pipeline hiện tại.

## Setup

```powershell
cd "D:\Work space\DomOS\backend-python"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install amqtt
```

`amqtt` hiện được dùng riêng bởi `run_broker.py` nhưng chưa nằm trong `requirements.txt`, vì vậy cần cài thêm khi tạo môi trường mới. Có thể bỏ bước này nếu dùng Mosquitto bên ngoài.

`config.py` đọc biến từ root `.env` trước và `backend-python/.env` sau. Tạo một trong hai file với key thật, không commit secret:

```dotenv
APP_NAME=DomOS AI Voice Gateway
HOST=0.0.0.0
PORT=8000
DOMOS_DEBUG=false

OPENROUTER_API_KEY=thay_bang_openrouter_api_key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=openrouter/free
OPENROUTER_AUDIO_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
OPENROUTER_TIMEOUT_SEC=60

STT_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
OPENAI_STT_MODEL=gpt-4o-mini-transcribe
LLM_PROVIDER_ORDER=openai,openrouter
LLM_STREAMING_ENABLED=true
ASSISTANT_LOCATION=Việt Nam
ASSISTANT_TIMEZONE=Asia/Bangkok
WEB_SEARCH_PROVIDER=auto
WEB_SEARCH_BASE_URL=<WEB_SEARCH_PROVIDER_URL>
WEB_SEARCH_TIMEOUT_SEC=8
WEB_SEARCH_MAX_RESULTS=3
TAVILY_API_KEY=
SERPER_API_KEY=
STT_LANGUAGE=vi-VN
WAKE_STT_PROVIDER=configured
WAKE_STT_TIMEOUT_SEC=8
WAKE_STT_OPENAI_FALLBACK=true
WAKE_STT_FALLBACK_MIN_SPEECH_FRAMES=5
WAKE_STT_FALLBACK_MIN_PEAK_RMS=400
TTS_PROVIDER=google
TTS_VOICE=vi-VN-HoaiMyNeural
TTS_TIMEOUT_SEC=15

CONVERSATION_DB_PATH=data/conversations.db
MQTT_BROKER_HOST=<HOST_IP>
MQTT_BROKER_PORT=1883
MQTT_CLIENT_ID=domos-ai-gateway
MQTT_USERNAME=
MQTT_PASSWORD=

VOICE_SESSION_TIMEOUT_SEC=30
VOICE_HEARTBEAT_INTERVAL_SEC=20
VOICE_AUTH_TOKEN=

FOOTBALL_DATA_API_KEY=thay_bang_football_data_key
FOOTBALL_DATA_BASE_URL=<FOOTBALL_DATA_API_BASE_URL>
FOOTBALL_TEAM_ID=66
FOOTBALL_TIMEZONE=Asia/Bangkok
FOOTBALL_CACHE_TTL_SECONDS=86400
MANCHESTER_UNITED_BADGE_URL=<MANCHESTER_UNITED_BADGE_IMAGE_URL>

CODEX_USAGE_LOCAL_ENABLED=True
CODEX_CLI_PATH=codex
CODEX_USAGE_CACHE_PATH=data/codex_usage.json
CODEX_USAGE_TIMEZONE=Asia/Bangkok
CODEX_USAGE_REFRESH_SECONDS=60
CODEX_USAGE_STALE_SECONDS=900
CODEX_USAGE_SYNC_TOKEN=
```

Tạo API key miễn phí tại football-data.org rồi chỉ lưu key và URL thật trong `.env`. Gateway cập nhật lịch tối đa một lần mỗi ngày, đổi giờ UTC sang `Asia/Bangkok` và giữ bản dữ liệu thành công gần nhất để board vẫn hiển thị khi nhà cung cấp tạm gián đoạn.

### Ý nghĩa cấu hình

| Biến | Mặc định | Vai trò |
|---|---|---|
| `OPENROUTER_API_KEY` | rỗng | Bắt buộc để gọi LLM OpenRouter |
| `OPENROUTER_MODEL` | `openrouter/free` | Router/model chat và tool calling |
| `OPENROUTER_AUDIO_MODEL` | Nemotron free | STT dự phòng sau khi OpenAI xác nhận hết credit |
| `STT_PROVIDER` | `openai` | `openai`, `google-web` hoặc `openrouter`; có OpenAI key thì luôn ưu tiên OpenAI |
| `OPENAI_API_KEY` | rỗng | Bắt buộc cho ChatGPT và OpenAI STT |
| `OPENAI_STT_MODEL` | `gpt-4o-mini-transcribe` | Model nhận dạng âm thanh OpenAI |
| `LLM_STREAMING_ENABLED` | `true` | Stream token và phát TTS ngay khi hoàn thành một vế câu |
| `ASSISTANT_LOCATION` | `Việt Nam` | Địa điểm được chèn vào prompt động mỗi lượt |
| `ASSISTANT_TIMEZONE` | `Asia/Bangkok` | Múi giờ dùng cho câu hỏi ngày giờ và prompt |
| `WEB_SEARCH_PROVIDER` | `auto` | Chọn Tavily, Serper hoặc DuckDuckGo theo key hiện có |
| `WEB_SEARCH_BASE_URL` | theo `.env` | Endpoint của nhà cung cấp tìm kiếm, không hard-code trong source |
| `WEB_SEARCH_MAX_RESULTS` | `3` | Số kết quả ngắn đưa lại cho LLM tổng hợp |
| `STT_LANGUAGE` | `vi-VN` | Ngôn ngữ câu lệnh; wake còn chạy thêm `en-US` |
| `WAKE_STT_PROVIDER` | `configured` | Dùng OpenAI STT trước cho wake; Google và OpenRouter là các tuyến dự phòng |
| `WAKE_STT_TIMEOUT_SEC` | `8` | Timeout mỗi provider khi nhận dạng wake word |
| `WAKE_STT_OPENAI_FALLBACK` | `true` | Thử OpenAI STT khi Google chưa nhận ra wake word và bản ghi đủ mạnh |
| `WAKE_STT_FALLBACK_MIN_SPEECH_FRAMES` | `5` | Số frame giọng nói tối thiểu trước khi gọi fallback, tránh tốn API cho nhiễu ngắn |
| `WAKE_STT_FALLBACK_MIN_PEAK_RMS` | `400` | Peak RMS tối thiểu trước khi gọi fallback wake STT |
| `STT_OPENROUTER_FALLBACK` | `false` | Cho phép OpenRouter Audio với lỗi STT thường; khi OpenAI hết credit thì tự bật cho lượt fallback |
| `TTS_PROVIDER` | `google` | `google` hoặc nhánh Edge TTS |
| `TTS_VOICE` | `vi-VN-HoaiMyNeural` | Voice dùng bởi Edge TTS |
| `VOICE_SESSION_TIMEOUT_SEC` | `30` | Thời gian chờ lệnh sau khi wake |
| `VOICE_HEARTBEAT_INTERVAL_SEC` | `20` | Heartbeat ứng dụng giữ phiên WebSocket cloud và loại session đã chết |
| `VOICE_AUTH_TOKEN` | rỗng | Nếu có, firmware phải gửi Bearer token giống hệt |
| `CONVERSATION_DB_PATH` | `data/conversations.db` | File lưu hội thoại và trace tool |
| `FOOTBALL_DATA_API_KEY` | rỗng | API key lịch thi đấu; bắt buộc để tải dữ liệu mới |
| `FOOTBALL_TEAM_ID` | `66` | Team ID Manchester United trên football-data.org |
| `FOOTBALL_TIMEZONE` | `Asia/Bangkok` | Múi giờ ngày/giờ hiển thị trên ESP32 |
| `FOOTBALL_CACHE_TTL_SECONDS` | `86400` | Chu kỳ làm mới hằng ngày; lỗi mạng dùng last-known-good cache |
| `CODEX_USAGE_LOCAL_ENABLED` | `True` | Cho gateway trên PC đọc Codex CLI đã đăng nhập |
| `CODEX_USAGE_REFRESH_SECONDS` | `60` | TTL trước khi collector đọc usage live lần tiếp theo |
| `CODEX_USAGE_STALE_SECONDS` | `900` | Đánh dấu cache cũ trên thiết bị |
| `CODEX_USAGE_SYNC_TOKEN` | rỗng | Bearer token riêng để PC đẩy snapshot lên Oracle; rỗng sẽ khóa route sync |

## Khởi động

Khởi động MQTT development broker trước:

```powershell
.\.venv\Scripts\python.exe run_broker.py
```

Khởi động gateway:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Chế độ reload khi phát triển:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## HTTP API

| Method | Path | Mô tả |
|---|---|---|
| `GET` | `/health` | Provider, model, STT/TTS, API key status, số Voice session |
| `WS` | `/api/v1/voice/stream` | Dom Voice Protocol v3 |
| `GET` | `/api/v1/conversations?device_id=&limit=50` | Lịch sử hội thoại và tool trace |
| `GET` | `/api/wallpapers/slideshow` | Proxy danh sách slideshow từ Go :8081 |
| `GET` | `/uploads/wallpapers/{filename}` | Proxy file wallpaper từ Go :8081 |
| `GET` | `/api/football/manchester-united` | Trận kế tiếp của Manchester United; `force=true` bỏ TTL cache |
| `GET` | `/api/football/manchester-united/background.jpg` | Ảnh nền JPEG 320×240 gồm nền đỏ và logo MU |
| `GET` | `/api/codex/usage` | Hạn mức 5 giờ, tuần và full-reset; `force=true` yêu cầu làm mới |
| `POST` | `/api/codex/usage/sync` | Nhận snapshot từ PC, yêu cầu Bearer token |

Kiểm tra:

```powershell
Invoke-RestMethod http://<HOST_IP>:8000/health
```

Khi Assistant đang mở, `active_sessions` phải là `1`.

## Voice Protocol v3

### Handshake

Board gửi:

```json
{
  "type": "hello",
  "version": 3,
  "features": {"mcp": true, "aec": false, "vad": true},
  "audio_params": {
    "codec": "pcm",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60
  }
}
```

Gateway trả `hello` với `session_id`, provider và feature acknowledgement. Binary frame hai chiều là raw signed PCM little-endian, không phải Opus/MP3.

### Message JSON

| Type | Hướng | Nội dung |
|---|---|---|
| `listen` | hai chiều | `wake`, `start`, `processing`, `stop` |
| `stt` | gateway → board | Transcript câu lệnh |
| `llm` | gateway → board | Text/emotion; firmware chỉ hiện trạng thái |
| `tts` | gateway → board | `start`, `sentence_start`, `stop` |
| `abort` | board → gateway | Hủy pipeline/TTS khi nhấn nút X |
| `mcp` | hai chiều | JSON-RPC 2.0 `initialize`, `tools/list`, `tools/call` |

## Wake word và VAD

Gateway có hai chế độ capture:

```text
WAKE_WORD -> LISTENING -> PROCESSING -> SPEAKING -> WAKE_WORD
```

- Wake phrase: `Hey Dom` và các transcript gần âm do Google tạo trên board hiện tại.
- Wake STT chạy song song `en-US` và `vi-VN`; một số lỗi lặp lại chỉ được chấp nhận khi cả hai kết quả tạo đúng cặp chữ ký, giúp giảm false positive.
- Đoạn wake ngắn được normalize và thêm 250 ms silence hai đầu.
- `VAD_ENERGY_THRESHOLD=180`.
- `VAD_MIN_SPEECH_FRAMES=3`.
- `VAD_SILENCE_FRAMES=9`, xét trên cửa sổ 12 frame để chịu được xung nhiễu.
- Wake hard limit: 3 giây; câu lệnh hard limit: 20 giây.
- Mỗi frame: 60 ms, 1920 byte.
- Sau wake, nếu không có lệnh trong 30 giây, gateway trở lại `WAKE_WORD`.

Không chấp nhận transcript rỗng làm wake. Khi test lặp, sau một wake thành công phải đợi `Listening` và nói lệnh; cụm tiếp theo không còn là một wake event.

## Pipeline câu lệnh

```text
PCM -> VAD end -> listen.processing -> STT vi-VN
    -> lọc hallucination ASR
    -> fast path deterministic hoặc OpenAI chat/tool call
    -> web_search cho dữ liệu thời gian thực khi cần
    -> llm text/emotion
    -> stream theo dấu câu -> chuẩn hóa TTS -> sentence_start + binary PCM
    -> tts.stop -> WAKE_WORD
```

Lệnh volume, brightness, bật tắt màn hình, tắt loa, ngày giờ, trạng thái và app
có parser trực tiếp để phản hồi nhanh, không chờ LLM. Mọi lệnh phần cứng vẫn phải
nhận MCP ACK từ board rồi mới báo thành công. Yêu cầu khác đi qua OpenAI;
OpenRouter chỉ nhận lượt hội thoại sau khi OpenAI xác nhận hết credit/quota.

Prompt hệ thống được tạo lại ở mỗi lượt với thời gian, ngày, múi giờ và địa điểm
hiện tại. Prompt yêu cầu tiếng Việt mặc định, câu trả lời một đến hai câu dưới
40 từ và dùng lịch sử hội thoại cho câu nói tiếp nối.

Các câu hỏi chứa dấu hiệu dữ liệu thay đổi như hôm nay, mới nhất, thời tiết, giá,
kết quả hoặc lịch thi đấu sẽ ép tool `web_search` ở vòng đầu. Gateway lấy tối đa
ba kết quả qua Tavily, Serper hoặc DuckDuckGo và gửi lại cho LLM tổng hợp. Tool
này chạy trên gateway, không gửi xuống ESP32.

Lệnh mở Codex Credit: “mở ứng dụng Codex Credit”, “mở Codex”, “Codex usage”
hoặc “code credit checking”. Parser hỗ trợ câu không dấu và bản nhận dạng nhầm
đã ghi nhận “Tracking topic Credit”; không tự ánh xạ từ chung như “Connect” hay
“credit” thành tên app. Tên app chưa rõ sẽ được hỏi lại.

Hai câu lệnh tắt (có dấu hoặc không dấu):

- “kiểm tra lịch thi đấu bóng đá” → mở app Manchester United (`man-utd`).
- “kiểm tra Codex Credit” → mở app Codex Credit (`codex-credit`).

Có thể thêm “Hey Dom” ở đầu câu hoặc “giúp mình nhé” ở cuối câu. Câu hỏi
về lịch của đội khác không tự ánh xạ sang Manchester United.

Gateway gửi `app.launch({"app":"codex-credit"})` và chờ MCP result từ ESP32
trước khi xác nhận bằng giọng nói. Nếu timeout hoặc firmware báo lỗi, gateway
không báo mở thành công. Lời xác nhận mở app do LLM tự tạo mà không gọi công cụ
sẽ bị chặn. Tool trace được lưu trong lịch sử để đối chiếu với log `Launched`
của firmware (MCP result xác nhận yêu cầu được nhận, không phải ảnh chụp màn hình).

Mọi câu LLM đi qua bộ chuẩn hóa văn bản thuần trước khi lưu SQLite, hiển thị LCD
và phát TTS. Bộ lọc bỏ heading, Markdown, emoji, dấu ngoặc, link, code và HTML;
đồng thời đổi giờ, nhiệt độ, phần trăm, tốc độ, công suất, phiên bản và từ mượn
phổ biến sang dạng tiếng Việt dễ đọc. Lịch sử cũ được làm sạch idempotent khi
gateway khởi động.

Transcript ASR đi qua blacklist cho các câu rác phổ biến, filler quá ngắn và từ
lặp do tiếng ồn. Wake word ngắn như `Hey` và `Dom` được giữ lại. Khi LLM stream,
gateway xẻ tại dấu phẩy, chấm, hỏi hoặc cảm thán và đưa vế hoàn chỉnh vào hàng
đợi TTS; LLM tiếp tục sinh phần sau trong lúc board phát phần trước.

OpenAI LLM và OpenAI STT có circuit riêng. Lỗi STT thông thường chỉ chuyển sang
Google STT và không làm ChatGPT bị bỏ qua. Nếu OpenAI xác nhận hết credit, STT
thử Google rồi OpenRouter Audio; LLM chuyển sang OpenRouter. Rate limit tạm thời,
timeout, lỗi mạng, xác thực hoặc lỗi server không bị coi nhầm là hết credit.
Provider/model thực tế sau fallback được cập nhật vào conversation turn thay cho
provider ưu tiên.

## MCP tools

Assistant tự kết nối sau khi ESP32 có Wi-Fi, không cần mở màn hình Assistant
lần đầu. Khi nghe “Hey Dom”, “Hey” hoặc “Dom”, gateway gửi `listen.start`
với `source=wake_word`; nếu câu gọi kèm lệnh thì dùng `listen.processing`
với cùng source. Firmware đưa Assistant lên trước qua EventBus và hàng đợi UI.
Các trạng thái TTS thông thường không giành lại màn hình từ app vừa được mở.

Wake STT mặc định dùng cùng chuỗi provider với câu lệnh: OpenAI trước, Google
Web Speech dự phòng khi OpenAI lỗi, timeout hoặc trả transcript rỗng. Circuit
breaker wake và câu lệnh tách biệt để timeout của wake không làm mất OpenAI ở
câu lệnh ngay sau đó. Log `stt_ms` đo phần kiểm tra wake, không bao gồm thời gian
chờ kết thúc câu VAD.

Không còn xem “Hello”, “huy động”, “he does” là wake word vì dễ kích hoạt nhầm
khi nghe nền. “Hey” và “Dom” vẫn có nguy cơ kích hoạt nhầm trong hội thoại.
Đây là cải tiến luồng nhận dạng, **chưa phải model đã huấn luyện theo giọng riêng**.
Xem [thu mẫu và đánh giá wake word](../docs/WAKE_WORD.md).

| Tool | Arguments | Kết quả |
|---|---|---|
| `device.get_status` | `{}` | State assistant và audio pipeline |
| `speaker.set_volume` | `{"volume": 0..100}` | Đặt volume tuyệt đối |
| `speaker.adjust_volume` | `{"delta": -100..100}` | Tăng/giảm volume |
| `display.adjust_brightness` | `{"delta": -100..100}` | Tăng/giảm backlight |
| `app.launch` | `{"app":"wallpaper"}`, `clock`, `man-utd` hoặc `codex-credit` | Yêu cầu AppManager mở app |

## Codex Usage app

Khi gateway chạy trên máy đã đăng nhập Codex, giữ `CODEX_USAGE_LOCAL_ENABLED=True`.
Gateway gọi local Codex app-server, chuẩn hóa dữ liệu và chỉ gửi phần trăm/thời điểm reset cho ESP32; credential không được gửi xuống board.

Khi gateway chạy trên Oracle Free Tier, đặt `CODEX_USAGE_LOCAL_ENABLED=False`, tạo một `CODEX_USAGE_SYNC_TOKEN` mạnh ở cả PC và Oracle, rồi chạy định kỳ trên PC:

```powershell
$env:CODEX_USAGE_SYNC_URL="https://voice.<YOUR_DOMAIN>"
$env:CODEX_USAGE_SYNC_TOKEN="<PRIVATE_SYNC_TOKEN>"
.\.venv\Scripts\python.exe scripts\sync_codex_usage.py
```

Có thể tạo Windows Task Scheduler chạy lệnh trên mỗi 5 phút. Không sao chép thư mục đăng nhập Codex hoặc token tài khoản lên Oracle.

Gateway chỉ xác nhận thành công sau khi firmware trả MCP result.

## Conversation memory

`ConversationStore` tạo SQLite schema tự động. Mỗi turn lưu:

- `device_id`, câu người dùng, câu AI;
- provider/model;
- trạng thái và timestamp;
- tool name, arguments, result, duration và success/error.

Context gần nhất được đưa vào lần gọi LLM tiếp theo. Dashboard đọc cùng dữ liệu qua `/api/v1/conversations`.

## Test và debug

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py config.py services\openrouter_voice_service.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Log cần quan sát:

- `Cloud voice connected`: handshake thành công.
- `Wake audio`: số frame, speech frame, peak/average RMS.
- `Wake phrase accepted/rejected`: transcript song ngữ.
- `Command speech ended`: VAD đã tự chốt câu.
- `STT device=...`: transcript lệnh.
- `STT provider selected ... provider=openai`: OpenAI đã nhận dạng thành công.
- HTTP `200 OK` từ OpenAI; OpenRouter chỉ xuất hiện khi OpenAI hết credit/quota.

Nếu board online nhưng `active_sessions=0`, mở app Assistant hoặc `POST http://<DEVICE_IP>/api/launch` với `{"app":"assistant"}`.

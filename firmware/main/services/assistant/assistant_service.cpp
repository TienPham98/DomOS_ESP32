#include "assistant_service.h"

#include <algorithm>
#include <cstring>
#include <cstdio>

#include "app/launcher/app_manager.h"
#include "board/es3c28p/board_es3c28p.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "cJSON.h"

static const char *TAG = "assistant";

// ─── Start / Stop ─────────────────────────────────────────────────────────────

bool AssistantService::Start(ES3C28PBoard *board, EventBus *events, const AssistantConfig &cfg)
{
    board_  = board;
    events_ = events;
    cfg_    = cfg;

    ws_.OnText([this](const char *data, size_t len) {
        char *buf = new char[len + 1];
        memcpy(buf, data, len);
        buf[len] = '\0';
        this->OnWsText(buf, len);
        delete[] buf;
    });

    ws_.OnBinary([this](const uint8_t *data, size_t len) {
        this->OnWsBinary(data, len);
    });

    ws_.OnConnect([this](bool connected) {
        this->OnWsConnect(connected);
    });

    AudioPipelineConfig pipe_cfg;
    pipe_cfg.mic_chunk_samples = 960;  // 60ms @ 16kHz
    pipe_cfg.output_queue_depth = 4;
    pipe_cfg.on_mic_data = [this](const int16_t *pcm, size_t samples) {
        // Only stream audio once handshake is complete
        // With AEC disabled, uploading the microphone while the speaker is
        // active would make Dom interrupt itself. Capture only while listening.
        const AssistantState state = this->GetState();
        if (this->handshake_done_.load() &&
            (state == AssistantState::Armed || state == AssistantState::Listening)) {
            this->ws_.SendBinary(reinterpret_cast<const uint8_t *>(pcm), samples * sizeof(int16_t));
        }
    };

    if (!pipeline_.Start(board_, pipe_cfg)) {
        ESP_LOGE(TAG, "Audio pipeline start failed");
        return false;
    }

    ESP_LOGI(TAG, "AssistantService ready (uri=%s)", cfg_.ws_uri ? cfg_.ws_uri : "none");
    return true;
}

void AssistantService::Stop()
{
    CloseAudioChannel();
    pipeline_.Stop();
    ESP_LOGI(TAG, "AssistantService stopped");
}

// ─── Getters ──────────────────────────────────────────────────────────────────

std::string AssistantService::GetEmotion() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return emotion_;
}

std::string AssistantService::GetUserText() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return user_text_;
}

std::string AssistantService::GetAssistantText() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return assistant_text_;
}

void AssistantService::NotifyUi()
{
    if (ui_cb_) {
        ui_cb_();
    }
}

// ─── Open / Close Audio Channel ──────────────────────────────────────────────

void AssistantService::OpenAudioChannel()
{
    std::lock_guard<std::mutex> channel_lock(channel_mutex_);
    if (ws_.IsStarted() || GetState() != AssistantState::Idle) {
        ESP_LOGW(TAG, "OpenAudioChannel: already active (state=%d)", (int)GetState());
        return;
    }

    if (!cfg_.ws_uri || cfg_.ws_uri[0] == '\0') {
        ESP_LOGE(TAG, "AI Gateway WebSocket URI is not configured");
        return;
    }

    SetState(AssistantState::Connecting);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "thinking";
        user_text_ = "";
        assistant_text_ = "Connecting to Dom AI...";
    }
    NotifyUi();

    // Keep the active-low amplifier muted until a TTS start event arrives.
    board_->SetPAEnabled(false);

    static char mac_str[18];
    if (!cfg_.device_id || cfg_.device_id[0] == '\0') {
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_WIFI_STA);
        snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        cfg_.device_id = mac_str;
    }

    WsClientConfig ws_cfg;
    ws_cfg.uri              = cfg_.ws_uri;
    ws_cfg.auth_token       = cfg_.auth_token;
    ws_cfg.device_id        = cfg_.device_id;
    ws_cfg.client_id        = cfg_.client_id ? cfg_.client_id : "domos-esp32-s3";
    ws_cfg.protocol_version = 3;
    ws_cfg.reconnect_ms     = 3000;

    if (!ws_.Connect(ws_cfg)) {
        ESP_LOGE(TAG, "WS connect failed");
        SetState(AssistantState::Idle);
        board_->SetPAEnabled(false);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            assistant_text_ = "Server connection error!";
            emotion_ = "sad";
        }
        NotifyUi();
    }
}

void AssistantService::CloseAudioChannel()
{
    std::lock_guard<std::mutex> channel_lock(channel_mutex_);
    if (GetState() == AssistantState::Listening) {
        SendListenStop();
    }
    ws_.Disconnect();
    pipeline_.FlushOutput();
    board_->SetPAEnabled(false);
    SetState(AssistantState::Idle);
    handshake_done_.store(false);
    tts_active_.store(false);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "idle";
        user_text_ = "";
        assistant_text_ = "Dialogue session stopped";
    }
    NotifyUi();
    ESP_LOGI(TAG, "Audio channel closed");
}

void AssistantService::SubmitSpeech()
{
    if (GetState() != AssistantState::Listening || !handshake_done_.load()) {
        ESP_LOGW(TAG, "SubmitSpeech ignored in state=%d", (int)GetState());
        return;
    }
    SendListenStop();
}

void AssistantService::ActivateListening()
{
    if (GetState() != AssistantState::Armed || !handshake_done_.load()) {
        ESP_LOGW(TAG, "ActivateListening ignored in state=%d", (int)GetState());
        return;
    }
    SendListenStart();
}

void AssistantService::CancelResponse()
{
    const AssistantState state = GetState();
    if ((state != AssistantState::Processing && state != AssistantState::Speaking) ||
        !handshake_done_.load()) {
        ESP_LOGW(TAG, "CancelResponse ignored in state=%d", (int)state);
        return;
    }
    ESP_LOGI(TAG, "Cancelling active AI response");
    SendAbort("screen_cancel_button");
}

// ─── WebSocket Event Callbacks ────────────────────────────────────────────────

void AssistantService::OnWsConnect(bool connected)
{
    if (connected) {
        ESP_LOGI(TAG, "WS connected — sending hello");
        handshake_done_.store(false);
        SendHello();
        if (events_) events_->Publish(EventType::AssistantConnected, TAG, "");
    } else {
        ESP_LOGW(TAG, "WS disconnected");
        SetState(AssistantState::Idle);
        handshake_done_.store(false);
        tts_active_.store(false);
        pipeline_.FlushOutput();
        if (board_) board_->SetPAEnabled(false);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            emotion_ = "sad";
            assistant_text_ = "Lost connection to server";
        }
        NotifyUi();
        if (events_) events_->Publish(EventType::AssistantDisconnected, TAG, "");
    }
}

void AssistantService::OnWsText(const char *data, size_t /*len*/)
{
    cJSON *root = cJSON_Parse(data);
    if (!root) {
        ESP_LOGW(TAG, "Invalid JSON: %.40s", data);
        return;
    }

    cJSON *type_j = cJSON_GetObjectItemCaseSensitive(root, "type");
    const char *type = type_j && cJSON_IsString(type_j) ? type_j->valuestring : "";

    if      (strcmp(type, "hello")  == 0) HandleHello(data);
    else if (strcmp(type, "listen") == 0) HandleListen(data);
    else if (strcmp(type, "stt")    == 0) HandleStt(data);
    else if (strcmp(type, "llm")    == 0) HandleLlm(data);
    else if (strcmp(type, "tts")    == 0) HandleTts(data);
    else if (strcmp(type, "mcp")    == 0) HandleMcp(data);
    else if (strcmp(type, "system") == 0) HandleSystem(data);
    else if (strcmp(type, "alert")  == 0) HandleAlert(data);

    cJSON_Delete(root);
}

void AssistantService::OnWsBinary(const uint8_t *data, size_t len)
{
    if (GetState() != AssistantState::Speaking) {
        return;
    }
    const size_t samples = len / sizeof(int16_t);
    pipeline_.EnqueueAudio(reinterpret_cast<const int16_t *>(data), samples);
}

// ─── Protocol Senders ────────────────────────────────────────────────────────

void AssistantService::SendHello()
{
    char json[256];
    snprintf(json, sizeof(json),
             "{\"type\":\"hello\",\"version\":3,"
             "\"features\":{\"mcp\":true,\"aec\":false,\"vad\":true},"
             "\"audio_params\":{\"codec\":\"pcm\",\"sample_rate\":16000,"
             "\"channels\":1,\"frame_duration\":60}}");
    ws_.SendText(json);
}

void AssistantService::SendListenStart()
{
    // Let gateway VAD detect speech end automatically. A screen tap can still
    // call SendListenStop() as a manual fallback in a noisy environment.
    const char *json = "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"auto\"}";
    ws_.SendText(json);
    SetState(AssistantState::Listening);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "listening";
    }
    NotifyUi();
    if (events_) events_->Publish(EventType::AssistantListening, TAG, "start");
    ESP_LOGI(TAG, "Automatic listening started");
}

void AssistantService::SendWakeStart()
{
    const char *json = "{\"type\":\"listen\",\"state\":\"start\",\"mode\":\"wake\"}";
    ws_.SendText(json);
    SetState(AssistantState::Armed);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "idle";
        user_text_ = "";
        assistant_text_ = "Say Hey Dom or tap the screen";
    }
    NotifyUi();
    ESP_LOGI(TAG, "Wake-word mode armed");
}

void AssistantService::SendListenStop()
{
    const char *json = "{\"type\":\"listen\",\"state\":\"stop\"}";
    ws_.SendText(json);
    SetState(AssistantState::Processing);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "processing";
        assistant_text_ = "OpenRouter is processing...";
    }
    NotifyUi();
    if (events_) events_->Publish(EventType::AssistantProcessing, TAG, "stop");
}

void AssistantService::SendAbort(const char *reason)
{
    char json[128];
    snprintf(json, sizeof(json), "{\"type\":\"abort\",\"reason\":\"%s\"}", reason);
    ws_.SendText(json);
    tts_active_.store(false);
    pipeline_.FlushOutput();
    board_->SetPAEnabled(false);
    SetState(AssistantState::Armed);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "idle";
        assistant_text_ = "Response cancelled";
    }
    NotifyUi();
}

void AssistantService::SendMcpResult(int req_id, const char *text, bool is_error)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *payload = cJSON_AddObjectToObject(root, "payload");
    cJSON *result = cJSON_AddObjectToObject(payload, "result");
    cJSON *content = cJSON_AddArrayToObject(result, "content");
    cJSON *item = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "mcp");
    cJSON_AddStringToObject(payload, "jsonrpc", "2.0");
    cJSON_AddNumberToObject(payload, "id", req_id);
    cJSON_AddStringToObject(item, "type", "text");
    cJSON_AddStringToObject(item, "text", text ? text : "ok");
    cJSON_AddItemToArray(content, item);
    cJSON_AddBoolToObject(result, "isError", is_error);
    char *json = cJSON_PrintUnformatted(root);
    if (json != nullptr) {
        ws_.SendText(json);
        cJSON_free(json);
    }
    cJSON_Delete(root);
}

// ─── Protocol Handlers ────────────────────────────────────────────────────────

void AssistantService::HandleHello(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;

    cJSON *sid = cJSON_GetObjectItemCaseSensitive(root, "session_id");
    if (sid && cJSON_IsString(sid)) {
        ESP_LOGI(TAG, "Dom AI session established: %s", sid->valuestring);
    }
    cJSON_Delete(root);

    handshake_done_.store(true);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        assistant_text_ = "Say Hey Dom or tap the screen";
    }
    SendWakeStart();
}

void AssistantService::HandleListen(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    cJSON *state_j = cJSON_GetObjectItemCaseSensitive(root, "state");
    const char *listen_state = cJSON_IsString(state_j) ? state_j->valuestring : "";
    cJSON *source_j = cJSON_GetObjectItemCaseSensitive(root, "source");
    const bool from_wake_word = cJSON_IsString(source_j) &&
        strcmp(source_j->valuestring, "wake_word") == 0;
    // Older gateways omit source. Armed -> active is their wake notification;
    // manual taps already moved the device to Listening before their reply.
    const bool legacy_wake = source_j == nullptr && GetState() == AssistantState::Armed;
    const bool wake_activation = (from_wake_word || legacy_wake) &&
        (strcmp(listen_state, "start") == 0 || strcmp(listen_state, "processing") == 0);
    if (wake_activation && events_) {
        // Wake activation and subsequent app.launch requests share one FIFO.
        // Never foreground Assistant on ordinary TTS/state updates.
        if (!events_->Publish(EventType::AppLaunchRequested, TAG, "assistant")) {
            ESP_LOGW(TAG, "Wake activation queue is full");
        }
    }
    if (strcmp(listen_state, "wake") == 0) {
        SetState(AssistantState::Armed);
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "idle";
        user_text_ = "";
        assistant_text_ = "Say Hey Dom or tap the screen";
    } else if (strcmp(listen_state, "start") == 0) {
        SetState(AssistantState::Listening);
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "listening";
        user_text_ = "";
        assistant_text_ = "I'm listening...";
    } else if (strcmp(listen_state, "processing") == 0) {
        SetState(AssistantState::Processing);
        std::lock_guard<std::mutex> lock(mutex_);
        emotion_ = "thinking";
        assistant_text_ = "Thinking...";
    }
    cJSON_Delete(root);
    NotifyUi();
}

void AssistantService::HandleStt(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    cJSON *text_j = cJSON_GetObjectItemCaseSensitive(root, "text");
    if (text_j && cJSON_IsString(text_j)) {
        ESP_LOGI(TAG, "STT: %s", text_j->valuestring);
        SetState(AssistantState::Processing);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            user_text_ = text_j->valuestring;
            emotion_ = "thinking";
        }
        NotifyUi();
        if (events_) events_->Publish(EventType::AssistantProcessing, TAG, text_j->valuestring);
    }
    cJSON_Delete(root);
}

void AssistantService::HandleLlm(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    cJSON *emotion_j = cJSON_GetObjectItemCaseSensitive(root, "emotion");
    cJSON *text_j    = cJSON_GetObjectItemCaseSensitive(root, "text");

    std::string emotion_str = "idle";
    std::string text_str = "";

    if (emotion_j && cJSON_IsString(emotion_j)) {
        emotion_str = emotion_j->valuestring;
    }
    if (text_j && cJSON_IsString(text_j)) {
        text_str = text_j->valuestring;
    }

    if (emotion_str == "thinking" || emotion_str == "processing") {
        SetState(AssistantState::Processing);
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!emotion_str.empty()) emotion_ = emotion_str;
        if (!text_str.empty()) assistant_text_ = text_str;
    }
    NotifyUi();
    cJSON_Delete(root);
}

void AssistantService::HandleTts(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;

    cJSON *state_j = cJSON_GetObjectItemCaseSensitive(root, "state");
    const char *tts_state = state_j && cJSON_IsString(state_j) ? state_j->valuestring : "";

    if (strcmp(tts_state, "start") == 0) {
        tts_active_.store(true);
        SetState(AssistantState::Speaking);
        board_->SetPAEnabled(true);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            emotion_ = "speaking";
        }
        NotifyUi();
        if (events_) events_->Publish(EventType::AssistantSpeaking, TAG, "start");

    } else if (strcmp(tts_state, "sentence_start") == 0) {
        cJSON *text_j = cJSON_GetObjectItemCaseSensitive(root, "text");
        if (text_j && cJSON_IsString(text_j)) {
            std::lock_guard<std::mutex> lock(mutex_);
            assistant_text_ = text_j->valuestring;
            emotion_ = "speaking";
        }
        NotifyUi();

    } else if (strcmp(tts_state, "stop") == 0) {
        tts_active_.store(false);
        pipeline_.FlushOutput();
        board_->SetPAEnabled(false);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            emotion_ = "idle";
        }
        NotifyUi();
        if (events_) events_->Publish(EventType::AssistantSpeaking, TAG, "stop");
        // The gateway sends listen.wake after the response is fully complete.
    }

    cJSON_Delete(root);
}

void AssistantService::HandleMcp(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;

    cJSON *payload = cJSON_GetObjectItemCaseSensitive(root, "payload");
    if (payload) {
        cJSON *method_j = cJSON_GetObjectItemCaseSensitive(payload, "method");
        cJSON *id_j     = cJSON_GetObjectItemCaseSensitive(payload, "id");
        int req_id = (id_j && cJSON_IsNumber(id_j)) ? id_j->valueint : 0;

        if (method_j && cJSON_IsString(method_j)) {
            const char *method = method_j->valuestring;
            ESP_LOGI(TAG, "MCP: %s (id=%d)", method, req_id);

            if (strcmp(method, "initialize") == 0) {
                cJSON *response = cJSON_CreateObject();
                cJSON *out_payload = cJSON_AddObjectToObject(response, "payload");
                cJSON *result = cJSON_AddObjectToObject(out_payload, "result");
                cJSON_AddStringToObject(response, "type", "mcp");
                cJSON_AddStringToObject(out_payload, "jsonrpc", "2.0");
                cJSON_AddNumberToObject(out_payload, "id", req_id);
                cJSON_AddStringToObject(result, "protocolVersion", "2024-11-05");
                cJSON_AddObjectToObject(result, "capabilities");
                cJSON *server_info = cJSON_AddObjectToObject(result, "serverInfo");
                cJSON_AddStringToObject(server_info, "name", "DomOS-ES3C28P");
                cJSON_AddStringToObject(server_info, "version", "0.3.5");
                char *out = cJSON_PrintUnformatted(response);
                if (out != nullptr) {
                    ws_.SendText(out);
                    cJSON_free(out);
                }
                cJSON_Delete(response);
            } else if (strcmp(method, "tools/list") == 0) {
                cJSON *response = cJSON_CreateObject();
                cJSON *out_payload = cJSON_AddObjectToObject(response, "payload");
                cJSON *result = cJSON_AddObjectToObject(out_payload, "result");
                cJSON *tools = cJSON_AddArrayToObject(result, "tools");
                cJSON_AddStringToObject(response, "type", "mcp");
                cJSON_AddStringToObject(out_payload, "jsonrpc", "2.0");
                cJSON_AddNumberToObject(out_payload, "id", req_id);

                cJSON *status_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(status_tool, "name", "device.get_status");
                cJSON_AddStringToObject(status_tool, "description", "Get assistant and audio status");
                cJSON *status_schema = cJSON_AddObjectToObject(status_tool, "inputSchema");
                cJSON_AddStringToObject(status_schema, "type", "object");
                cJSON_AddObjectToObject(status_schema, "properties");
                cJSON_AddItemToArray(tools, status_tool);

                cJSON *volume_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(volume_tool, "name", "speaker.set_volume");
                cJSON_AddStringToObject(volume_tool, "description", "Set speaker volume from 0 to 100");
                cJSON *volume_schema = cJSON_AddObjectToObject(volume_tool, "inputSchema");
                cJSON_AddStringToObject(volume_schema, "type", "object");
                cJSON *properties = cJSON_AddObjectToObject(volume_schema, "properties");
                cJSON *volume = cJSON_AddObjectToObject(properties, "volume");
                cJSON_AddStringToObject(volume, "type", "integer");
                cJSON_AddNumberToObject(volume, "minimum", 0);
                cJSON_AddNumberToObject(volume, "maximum", 100);
                cJSON *required = cJSON_AddArrayToObject(volume_schema, "required");
                cJSON_AddItemToArray(required, cJSON_CreateString("volume"));
                cJSON_AddItemToArray(tools, volume_tool);

                cJSON *adjust_volume_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(adjust_volume_tool, "name", "speaker.adjust_volume");
                cJSON_AddStringToObject(adjust_volume_tool, "description", "Increase or decrease speaker volume");
                cJSON *adjust_volume_schema = cJSON_AddObjectToObject(adjust_volume_tool, "inputSchema");
                cJSON_AddStringToObject(adjust_volume_schema, "type", "object");
                cJSON *adjust_volume_properties = cJSON_AddObjectToObject(adjust_volume_schema, "properties");
                cJSON *volume_delta = cJSON_AddObjectToObject(adjust_volume_properties, "delta");
                cJSON_AddStringToObject(volume_delta, "type", "integer");
                cJSON_AddNumberToObject(volume_delta, "minimum", -100);
                cJSON_AddNumberToObject(volume_delta, "maximum", 100);
                cJSON *adjust_volume_required = cJSON_AddArrayToObject(adjust_volume_schema, "required");
                cJSON_AddItemToArray(adjust_volume_required, cJSON_CreateString("delta"));
                cJSON_AddItemToArray(tools, adjust_volume_tool);

                cJSON *brightness_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(brightness_tool, "name", "display.adjust_brightness");
                cJSON_AddStringToObject(brightness_tool, "description", "Increase or decrease display brightness");
                cJSON *brightness_schema = cJSON_AddObjectToObject(brightness_tool, "inputSchema");
                cJSON_AddStringToObject(brightness_schema, "type", "object");
                cJSON *brightness_properties = cJSON_AddObjectToObject(brightness_schema, "properties");
                cJSON *brightness_delta = cJSON_AddObjectToObject(brightness_properties, "delta");
                cJSON_AddStringToObject(brightness_delta, "type", "integer");
                cJSON_AddNumberToObject(brightness_delta, "minimum", -100);
                cJSON_AddNumberToObject(brightness_delta, "maximum", 100);
                cJSON *brightness_required = cJSON_AddArrayToObject(brightness_schema, "required");
                cJSON_AddItemToArray(brightness_required, cJSON_CreateString("delta"));
                cJSON_AddItemToArray(tools, brightness_tool);

                cJSON *set_brightness_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(set_brightness_tool, "name", "display.set_brightness");
                cJSON_AddStringToObject(set_brightness_tool, "description", "Set display brightness from 0 to 100");
                cJSON *set_brightness_schema = cJSON_AddObjectToObject(set_brightness_tool, "inputSchema");
                cJSON_AddStringToObject(set_brightness_schema, "type", "object");
                cJSON *set_brightness_properties = cJSON_AddObjectToObject(set_brightness_schema, "properties");
                cJSON *brightness_value = cJSON_AddObjectToObject(set_brightness_properties, "brightness");
                cJSON_AddStringToObject(brightness_value, "type", "integer");
                cJSON_AddNumberToObject(brightness_value, "minimum", 0);
                cJSON_AddNumberToObject(brightness_value, "maximum", 100);
                cJSON *set_brightness_required = cJSON_AddArrayToObject(set_brightness_schema, "required");
                cJSON_AddItemToArray(set_brightness_required, cJSON_CreateString("brightness"));
                cJSON_AddItemToArray(tools, set_brightness_tool);

                cJSON *clock_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(clock_tool, "name", "clock.configure");
                cJSON_AddStringToObject(clock_tool, "description", "Configure and open the clock application");
                cJSON *clock_schema = cJSON_AddObjectToObject(clock_tool, "inputSchema");
                cJSON_AddStringToObject(clock_schema, "type", "object");
                cJSON *clock_properties = cJSON_AddObjectToObject(clock_schema, "properties");
                cJSON *clock_style = cJSON_AddObjectToObject(clock_properties, "style");
                cJSON_AddStringToObject(clock_style, "type", "string");
                cJSON *clock_color = cJSON_AddObjectToObject(clock_properties, "color");
                cJSON_AddStringToObject(clock_color, "type", "string");
                cJSON *clock_mode = cJSON_AddObjectToObject(clock_properties, "mode");
                cJSON_AddStringToObject(clock_mode, "type", "string");
                cJSON *clock_required = cJSON_AddArrayToObject(clock_schema, "required");
                cJSON_AddItemToArray(clock_required, cJSON_CreateString("style"));
                cJSON_AddItemToArray(clock_required, cJSON_CreateString("color"));
                cJSON_AddItemToArray(clock_required, cJSON_CreateString("mode"));
                cJSON_AddItemToArray(tools, clock_tool);

                cJSON *wallpaper_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(wallpaper_tool, "name", "wallpaper.set");
                cJSON_AddStringToObject(wallpaper_tool, "description", "Download and open a cloud wallpaper");
                cJSON *wallpaper_schema = cJSON_AddObjectToObject(wallpaper_tool, "inputSchema");
                cJSON_AddStringToObject(wallpaper_schema, "type", "object");
                cJSON *wallpaper_properties = cJSON_AddObjectToObject(wallpaper_schema, "properties");
                cJSON *wallpaper_url = cJSON_AddObjectToObject(wallpaper_properties, "url");
                cJSON_AddStringToObject(wallpaper_url, "type", "string");
                cJSON *wallpaper_name = cJSON_AddObjectToObject(wallpaper_properties, "name");
                cJSON_AddStringToObject(wallpaper_name, "type", "string");
                cJSON *wallpaper_required = cJSON_AddArrayToObject(wallpaper_schema, "required");
                cJSON_AddItemToArray(wallpaper_required, cJSON_CreateString("url"));
                cJSON_AddItemToArray(tools, wallpaper_tool);

                cJSON *wallpaper_sync_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(wallpaper_sync_tool, "name", "wallpaper.sync");
                cJSON_AddStringToObject(wallpaper_sync_tool, "description", "Sync wallpapers from the cloud gateway");
                cJSON *wallpaper_sync_schema = cJSON_AddObjectToObject(wallpaper_sync_tool, "inputSchema");
                cJSON_AddStringToObject(wallpaper_sync_schema, "type", "object");
                cJSON_AddObjectToObject(wallpaper_sync_schema, "properties");
                cJSON_AddItemToArray(tools, wallpaper_sync_tool);

                cJSON *launch_tool = cJSON_CreateObject();
                cJSON_AddStringToObject(launch_tool, "name", "app.launch");
                cJSON_AddStringToObject(launch_tool, "description", "Open a DomOS application");
                cJSON *launch_schema = cJSON_AddObjectToObject(launch_tool, "inputSchema");
                cJSON_AddStringToObject(launch_schema, "type", "object");
                cJSON *launch_properties = cJSON_AddObjectToObject(launch_schema, "properties");
                cJSON *app_name = cJSON_AddObjectToObject(launch_properties, "app");
                cJSON_AddStringToObject(app_name, "type", "string");
                cJSON *app_enum = cJSON_AddArrayToObject(app_name, "enum");
                cJSON_AddItemToArray(app_enum, cJSON_CreateString("wallpaper"));
                cJSON_AddItemToArray(app_enum, cJSON_CreateString("clock"));
                cJSON_AddItemToArray(app_enum, cJSON_CreateString("tracking-status"));
                cJSON_AddItemToArray(app_enum, cJSON_CreateString("man-utd"));
                cJSON_AddItemToArray(app_enum, cJSON_CreateString("codex-credit"));
                cJSON *launch_required = cJSON_AddArrayToObject(launch_schema, "required");
                cJSON_AddItemToArray(launch_required, cJSON_CreateString("app"));
                cJSON_AddItemToArray(tools, launch_tool);

                char *out = cJSON_PrintUnformatted(response);
                if (out != nullptr) {
                    ws_.SendText(out);
                    cJSON_free(out);
                }
                cJSON_Delete(response);
            } else if (strcmp(method, "tools/call") == 0) {
                cJSON *params = cJSON_GetObjectItemCaseSensitive(payload, "params");
                cJSON *name_j = params ? cJSON_GetObjectItemCaseSensitive(params, "name") : nullptr;
                cJSON *args = params ? cJSON_GetObjectItemCaseSensitive(params, "arguments") : nullptr;
                const char *name = cJSON_IsString(name_j) ? name_j->valuestring : "";

                if (strcmp(name, "device.get_status") == 0) {
                    const char *state = GetState() == AssistantState::Speaking ? "speaking" :
                                        GetState() == AssistantState::Listening ? "listening" :
                                        GetState() == AssistantState::Armed ? "armed" :
                                        GetState() == AssistantState::Processing ? "processing" : "idle";
                    const esp_app_desc_t *app = esp_app_get_description();
                    char status[256];
                    snprintf(status, sizeof(status),
                             "{\"assistant_state\":\"%s\",\"audio_running\":%s,"
                             "\"firmware\":\"%s\",\"free_heap\":%u,\"storage_used\":40960,"
                             "\"storage_total\":7340032,\"volume\":%d,\"brightness\":%u}",
                             state, pipeline_.IsRunning() ? "true" : "false", app->version,
                             heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                             board_->GetCodec()->GetVolume(), board_->GetBrightness());
                    SendMcpResult(req_id, status);
                } else if (strcmp(name, "speaker.set_volume") == 0) {
                    cJSON *volume_j = args ? cJSON_GetObjectItemCaseSensitive(args, "volume") : nullptr;
                    if (!cJSON_IsNumber(volume_j) || volume_j->valueint < 0 || volume_j->valueint > 100) {
                        SendMcpResult(req_id, "volume must be an integer from 0 to 100", true);
                    } else if (board_->GetCodec()->SetVolume(volume_j->valueint) != ESP_OK) {
                        SendMcpResult(req_id, "codec rejected the volume change", true);
                    } else {
                        char result_text[64];
                        snprintf(result_text, sizeof(result_text), "speaker volume set to %d", volume_j->valueint);
                        SendMcpResult(req_id, result_text);
                    }
                } else if (strcmp(name, "speaker.adjust_volume") == 0) {
                    cJSON *delta_j = args ? cJSON_GetObjectItemCaseSensitive(args, "delta") : nullptr;
                    if (!cJSON_IsNumber(delta_j) || delta_j->valueint < -100 || delta_j->valueint > 100) {
                        SendMcpResult(req_id, "delta must be an integer from -100 to 100", true);
                    } else {
                        const int volume = std::clamp(
                            board_->GetCodec()->GetVolume() + delta_j->valueint, 0, 100);
                        if (board_->GetCodec()->SetVolume(volume) != ESP_OK) {
                            SendMcpResult(req_id, "codec rejected the volume change", true);
                        } else {
                            char result_text[64];
                            snprintf(result_text, sizeof(result_text), "speaker volume set to %d", volume);
                            SendMcpResult(req_id, result_text);
                        }
                    }
                } else if (strcmp(name, "display.adjust_brightness") == 0) {
                    cJSON *delta_j = args ? cJSON_GetObjectItemCaseSensitive(args, "delta") : nullptr;
                    if (!cJSON_IsNumber(delta_j) || delta_j->valueint < -100 || delta_j->valueint > 100) {
                        SendMcpResult(req_id, "delta must be an integer from -100 to 100", true);
                    } else {
                        const int brightness = std::clamp(
                            static_cast<int>(board_->GetBrightness()) + delta_j->valueint, 0, 100);
                        board_->SetBrightness(static_cast<uint8_t>(brightness));
                        char result_text[64];
                        snprintf(result_text, sizeof(result_text), "display brightness set to %d", brightness);
                        SendMcpResult(req_id, result_text);
                    }
                } else if (strcmp(name, "display.set_brightness") == 0) {
                    cJSON *brightness_j = args ? cJSON_GetObjectItemCaseSensitive(args, "brightness") : nullptr;
                    if (!cJSON_IsNumber(brightness_j) ||
                        brightness_j->valueint < 0 || brightness_j->valueint > 100) {
                        SendMcpResult(req_id, "brightness must be an integer from 0 to 100", true);
                    } else {
                        board_->SetBrightness(static_cast<uint8_t>(brightness_j->valueint));
                        char result_text[64];
                        snprintf(result_text, sizeof(result_text), "display brightness set to %d",
                                 brightness_j->valueint);
                        SendMcpResult(req_id, result_text);
                    }
                } else if (strcmp(name, "clock.configure") == 0) {
                    cJSON *style_j = args ? cJSON_GetObjectItemCaseSensitive(args, "style") : nullptr;
                    cJSON *color_j = args ? cJSON_GetObjectItemCaseSensitive(args, "color") : nullptr;
                    cJSON *mode_j = args ? cJSON_GetObjectItemCaseSensitive(args, "mode") : nullptr;
                    const char *style = cJSON_IsString(style_j) ? style_j->valuestring : "";
                    const char *color = cJSON_IsString(color_j) ? color_j->valuestring : "";
                    const char *mode = cJSON_IsString(mode_j) ? mode_j->valuestring : "";
                    const bool valid_style = strcmp(style, "digital") == 0 || strcmp(style, "minimal") == 0 ||
                                             strcmp(style, "analog") == 0 || strcmp(style, "flip") == 0 ||
                                             strcmp(style, "word") == 0 || strcmp(style, "binary") == 0;
                    const bool valid_mode = strcmp(mode, "dark") == 0 || strcmp(mode, "light") == 0;
                    unsigned int color_hex = 0;
                    const bool valid_color = strlen(color) == 7 && color[0] == '#' &&
                                             sscanf(color + 1, "%06x", &color_hex) == 1;
                    if (!valid_style || !valid_mode || !valid_color) {
                        SendMcpResult(req_id, "invalid clock settings", true);
                    } else if (apps_ == nullptr) {
                        SendMcpResult(req_id, "app manager is not ready", true);
                    } else if (!apps_->RequestClockSettings(style, color_hex, mode)) {
                        SendMcpResult(req_id, "clock command queue is full", true);
                    } else {
                        SendMcpResult(req_id, "clock settings queued");
                    }
                } else if (strcmp(name, "wallpaper.set") == 0) {
                    cJSON *url_j = args ? cJSON_GetObjectItemCaseSensitive(args, "url") : nullptr;
                    cJSON *name_j = args ? cJSON_GetObjectItemCaseSensitive(args, "name") : nullptr;
                    const char *url = cJSON_IsString(url_j) ? url_j->valuestring : "";
                    const char *wallpaper_name = cJSON_IsString(name_j) ? name_j->valuestring : "";
                    if (strncmp(url, "https://", 8) != 0 || strlen(url) > 512 || strlen(wallpaper_name) > 96) {
                        SendMcpResult(req_id, "wallpaper URL must be a valid HTTPS URL", true);
                    } else if (apps_ == nullptr) {
                        SendMcpResult(req_id, "app manager is not ready", true);
                    } else if (!apps_->RequestWallpaperUrl(url, wallpaper_name)) {
                        SendMcpResult(req_id, "wallpaper worker is busy", true);
                    } else {
                        SendMcpResult(req_id, "wallpaper download queued");
                    }
                } else if (strcmp(name, "wallpaper.sync") == 0) {
                    if (apps_ == nullptr) {
                        SendMcpResult(req_id, "app manager is not ready", true);
                    } else if (!apps_->RequestWallpaperSync()) {
                        SendMcpResult(req_id, "wallpaper worker is busy", true);
                    } else {
                        SendMcpResult(req_id, "wallpaper sync queued");
                    }
                } else if (strcmp(name, "app.launch") == 0) {
                    cJSON *app_j = args ? cJSON_GetObjectItemCaseSensitive(args, "app") : nullptr;
                    const char *app = cJSON_IsString(app_j) ? app_j->valuestring : "";
                    if (strcmp(app, "wallpaper") != 0 && strcmp(app, "clock") != 0 &&
                        strcmp(app, "tracking-status") != 0 &&
                        strcmp(app, "man-utd") != 0 && strcmp(app, "codex-credit") != 0) {
                        SendMcpResult(req_id, "unsupported app", true);
                    } else if (apps_ == nullptr) {
                        SendMcpResult(req_id, "app manager is not ready", true);
                    } else {
                        if (events_ == nullptr ||
                            !events_->Publish(EventType::AppLaunchRequested, TAG, app)) {
                            SendMcpResult(req_id, "app launch queue is full", true);
                        } else {
                            char result_text[64];
                            snprintf(result_text, sizeof(result_text), "queued app %s", app);
                            SendMcpResult(req_id, result_text);
                        }
                    }
                } else {
                    SendMcpResult(req_id, "unknown device tool", true);
                }
            }
        }
    }

    cJSON_Delete(root);
}

void AssistantService::HandleSystem(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    cJSON *text_j = cJSON_GetObjectItemCaseSensitive(root, "text");
    if (text_j && cJSON_IsString(text_j)) {
        ESP_LOGI(TAG, "System: %s", text_j->valuestring);
    }
    cJSON_Delete(root);
}

void AssistantService::HandleAlert(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    cJSON *text_j = cJSON_GetObjectItemCaseSensitive(root, "text");
    if (text_j && cJSON_IsString(text_j)) {
        ESP_LOGW(TAG, "Alert: %s", text_j->valuestring);
        if (events_) events_->Publish(EventType::AssistantError, TAG, text_j->valuestring);
    }
    cJSON_Delete(root);
}

// ─── State ────────────────────────────────────────────────────────────────────

void AssistantService::SetState(AssistantState s)
{
    const auto previous = static_cast<AssistantState>(
        state_.exchange(static_cast<uint8_t>(s))
    );
    if (previous == s || events_ == nullptr) return;

    const char *name = "idle";
    switch (s) {
    case AssistantState::Connecting: name = "connecting"; break;
    case AssistantState::Listening:  name = "listening";  break;
    case AssistantState::Processing: name = "processing"; break;
    case AssistantState::Speaking:   name = "speaking";   break;
    case AssistantState::Armed:      name = "armed";      break;
    case AssistantState::Idle:       name = "idle";       break;
    }
    events_->Publish(EventType::AssistantState, TAG, name);
}

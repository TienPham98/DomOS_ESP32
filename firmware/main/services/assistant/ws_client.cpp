#include "ws_client.h"

#include <cstring>
#include <cstdio>

#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_websocket_client.h"

static const char *TAG = "ws_client";

// ─────────────────────────────────────────────────────────────────────────────

WsClient::~WsClient()
{
    Disconnect();
}

bool WsClient::Connect(const WsClientConfig &cfg)
{
    if (client_ != nullptr) {
        Disconnect();
    }

    esp_websocket_client_config_t ws_cfg = {};
    if (cfg.uri == nullptr || cfg.uri[0] == '\0') {
        ESP_LOGE(TAG, "WebSocket URI is not configured");
        return false;
    }
    ws_cfg.uri                  = cfg.uri;
    ws_cfg.buffer_size          = 2048;  // One 1920-byte / 60 ms PCM frame plus WS header
    ws_cfg.task_stack           = 4096;
    ws_cfg.reconnect_timeout_ms = cfg.reconnect_ms > 0 ? cfg.reconnect_ms : 3000;
    // A cloud rolling deployment closes sockets cleanly (e.g. code 1012),
    // which otherwise stops the client instead of entering its retry loop.
    ws_cfg.enable_close_reconnect = true;
    ws_cfg.network_timeout_ms   = 10000;
    ws_cfg.keep_alive_enable    = true;
    ws_cfg.keep_alive_idle      = 10;
    ws_cfg.keep_alive_interval  = 5;
    ws_cfg.keep_alive_count     = 3;
    if (strncmp(cfg.uri, "wss://", 6) == 0) {
        ws_cfg.crt_bundle_attach = esp_crt_bundle_attach;
    }

    // HTTP headers: Authorization, Protocol-Version, Device-Id, Client-Id
    // Format: "Key: Value\r\nKey: Value\r\n"
    static char headers[256];
    memset(headers, 0, sizeof(headers));
    int offset = 0;
    if (cfg.auth_token && cfg.auth_token[0] != '\0') {
        offset += snprintf(headers + offset, sizeof(headers) - offset,
                           "Authorization: Bearer %s\r\n", cfg.auth_token);
    }
    offset += snprintf(headers + offset, sizeof(headers) - offset,
                       "Protocol-Version: %d\r\n", cfg.protocol_version);
    if (cfg.device_id && cfg.device_id[0] != '\0') {
        offset += snprintf(headers + offset, sizeof(headers) - offset,
                           "Device-Id: %s\r\n", cfg.device_id);
    }
    if (cfg.client_id && cfg.client_id[0] != '\0') {
        offset += snprintf(headers + offset, sizeof(headers) - offset,
                           "Client-Id: %s\r\n", cfg.client_id);
    }
    if (offset > 0) {
        ws_cfg.headers = headers;
    } else {
        ws_cfg.headers = nullptr;
    }

    client_ = esp_websocket_client_init(&ws_cfg);
    if (client_ == nullptr) {
        ESP_LOGE(TAG, "esp_websocket_client_init failed");
        return false;
    }

    esp_websocket_register_events(
        static_cast<esp_websocket_client_handle_t>(client_),
        WEBSOCKET_EVENT_ANY,
        WsEventHandler,
        this
    );

    ESP_LOGI(TAG, "Starting WebSocket (internal heap=%u, largest=%u)",
             static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)));
    esp_err_t ret = esp_websocket_client_start(
        static_cast<esp_websocket_client_handle_t>(client_)
    );
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "esp_websocket_client_start failed: %s", esp_err_to_name(ret));
        esp_websocket_client_destroy(static_cast<esp_websocket_client_handle_t>(client_));
        client_ = nullptr;
        return false;
    }
    ESP_LOGI(TAG, "Connecting to %s (buf=%d, reconnect=%dms)",
             ws_cfg.uri ? ws_cfg.uri : "?", ws_cfg.buffer_size, ws_cfg.reconnect_timeout_ms);
    started_.store(true);
    return true;
}

void WsClient::Disconnect()
{
    started_.store(false);
    if (client_ == nullptr) return;
    auto *handle = static_cast<esp_websocket_client_handle_t>(client_);
    esp_websocket_client_stop(handle);
    esp_websocket_client_destroy(handle);
    client_    = nullptr;
    connected_.store(false);
    ESP_LOGI(TAG, "Disconnected");
}

bool WsClient::SendText(const char *json, size_t len)
{
    if (!connected_.load() || client_ == nullptr) return false;
    if (len == 0) len = strlen(json);
    int ret = esp_websocket_client_send_text(
        static_cast<esp_websocket_client_handle_t>(client_),
        json, static_cast<int>(len), pdMS_TO_TICKS(2000)
    );
    if (ret <= 0) {
        ESP_LOGW(TAG, "Text frame send failed (len=%u, ret=%d)",
                 static_cast<unsigned>(len), ret);
    }
    return ret > 0;
}

bool WsClient::SendBinary(const uint8_t *data, size_t len)
{
    if (!connected_.load() || client_ == nullptr) return false;
    int ret = esp_websocket_client_send_bin(
        static_cast<esp_websocket_client_handle_t>(client_),
        reinterpret_cast<const char *>(data), static_cast<int>(len), pdMS_TO_TICKS(2000)
    );
    return ret > 0;
}

// ─── Event Handler ────────────────────────────────────────────────────────────

void WsClient::WsEventHandler(void *handler_args, esp_event_base_t /*base*/,
                               int32_t event_id, void *event_data)
{
    auto *self = static_cast<WsClient *>(handler_args);
    auto *data = static_cast<esp_websocket_event_data_t *>(event_data);

    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        self->receive_buffer_.Reset();
        self->connected_.store(true);
        ESP_LOGI(TAG, "Connected");
        if (self->event_cb_) self->event_cb_(true);
        break;

    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
        self->connected_.store(false);
        self->receive_buffer_.Reset();
        ESP_LOGW(TAG, "Disconnected");
        if (self->event_cb_) self->event_cb_(false);
        break;

    case WEBSOCKET_EVENT_DATA: {
        if (!data || data->data_len < 0 || data->payload_len < 0 || data->payload_offset < 0) break;
        const auto result = self->receive_buffer_.Append(
            data->op_code, data->fin, data->payload_len, data->payload_offset,
            data->data_ptr, data->data_len);
        if (result == WsMessageBuffer::Result::Complete) {
            const auto& message = self->receive_buffer_.Message();
            if (self->receive_buffer_.Opcode() == 1 && self->text_cb_)
                self->text_cb_(message.data(), message.size());
            else if (self->receive_buffer_.Opcode() == 2 && self->binary_cb_)
                self->binary_cb_(reinterpret_cast<const uint8_t*>(message.data()), message.size());
            self->receive_buffer_.Reset();
        } else if (result == WsMessageBuffer::Result::Invalid) {
            ESP_LOGW(TAG, "Discarded invalid or oversized WebSocket message");
        }
        break;
    }

    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "WebSocket error");
        break;

    default:
        break;
    }
}

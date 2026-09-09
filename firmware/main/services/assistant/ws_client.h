#pragma once
/**
 * ws_client.h — WebSocket client wrapper cho ESP32
 * Dựa trên esp_websocket_client (ESP-IDF built-in).
 * Hỗ trợ:
 *   - kết nối với Authorization / Protocol-Version / Device-Id headers
 *   - callback riêng cho text frame và binary frame
 *   - auto-reconnect
 */

#include <cstdint>
#include <cstddef>
#include <functional>
#include <string>
#include <atomic>

#include "esp_event.h"
#include "ws_message_buffer.h"

using WsTextCallback   = std::function<void(const char *data, size_t len)>;
using WsBinaryCallback = std::function<void(const uint8_t *data, size_t len)>;
using WsEventCallback  = std::function<void(bool connected)>;

struct WsClientConfig {
    const char *uri;               // ws://host:port/path
    const char *auth_token;        // Bearer token (optional, nullptr to skip)
    const char *device_id;         // MAC address string
    const char *client_id;         // UUID software identifier
    int         protocol_version;  // 3
    int         reconnect_ms;      // reconnect delay (default 3000)
};

class WsClient {
public:
    WsClient() = default;
    ~WsClient();

    bool Connect(const WsClientConfig &cfg);
    void Disconnect();
    bool IsConnected() const { return connected_.load(); }
    // A disconnected client still owns its automatic reconnect loop.
    bool IsStarted() const { return started_.load(); }

    bool SendText(const char *json, size_t len = 0);
    bool SendBinary(const uint8_t *data, size_t len);

    void OnText(WsTextCallback cb)   { text_cb_   = cb; }
    void OnBinary(WsBinaryCallback cb) { binary_cb_ = cb; }
    void OnConnect(WsEventCallback cb) { event_cb_  = cb; }

private:
    static void WsEventHandler(void *handler_args, esp_event_base_t base,
                                int32_t event_id, void *event_data);

    void *client_          = nullptr;  // esp_websocket_client_handle_t
    std::atomic<bool> connected_{false};
    std::atomic<bool> started_{false};
    WsTextCallback   text_cb_;
    WsBinaryCallback binary_cb_;
    WsEventCallback  event_cb_;
    WsMessageBuffer receive_buffer_;
};

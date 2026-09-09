#pragma once
/**
 * assistant_service.h — Dom Voice AI Assistant Service
 *
 * Full duplex voice session management:
 *   - WebSocket connection to Python AI Gateway
 *   - Voice Protocol (hello/listen/tts/stt/mcp/emotion)
 *   - State machine: Idle → Connecting → Listening → Processing → Speaking → Listening
 *   - Audio pipeline integration (mic capture + speaker output)
 *   - Subtitles & reactive emotion states for UI Avatar
 */

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>

#include "audio_pipeline.h"
#include "kernel/event_bus.h"
#include "ws_client.h"

// Forward declare
class ES3C28PBoard;
class AppManager;

enum class AssistantState : uint8_t {
    Idle       = 0,
    Connecting = 1,
    Listening  = 2,
    Processing = 3,
    Speaking   = 4,
    Armed      = 5,
};

struct AssistantConfig {
    const char *ws_uri;       // Supplied by CONFIG_DOMOS_AI_WS_URI.
    const char *auth_token;   // Bearer token (from Kconfig/NVS)
    const char *device_id;    // MAC address string
    const char *client_id;    // UUID
};

using AssistantUiCallback = std::function<void()>;

class AssistantService {
public:
    bool Start(ES3C28PBoard *board, EventBus *events, const AssistantConfig &cfg);
    void Stop();

    // Bắt đầu / kết thúc phiên thoại (gọi từ UI hoặc nút bấm)
    void OpenAudioChannel();
    void CloseAudioChannel();
    // Manually finish the current utterance without closing the cloud session.
    // Normally the gateway VAD finishes it automatically in auto listening mode.
    void SubmitSpeech();
    void ActivateListening();
    void CancelResponse();

    AssistantState GetState() const { return static_cast<AssistantState>(state_.load()); }
    bool IsActive() const { return static_cast<AssistantState>(state_.load()) != AssistantState::Idle; }

    // UI display data for Dom Voice Assistant
    std::string GetEmotion() const;
    std::string GetUserText() const;
    std::string GetAssistantText() const;

    void SetUiCallback(AssistantUiCallback cb) { ui_cb_ = cb; }
    void SetAppManager(AppManager *apps) { apps_ = apps; }

private:
    // WebSocket callbacks
    void OnWsText(const char *data, size_t len);
    void OnWsBinary(const uint8_t *data, size_t len);
    void OnWsConnect(bool connected);

    // Protocol handlers
    void HandleHello(const char *json);
    void HandleListen(const char *json);
    void HandleStt(const char *json);
    void HandleLlm(const char *json);
    void HandleTts(const char *json);
    void HandleMcp(const char *json);
    void HandleSystem(const char *json);
    void HandleAlert(const char *json);

    // Helpers
    void SetState(AssistantState s);
    void SendHello();
    void SendWakeStart();
    void SendListenStart();
    void SendListenStop();
    void SendAbort(const char *reason = "user_interrupted");
    void SendMcpResult(int req_id, const char *text, bool is_error = false);

    void NotifyUi();
    void FinishPlaybackIfDrained();

    ES3C28PBoard          *board_   = nullptr;
    AppManager            *apps_    = nullptr;
    EventBus              *events_  = nullptr;
    AssistantConfig        cfg_     = {};

    WsClient               ws_;
    AudioPipeline          pipeline_;

    std::atomic<uint8_t>  state_{static_cast<uint8_t>(AssistantState::Idle)};
    std::atomic<bool> handshake_done_{false};
    std::atomic<bool> tts_active_{false};
    std::atomic<bool> response_aborted_{false};
    std::mutex playback_mutex_;
    bool playback_finishing_ = false;
    AssistantState after_playback_ = AssistantState::Armed;

    mutable std::mutex mutex_;
    std::mutex channel_mutex_;
    std::string emotion_        = "idle";
    std::string user_text_      = "";
    std::string assistant_text_ = "";
    AssistantUiCallback ui_cb_  = nullptr;
};

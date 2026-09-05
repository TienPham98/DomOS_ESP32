#pragma once

#include <cstddef>
#include <cstdint>

enum class EventType : uint8_t {
    WifiConnected,
    WifiDisconnected,
    MqttConnected,
    MqttMessage,
    ThemeChanged,
    OtaAvailable,
    OtaProgress,
    // AI Assistant events
    AssistantState,
    AssistantConnected,
    AssistantDisconnected,
    AssistantListening,
    AssistantProcessing,
    AssistantSpeaking,
    AssistantError,
    AppLaunchRequested,
};

struct DomosEvent {
    EventType type;
    char source[16];
    char data[96];
};

using EventListener = void (*)(const DomosEvent &event, void *context);

class EventBus {
public:
    bool Init();
    bool Publish(EventType type, const char *source, const char *data = "");
    bool Subscribe(EventType type, EventListener listener, void *context);
    void DispatchPending();

private:
    static constexpr size_t kMaxListeners = 12;
    struct Listener {
        EventType type;
        EventListener callback;
        void *context;
    };

    void *queue_ = nullptr;
    Listener listeners_[kMaxListeners]{};
    size_t listener_count_ = 0;
};

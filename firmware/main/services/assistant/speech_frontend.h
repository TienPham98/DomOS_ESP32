#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>

enum class SpeechMode { Disabled, Armed, Listening };
struct SpeechContext {
    SpeechMode mode;
    uint32_t generation;
};

struct SpeechFrontendConfig {
    std::function<SpeechContext()> context;
    // These callbacks run on the AFE fetch task; must never block or use network.
    std::function<void(const int16_t *, size_t)> on_audio;
    std::function<void(uint32_t)> on_wake;
    std::function<void(uint32_t)> on_speech_end;
};

class SpeechFrontend {
public:
    bool Start(const SpeechFrontendConfig &config);
    bool Stop();
    // Bounded/non-blocking submission from the real-time capture task.
    bool Submit(const int16_t *pcm, size_t samples);
    bool IsReady() const { return ready_.load(); }
private:
    struct Impl;
    Impl *impl_ = nullptr;
    std::atomic<bool> ready_{false};
};

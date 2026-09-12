#pragma once

#include <cstddef>
#include <cstdint>

// Audio-time endpointing: network stalls cannot turn queued speech into silence.
// Reset when the assistant changes state or input frames are lost.
class SpeechEndpoint {
public:
    void Reset() { speech_samples_ = silence_samples_ = 0; fired_ = false; }
    bool Feed(bool speech, size_t samples) {
        if (fired_) return false;
        if (speech) {
            speech_samples_ += samples;
            silence_samples_ = 0;
        } else if (speech_samples_ >= 1600) {
            silence_samples_ += samples;
            if (silence_samples_ >= 32000) { fired_ = true; return true; }
        }
        return false;
    }
private:
    size_t speech_samples_ = 0;
    size_t silence_samples_ = 0;
    bool fired_ = false;
};

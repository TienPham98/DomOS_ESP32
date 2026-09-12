#pragma once

#include <cstddef>
#include <cstdint>

// Small ownership wrapper around Espressif's Opus encoder. One input frame is
// always 60 ms of mono PCM16 at 16 kHz (960 samples), matching voice protocol v3.
class OpusEncoder {
public:
    ~OpusEncoder();

    bool Start();
    void Stop();
    bool Encode(const int16_t *pcm, size_t samples,
                uint8_t *output, size_t capacity, size_t &encoded_bytes);

    bool IsReady() const { return handle_ != nullptr; }
    size_t MaxPacketBytes() const { return output_bytes_; }

private:
    void *handle_ = nullptr;
    size_t frame_samples_ = 0;
    size_t output_bytes_ = 0;
    int16_t *input_buffer_ = nullptr;
    uint8_t *output_buffer_ = nullptr;
};

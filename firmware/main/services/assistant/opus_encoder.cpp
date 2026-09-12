#include "opus_encoder.h"

#include "esp_audio_enc.h"
#include "esp_audio_types.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_opus_enc.h"

#include <cstring>

namespace {
constexpr char kTag[] = "opus_encoder";
constexpr size_t kExpectedFrameSamples = 960;
}

OpusEncoder::~OpusEncoder()
{
    Stop();
}

bool OpusEncoder::Start()
{
    if (handle_ != nullptr) return true;

    esp_opus_enc_config_t config{};
    config.sample_rate = ESP_AUDIO_SAMPLE_RATE_16K;
    config.channel = ESP_AUDIO_MONO;
    config.bits_per_sample = ESP_AUDIO_BIT16;
    config.bitrate = ESP_OPUS_BITRATE_AUTO;
    config.frame_duration = ESP_OPUS_ENC_FRAME_DURATION_60_MS;
    // Keep the same encoder profile used by Xiaozhi's microphone uplink.
    config.application_mode = ESP_OPUS_ENC_APPLICATION_AUDIO;
    config.complexity = 0;
    config.enable_fec = false;
    config.enable_dtx = true;
    config.enable_vbr = true;

    const auto result = esp_opus_enc_open(&config, sizeof(config), &handle_);
    int frame_bytes = 0;
    int output_bytes = 0;
    if (result != ESP_AUDIO_ERR_OK || handle_ == nullptr ||
        esp_opus_enc_get_frame_size(handle_, &frame_bytes, &output_bytes) != ESP_AUDIO_ERR_OK) {
        ESP_LOGE(kTag, "Encoder initialization failed: %d", static_cast<int>(result));
        Stop();
        return false;
    }
    frame_samples_ = static_cast<size_t>(frame_bytes) / sizeof(int16_t);
    output_bytes_ = static_cast<size_t>(output_bytes);
    if (frame_samples_ != kExpectedFrameSamples || output_bytes_ == 0) {
        ESP_LOGE(kTag, "Unexpected frame geometry: samples=%u output=%u",
                 static_cast<unsigned>(frame_samples_), static_cast<unsigned>(output_bytes_));
        Stop();
        return false;
    }
    // Espressif's optimized Opus routines expect DMA/SIMD-safe internal
    // buffers. The surrounding queues and task stack may remain in PSRAM.
    input_buffer_ = static_cast<int16_t *>(heap_caps_malloc(
        frame_samples_ * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    output_buffer_ = static_cast<uint8_t *>(heap_caps_malloc(
        output_bytes_, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (input_buffer_ == nullptr || output_buffer_ == nullptr) {
        ESP_LOGE(kTag, "Internal codec buffer allocation failed");
        Stop();
        return false;
    }
    ESP_LOGI(kTag, "Opus uplink ready: frame=%u samples max_packet=%u bytes",
             static_cast<unsigned>(frame_samples_), static_cast<unsigned>(output_bytes_));
    return true;
}

void OpusEncoder::Stop()
{
    if (handle_ != nullptr) {
        esp_opus_enc_close(handle_);
        handle_ = nullptr;
    }
    heap_caps_free(input_buffer_);
    heap_caps_free(output_buffer_);
    input_buffer_ = nullptr;
    output_buffer_ = nullptr;
    frame_samples_ = 0;
    output_bytes_ = 0;
}

bool OpusEncoder::Encode(const int16_t *pcm, size_t samples,
                         uint8_t *output, size_t capacity, size_t &encoded_bytes)
{
    encoded_bytes = 0;
    if (!handle_ || !input_buffer_ || !output_buffer_ || !pcm ||
        samples != frame_samples_ || !output || capacity < output_bytes_)
        return false;

    memcpy(input_buffer_, pcm, samples * sizeof(int16_t));
    esp_audio_enc_in_frame_t input{};
    input.buffer = reinterpret_cast<uint8_t *>(input_buffer_);
    input.len = static_cast<uint32_t>(samples * sizeof(int16_t));
    esp_audio_enc_out_frame_t encoded{};
    encoded.buffer = output_buffer_;
    encoded.len = static_cast<uint32_t>(output_bytes_);
    if (esp_opus_enc_process(handle_, &input, &encoded) != ESP_AUDIO_ERR_OK)
        return false;
    encoded_bytes = encoded.encoded_bytes;
    if (encoded_bytes > capacity) return false;
    memcpy(output, output_buffer_, encoded_bytes);
    return encoded_bytes > 0;
}

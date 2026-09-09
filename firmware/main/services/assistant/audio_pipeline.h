#pragma once
/**
 * audio_pipeline.h — FreeRTOS multi-task audio pipeline cho Voice AI
 *
 * Tasks:
 *   MicCaptureTask  (core 1, pri 7) : I2S RX → PCM ring buffer
 *   AudioOutputTask (core 0, pri 7) : output queue → I2S TX → speaker
 *
 * Thread-safe queues cho data flow.
 */

#include <cstddef>
#include <cstdint>
#include <functional>
#include <atomic>
#include <mutex>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

class ES3C28PBoard;

// Callback được gọi khi mic đọc được một chunk PCM đủ lớn
using MicChunkCallback = std::function<void(const int16_t *pcm, size_t samples)>;

struct AudioPipelineConfig {
    // Kích thước mỗi chunk mic đọc (samples = 960 cho 60ms @ 16kHz)
    size_t mic_chunk_samples = 960;
    // Số lượng chunk tối đa trong output queue
    size_t output_queue_depth = 8;
    // Callback nhận PCM từ mic
    MicChunkCallback on_mic_data;
    // Runs on the network/uplink task, never on the real-time I2S tasks.
    std::function<void()> on_service_tick;
};

class AudioPipeline {
public:
    bool Start(ES3C28PBoard *board, const AudioPipelineConfig &cfg);
    void Stop();

    // Đẩy PCM chunk vào hàng đợi output (phát qua speaker)
    // Non-blocking: bỏ qua nếu queue đầy
    bool EnqueueAudio(const int16_t *pcm, size_t samples);

    // Xóa output queue (dùng khi abort)
    void FlushOutput();
    bool IsOutputDrained();

    bool IsRunning() const { return running_.load(); }

private:
    static void MicTask(void *arg);
    static void UplinkTask(void *arg);
    static void OutputTask(void *arg);

    ES3C28PBoard       *board_  = nullptr;
    AudioPipelineConfig cfg_;
    std::atomic<bool>   running_{false};
    void               *output_queue_ = nullptr;  // QueueHandle_t
    void               *output_queue_storage_ = nullptr;
    StaticQueue_t        output_queue_control_{};
    void               *mic_queue_ = nullptr;
    void               *mic_queue_storage_ = nullptr;
    StaticQueue_t        mic_queue_control_{};
    std::atomic<TaskHandle_t> mic_task_{nullptr};
    std::atomic<TaskHandle_t> uplink_task_{nullptr};
    std::atomic<TaskHandle_t> output_task_{nullptr};
    std::mutex output_mutex_;
    bool output_in_flight_ = false;
    int64_t output_written_at_us_ = 0;

    struct AudioChunk {
        int16_t samples[960];  // max 60ms @ 16kHz
        size_t  count;
    };
};

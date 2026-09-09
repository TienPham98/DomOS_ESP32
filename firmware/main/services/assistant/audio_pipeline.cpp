#include "audio_pipeline.h"

#include <cstring>
#include "board/es3c28p/board_es3c28p.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "audio_pipeline";

// ─────────────────────────────────────────────────────────────────────────────

bool AudioPipeline::Start(ES3C28PBoard *board, const AudioPipelineConfig &cfg)
{
    if (running_.load()) return true;
    if (board == nullptr || cfg.mic_chunk_samples == 0 || cfg.mic_chunk_samples > 960 ||
        cfg.output_queue_depth == 0) {
        ESP_LOGE(TAG, "Invalid audio pipeline configuration");
        return false;
    }
    board_  = board;
    cfg_    = cfg;

    // Tạo output queue (AudioChunk items)
    // Keep the large frame queue in PSRAM to preserve internal heap for Wi-Fi,
    // WebSocket and the two real-time task stacks.
    const size_t queue_bytes = cfg_.output_queue_depth * sizeof(AudioChunk);
    output_queue_storage_ = heap_caps_malloc(queue_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (output_queue_storage_ == nullptr) {
        ESP_LOGE(TAG, "PSRAM output queue allocation failed (%d bytes)", (int)queue_bytes);
        return false;
    }
    output_queue_ = xQueueCreateStatic(
        cfg_.output_queue_depth,
        sizeof(AudioChunk),
        static_cast<uint8_t *>(output_queue_storage_),
        &output_queue_control_
    );
    if (output_queue_ == nullptr) {
        ESP_LOGE(TAG, "Output queue create failed");
        heap_caps_free(output_queue_storage_);
        output_queue_storage_ = nullptr;
        return false;
    }

    constexpr size_t kMicQueueDepth = 3;
    const size_t mic_queue_bytes = kMicQueueDepth * sizeof(AudioChunk);
    mic_queue_storage_ = heap_caps_malloc(mic_queue_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (mic_queue_storage_ == nullptr) {
        ESP_LOGE(TAG, "PSRAM mic queue allocation failed (%d bytes)", (int)mic_queue_bytes);
        vQueueDelete(static_cast<QueueHandle_t>(output_queue_));
        output_queue_ = nullptr;
        heap_caps_free(output_queue_storage_);
        output_queue_storage_ = nullptr;
        return false;
    }
    mic_queue_ = xQueueCreateStatic(
        kMicQueueDepth,
        sizeof(AudioChunk),
        static_cast<uint8_t *>(mic_queue_storage_),
        &mic_queue_control_
    );
    if (mic_queue_ == nullptr) {
        ESP_LOGE(TAG, "Mic queue create failed");
        heap_caps_free(mic_queue_storage_);
        mic_queue_storage_ = nullptr;
        vQueueDelete(static_cast<QueueHandle_t>(output_queue_));
        output_queue_ = nullptr;
        heap_caps_free(output_queue_storage_);
        output_queue_storage_ = nullptr;
        return false;
    }

    // Set the run flag before creating a higher-priority task: it may execute
    // immediately and must not mistake startup for a stop request.
    running_.store(true);

    // Mic capture task — Core 1, priority 7.
    TaskHandle_t mic_handle = nullptr;
    if (xTaskCreatePinnedToCore(MicTask, "mic_capture", 4096, this, 7, &mic_handle, 1) != pdPASS) {
        ESP_LOGE(TAG, "MicTask create failed");
        running_.store(false);
        vQueueDelete(static_cast<QueueHandle_t>(output_queue_));
        output_queue_ = nullptr;
        heap_caps_free(output_queue_storage_);
        output_queue_storage_ = nullptr;
        vQueueDelete(static_cast<QueueHandle_t>(mic_queue_));
        mic_queue_ = nullptr;
        heap_caps_free(mic_queue_storage_);
        mic_queue_storage_ = nullptr;
        return false;
    }
    mic_task_.store(mic_handle);

    // Audio output task — Core 0, priority 7.
    TaskHandle_t output_handle = nullptr;
    if (xTaskCreatePinnedToCore(OutputTask, "audio_out", 4096, this, 7, &output_handle, 0) != pdPASS) {
        ESP_LOGE(TAG, "OutputTask create failed");
        running_.store(false);
        while (mic_task_.load() != nullptr) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        vQueueDelete(static_cast<QueueHandle_t>(output_queue_));
        output_queue_ = nullptr;
        heap_caps_free(output_queue_storage_);
        output_queue_storage_ = nullptr;
        vQueueDelete(static_cast<QueueHandle_t>(mic_queue_));
        mic_queue_ = nullptr;
        heap_caps_free(mic_queue_storage_);
        mic_queue_storage_ = nullptr;
        return false;
    }
    output_task_.store(output_handle);

    // Network transmission is separated from MicTask. It may wait on TCP
    // without ever blocking I2S capture; its stack is allocated in PSRAM.
    TaskHandle_t uplink_handle = nullptr;
    if (xTaskCreatePinnedToCoreWithCaps(UplinkTask, "audio_uplink", 8192, this, 6,
                                        &uplink_handle, 1,
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        ESP_LOGE(TAG, "UplinkTask create failed");
        running_.store(false);
        while (mic_task_.load() != nullptr || output_task_.load() != nullptr) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        vQueueDelete(static_cast<QueueHandle_t>(output_queue_));
        output_queue_ = nullptr;
        heap_caps_free(output_queue_storage_);
        output_queue_storage_ = nullptr;
        vQueueDelete(static_cast<QueueHandle_t>(mic_queue_));
        mic_queue_ = nullptr;
        heap_caps_free(mic_queue_storage_);
        mic_queue_storage_ = nullptr;
        return false;
    }
    uplink_task_.store(uplink_handle);

    ESP_LOGI(TAG, "Audio pipeline started (chunk=%d samples)", (int)cfg_.mic_chunk_samples);
    return true;
}

void AudioPipeline::Stop()
{
    if (!running_.exchange(false)) return;

    // Wait for I2S/queue timeouts and the bounded WebSocket send timeout.
    for (int i = 0; i < 75 &&
         (mic_task_.load() != nullptr || uplink_task_.load() != nullptr ||
          output_task_.load() != nullptr); ++i) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (mic_task_.load() != nullptr || uplink_task_.load() != nullptr ||
        output_task_.load() != nullptr) {
        ESP_LOGE(TAG, "Audio task shutdown timed out; retaining queue to avoid use-after-free");
        return;
    }
    if (output_queue_) {
        vQueueDelete(static_cast<QueueHandle_t>(output_queue_));
        output_queue_ = nullptr;
    }
    if (output_queue_storage_) {
        heap_caps_free(output_queue_storage_);
        output_queue_storage_ = nullptr;
    }
    if (mic_queue_) {
        vQueueDelete(static_cast<QueueHandle_t>(mic_queue_));
        mic_queue_ = nullptr;
    }
    if (mic_queue_storage_) {
        heap_caps_free(mic_queue_storage_);
        mic_queue_storage_ = nullptr;
    }
    ESP_LOGI(TAG, "Audio pipeline stopped");
}

bool AudioPipeline::EnqueueAudio(const int16_t *pcm, size_t samples)
{
    if (!running_.load() || output_queue_ == nullptr || pcm == nullptr || samples == 0 || samples > 960) return false;
    AudioChunk chunk;
    chunk.count = samples;
    memcpy(chunk.samples, pcm, samples * sizeof(int16_t));
    std::lock_guard<std::mutex> lock(output_mutex_);
    return xQueueSend(static_cast<QueueHandle_t>(output_queue_), &chunk, 0) == pdPASS;
}

void AudioPipeline::FlushOutput()
{
    std::lock_guard<std::mutex> lock(output_mutex_);
    if (output_queue_) {
        xQueueReset(static_cast<QueueHandle_t>(output_queue_));
    }
}

bool AudioPipeline::IsOutputDrained()
{
    std::lock_guard<std::mutex> lock(output_mutex_);
    // Queue empty does not mean the codec has finished. Account for the frame
    // in I2S_WritePCM and the ES3C28P default DMA ring (6 * 240 / 16k = 90 ms).
    constexpr int64_t kDmaTailUs = 120000;
    return !output_in_flight_ &&
        (!output_queue_ || uxQueueMessagesWaiting(static_cast<QueueHandle_t>(output_queue_)) == 0) &&
        (output_written_at_us_ == 0 || esp_timer_get_time() - output_written_at_us_ >= kDmaTailUs);
}

// ─── Task implementations ─────────────────────────────────────────────────────

void AudioPipeline::MicTask(void *arg)
{
    auto *self = static_cast<AudioPipeline *>(arg);
    const size_t chunk_samples = self->cfg_.mic_chunk_samples > 0
                                     ? self->cfg_.mic_chunk_samples
                                     : 960;
    auto *chunk = static_cast<AudioChunk *>(
        heap_caps_calloc(1, sizeof(AudioChunk), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    auto *stale = static_cast<AudioChunk *>(
        heap_caps_malloc(sizeof(AudioChunk), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (chunk == nullptr || stale == nullptr) {
        ESP_LOGE(TAG, "MicTask: frame buffer allocation failed");
        heap_caps_free(chunk);
        heap_caps_free(stale);
        self->running_.store(false);
        self->mic_task_.store(nullptr);
        vTaskDelete(nullptr);
        return;
    }
    chunk->count = chunk_samples;

    ESP_LOGI(TAG, "MicTask running on core %d", (int)xPortGetCoreID());

    while (self->running_.load()) {
        esp_err_t ret = self->board_->I2S_ReadPCM(chunk->samples, chunk_samples);
        if (ret == ESP_OK) {
            auto queue = static_cast<QueueHandle_t>(self->mic_queue_);
            if (xQueueSend(queue, chunk, 0) != pdPASS) {
                // Prefer the newest capture frame if the network is behind.
                xQueueReceive(queue, stale, 0);
                xQueueSend(queue, chunk, 0);
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
    }

    heap_caps_free(chunk);
    heap_caps_free(stale);
    self->mic_task_.store(nullptr);
    ESP_LOGI(TAG, "MicTask exited");
    vTaskDelete(nullptr);
}

void AudioPipeline::UplinkTask(void *arg)
{
    auto *self = static_cast<AudioPipeline *>(arg);
    AudioChunk chunk;

    ESP_LOGI(TAG, "UplinkTask running on core %d", (int)xPortGetCoreID());
    while (self->running_.load()) {
        if (self->cfg_.on_service_tick) self->cfg_.on_service_tick();
        if (xQueueReceive(static_cast<QueueHandle_t>(self->mic_queue_),
                          &chunk, pdMS_TO_TICKS(50)) == pdPASS &&
            self->cfg_.on_mic_data && self->running_.load()) {
            self->cfg_.on_mic_data(chunk.samples, chunk.count);
        }
    }

    self->uplink_task_.store(nullptr);
    ESP_LOGI(TAG, "UplinkTask exited");
    vTaskDelete(nullptr);
}

void AudioPipeline::OutputTask(void *arg)
{
    auto *self = static_cast<AudioPipeline *>(arg);
    auto *chunk = static_cast<AudioChunk *>(
        heap_caps_malloc(sizeof(AudioChunk), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (chunk == nullptr) {
        ESP_LOGE(TAG, "OutputTask: frame buffer allocation failed");
        self->running_.store(false);
        self->output_task_.store(nullptr);
        vTaskDelete(nullptr);
        return;
    }

    ESP_LOGI(TAG, "OutputTask running on core %d", (int)xPortGetCoreID());

    while (self->running_.load()) {
        bool received = false;
        {
            std::lock_guard<std::mutex> lock(self->output_mutex_);
            received = xQueueReceive(static_cast<QueueHandle_t>(self->output_queue_), chunk, 0) == pdPASS;
            if (received) self->output_in_flight_ = true;
        }
        if (received) {
            const esp_err_t result = self->board_->I2S_WritePCM(chunk->samples, chunk->count);
            {
                std::lock_guard<std::mutex> lock(self->output_mutex_);
                self->output_written_at_us_ = esp_timer_get_time();
                self->output_in_flight_ = false;
            }
            if (result != ESP_OK) ESP_LOGE(TAG, "I2S output failed: %s", esp_err_to_name(result));
        } else {
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }

    heap_caps_free(chunk);
    self->output_task_.store(nullptr);
    ESP_LOGI(TAG, "OutputTask exited");
    vTaskDelete(nullptr);
}

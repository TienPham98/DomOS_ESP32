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
    if (output_queue_ || mic_queue_) {
        ESP_LOGE(TAG, "Previous audio shutdown has not completed");
        return false;
    }
    if (board == nullptr || cfg.mic_chunk_samples == 0 || cfg.mic_chunk_samples > 960 ||
        cfg.output_queue_depth == 0) {
        ESP_LOGE(TAG, "Invalid audio pipeline configuration");
        return false;
    }
    board_  = board;
    cfg_    = cfg;
    // Keep raw PCM as a graceful compatibility fallback if codec allocation
    // fails, but normally use the compact 60 ms Opus path used by Xiaozhi.
    opus_encoder_.Start();

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

    // Absorb up to 2.9 seconds of transient TLS/Wi-Fi backpressure. Storage is
    // in PSRAM, so this does not consume the scarce internal heap.
    constexpr size_t kMicQueueDepth = 48;
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

    auto frontend_config = cfg_.frontend;
    frontend_config.on_audio = [this](const int16_t *pcm, size_t samples) {
        AudioChunk chunk;
        chunk.count = samples;
        memcpy(chunk.samples, pcm, samples * sizeof(int16_t));
        // AFE inference never waits for network transmission.
        if (xQueueSend(static_cast<QueueHandle_t>(mic_queue_), &chunk, 0) != pdPASS) {
            const uint32_t dropped = mic_dropped_.fetch_add(1) + 1;
            if (dropped == 1 || dropped % 32 == 0) {
                ESP_LOGW(TAG, "Processed microphone queue full; dropped=%lu",
                         static_cast<unsigned long>(dropped));
            }
        }
    };
    frontend_.Start(frontend_config); // Missing model falls back to existing cloud wake.

    // Set the run flag before creating a higher-priority task: it may execute
    // immediately and must not mistake startup for a stop request.
    running_.store(true);
    mic_dropped_.store(0);

    // Mic capture task — Core 1, priority 7.
    TaskHandle_t mic_handle = nullptr;
    if (xTaskCreatePinnedToCore(MicTask, "mic_capture", 4096, this, 7, &mic_handle, 1) != pdPASS) {
        ESP_LOGE(TAG, "MicTask create failed");
        running_.store(false);
        if (!frontend_.Stop()) return false;
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
        if (!frontend_.Stop()) return false;
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
    // The prebuilt Opus encoder uses a sizeable temporary stack while its
    // analysis window runs. Keep that stack in PSRAM so Wi-Fi internal heap is
    // unaffected, and leave headroom for the nested WebSocket send call.
    // Xiaozhi reserves 12 * 2048 bytes for its Opus codec worker. Match that
    // proven stack budget because Espressif's optimized encoder has deep
    // analysis call frames even at complexity zero.
    if (xTaskCreatePinnedToCoreWithCaps(UplinkTask, "audio_uplink", 24576, this, 6,
                                        &uplink_handle, 1,
                                        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        ESP_LOGE(TAG, "UplinkTask create failed");
        running_.store(false);
        while (mic_task_.load() != nullptr || output_task_.load() != nullptr) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        if (!frontend_.Stop()) return false;
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
    running_.store(false);

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
    // The frontend owns another producer of mic_queue_. Join it before freeing
    // that queue, and only stop recognition when the whole pipeline stops.
    if (!frontend_.Stop()) return;
    opus_encoder_.Stop();
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

bool AudioPipeline::IsMicInputDrained() const
{
    return mic_queue_ == nullptr ||
           uxQueueMessagesWaiting(static_cast<QueueHandle_t>(mic_queue_)) == 0;
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
            if (self->frontend_.IsReady()) {
                self->frontend_.Submit(chunk->samples, chunk_samples);
                continue;
            }
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
    const size_t encoded_capacity = self->opus_encoder_.IsReady()
                                        ? self->opus_encoder_.MaxPacketBytes()
                                        : 0;
    auto *encoded = encoded_capacity > 0
        ? static_cast<uint8_t *>(heap_caps_malloc(
              encoded_capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT))
        : nullptr;
    if (encoded_capacity > 0 && encoded == nullptr) {
        ESP_LOGE(TAG, "UplinkTask: Opus packet allocation failed");
        self->running_.store(false);
        self->uplink_task_.store(nullptr);
        vTaskDeleteWithCaps(nullptr);
        return;
    }

    ESP_LOGI(TAG, "UplinkTask running on core %d", (int)xPortGetCoreID());
    while (self->running_.load()) {
        if (self->cfg_.on_service_tick) self->cfg_.on_service_tick();
        if (xQueueReceive(static_cast<QueueHandle_t>(self->mic_queue_),
                          &chunk, pdMS_TO_TICKS(50)) == pdPASS &&
            self->cfg_.on_mic_data && self->running_.load()) {
            if (self->opus_encoder_.IsReady()) {
                size_t encoded_bytes = 0;
                if (self->opus_encoder_.Encode(chunk.samples, chunk.count, encoded,
                                               encoded_capacity, encoded_bytes)) {
                    self->cfg_.on_mic_data(encoded, encoded_bytes);
                } else {
                    ESP_LOGW(TAG, "Opus frame encode failed");
                }
            } else {
                self->cfg_.on_mic_data(
                    reinterpret_cast<const uint8_t *>(chunk.samples),
                    chunk.count * sizeof(int16_t));
            }
        }
    }

    heap_caps_free(encoded);
    self->uplink_task_.store(nullptr);
    ESP_LOGI(TAG, "UplinkTask exited");
    vTaskDeleteWithCaps(nullptr);
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

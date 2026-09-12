#include "speech_frontend.h"
#include "sdkconfig.h"

#if CONFIG_DOMOS_ESP_SR
#include "speech_endpoint.h"
#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "esp_vadn_models.h"
#include "model_path.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <new>
#include <vector>

namespace {
constexpr char kTag[] = "speech_frontend";
constexpr size_t kFrameSamples = 960;
constexpr size_t kInputDepth = 8;

int SignalRms(const int16_t *pcm, size_t samples) {
    if (samples == 0) return 0;
    int64_t sum = 0, square_sum = 0;
    for (size_t i = 0; i < samples; ++i) {
        const int64_t sample = pcm[i];
        sum += sample;
        square_sum += sample * sample;
    }
    const double mean = static_cast<double>(sum) / samples;
    return static_cast<int>(std::sqrt(std::max(0.0,
        static_cast<double>(square_sum) / samples - mean * mean)));
}

// The vendor loader trusts its header. Reject missing/truncated model metadata
// before allowing it to allocate memory using counts read from flash.
bool ModelPartitionValid() {
    const auto *part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
                                               ESP_PARTITION_SUBTYPE_ANY, "model");
    uint8_t header[1024];
    if (!part || part->size < sizeof(header) ||
        esp_partition_read(part, 0, header, sizeof(header)) != ESP_OK) return false;
    const auto read32 = [&header](size_t at) {
        uint32_t value;
        memcpy(&value, header + at, sizeof(value));
        return value;
    };
    const uint32_t count = read32(0);
    if (count == 0 || count > 4) return false;
    size_t pos = 4;
    for (uint32_t model = 0; model < count; ++model) {
        if (pos + 36 > sizeof(header) || !memchr(header + pos, 0, 32)) return false;
        const uint32_t files = read32(pos + 32);
        pos += 36;
        if (files == 0 || files > 8) return false;
        for (uint32_t file = 0; file < files; ++file) {
            if (pos + 40 > sizeof(header) || !memchr(header + pos, 0, 32)) return false;
            const uint32_t offset = read32(pos + 32), size = read32(pos + 36);
            if (size == 0 || offset < 4 || offset > part->size || size > part->size - offset)
                return false;
            pos += 40;
        }
    }
    return true;
}
} // namespace

struct SpeechFrontend::Impl {
    struct Frame { int16_t pcm[kFrameSamples]; size_t samples; };
    SpeechFrontendConfig config;
    srmodel_list_t *models = nullptr;
    const esp_afe_sr_iface_t *afe = nullptr;
    esp_afe_sr_data_t *afe_data = nullptr;
    esp_mn_iface_t *mn = nullptr;
    model_iface_data_t *mn_data = nullptr;
    bool commands_allocated = false;
    QueueHandle_t queue = nullptr;
    StaticQueue_t queue_control{};
    uint8_t *queue_storage = nullptr;
    std::atomic<bool> running{false}, feed_exited{true}, fetch_exited{true};
    std::atomic<uint32_t> dropped{0};
    std::atomic<int> input_rms_peak{0};

    ~Impl() {
        if (commands_allocated) esp_mn_commands_free();
        if (mn_data) mn->destroy(mn_data);
        if (afe_data) afe->destroy(afe_data);
        if (models) esp_srmodel_deinit(models);
        if (queue) vQueueDelete(queue);
        heap_caps_free(queue_storage);
    }

    static void FeedTask(void *arg) {
        auto *self = static_cast<Impl *>(arg);
        const size_t size = self->afe->get_feed_chunksize(self->afe_data);
        std::vector<int16_t> input(size);
        size_t used = 0;
        uint32_t last_dropped = 0;
        Frame frame;
        while (self->running.load()) {
            if (xQueueReceive(self->queue, &frame, pdMS_TO_TICKS(30)) != pdPASS) continue;
            const int rms = SignalRms(frame.pcm, frame.samples);
            int peak = self->input_rms_peak.load();
            while (rms > peak && !self->input_rms_peak.compare_exchange_weak(peak, rms)) {}
            const uint32_t dropped = self->dropped.load();
            if (dropped != last_dropped) {
                used = 0;
                last_dropped = dropped;
                ESP_LOGW(kTag, "AFE input overrun, dropped=%lu", (unsigned long)dropped);
            }
            for (size_t at = 0; at < frame.samples && self->running.load();) {
                const size_t take = std::min(size - used, frame.samples - at);
                memcpy(input.data() + used, frame.pcm + at, take * sizeof(int16_t));
                used += take;
                at += take;
                if (used == size) {
                    self->afe->feed(self->afe_data, input.data());
                    used = 0;
                }
            }
        }
        // Release C++ storage before deleting this task's PSRAM stack.
        std::vector<int16_t>().swap(input);
        self->feed_exited.store(true);
        vTaskDeleteWithCaps(nullptr);
    }

    static void FetchTask(void *arg) {
        auto *self = static_cast<Impl *>(arg);
        Frame output{};
        std::vector<int16_t> recognition(self->mn->get_samp_chunksize(self->mn_data));
        SpeechEndpoint endpoint;
        uint32_t generation = UINT32_MAX, last_dropped = 0;
        bool woke = false;
        bool recognition_had_speech = false;
        bool upload_paused = false;
        int64_t measured_at = esp_timer_get_time(), infer_total = 0, infer_max = 0;
        uint32_t infer_count = 0;
        uint32_t voiced_frames = 0;
        int signal_peak = 0;
        while (self->running.load() || !self->feed_exited.load()) {
            // Capture arrives in 60 ms bursts. A shorter timeout reports empty
            // buffers during normal operation even though no frame was lost.
            auto *result = self->afe->fetch_with_delay(self->afe_data, pdMS_TO_TICKS(100));
            if (!self->running.load()) continue; // Drain any in-flight feed before destruction.
            if (!result || result->ret_value == ESP_FAIL || !result->data || result->data_size <= 0)
                continue;
            const SpeechContext context = self->config.context();
            if (generation != context.generation || last_dropped != self->dropped.load()) {
                generation = context.generation;
                last_dropped = self->dropped.load();
                endpoint.Reset();
                self->mn->clean(self->mn_data);
                output.samples = 0;
                woke = false;
                recognition_had_speech = false;
                upload_paused = false;
            }
            const size_t samples = result->data_size / sizeof(int16_t);
            if (context.mode == SpeechMode::Disabled) continue;
            const int rms = SignalRms(result->data, samples);
            signal_peak = std::max(signal_peak, rms);
            if (result->vad_state == VAD_SPEECH) ++voiced_frames;

            if (context.mode == SpeechMode::Armed && !woke) {
                recognition_had_speech |= result->vad_state == VAD_SPEECH;
                for (size_t i = 0; i < samples; ++i) {
                    const int32_t amplified = static_cast<int32_t>(result->data[i]) *
                                              CONFIG_DOMOS_SR_LINEAR_GAIN_PERCENT / 100;
                    recognition[i] = static_cast<int16_t>(std::clamp<int32_t>(
                        amplified, INT16_MIN, INT16_MAX));
                }
                const int64_t started = esp_timer_get_time();
                const auto state = self->mn->detect(self->mn_data, recognition.data());
                const int64_t elapsed = esp_timer_get_time() - started;
                infer_total += elapsed;
                infer_max = std::max(infer_max, elapsed);
                ++infer_count;
                if (started - measured_at >= 10000000) {
                    ESP_LOGI(kTag, "MultiNet avg=%lld max=%lld us dropped=%lu speech=%lu/%lu rms_peak=%d input_rms=%d heap=%u",
                             infer_total / infer_count, infer_max, (unsigned long)self->dropped.load(),
                             (unsigned long)voiced_frames, (unsigned long)infer_count, signal_peak,
                             self->input_rms_peak.exchange(0),
                             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
                    measured_at = started;
                    infer_count = 0; infer_total = 0; infer_max = 0;
                    voiced_frames = 0; signal_peak = 0;
                }
                if (state == ESP_MN_STATE_DETECTED) {
                    const auto *found = self->mn->get_results(self->mn_data);
                    if (found && found->num > 0 && found->command_id[0] == 1 &&
                        found->prob[0] >= CONFIG_DOMOS_WAKE_THRESHOLD / 100.0f) {
                        woke = true;
                        ESP_LOGI(kTag, "Local Hey Dom detected (confidence=%.2f)", found->prob[0]);
                        self->config.on_wake(generation);
                    }
                    self->mn->clean(self->mn_data);
                    recognition_had_speech = false;
                } else if (state == ESP_MN_STATE_TIMEOUT) {
                    const auto *found = self->mn->get_results(self->mn_data);
                    if (found && found->num > 0) {
                        ESP_LOGI(kTag, "Wake candidate at timeout: id=%d confidence=%.2f",
                                 found->command_id[0], found->prob[0]);
                    }
                    if (found && recognition_had_speech) {
                        // MultiNet5 emits phonetic symbols, useful for checking
                        // accent mismatch. Bounded output; never dump PCM/model data.
                        ESP_LOGI(kTag, "Wake speech unmatched: candidates=%d phonemes=%.96s",
                                 found->num, found->raw_string);
                    }
                    self->mn->clean(self->mn_data);
                    recognition_had_speech = false;
                }
            }
            if (context.mode == SpeechMode::Listening && !upload_paused) {
                // Never remove silence or prepend vad_cache when streaming AFE
                // frames: that would alter wire cadence or duplicate speech.
                for (size_t at = 0; at < samples;) {
                    const size_t take = std::min(kFrameSamples - output.samples, samples - at);
                    memcpy(output.pcm + output.samples, result->data + at, take * sizeof(int16_t));
                    output.samples += take;
                    at += take;
                    if (output.samples == kFrameSamples) {
                        self->config.on_audio(output.pcm, output.samples);
                        output.samples = 0;
                    }
                }
            }
            if (context.mode == SpeechMode::Listening &&
                endpoint.Feed(result->vad_state == VAD_SPEECH, samples)) {
                ESP_LOGI(kTag, "VADNet: speech ended after 2 seconds of silence");
                // Freeze this utterance so the uplink can drain deterministically.
                // Any partial tail is silence and is intentionally discarded.
                upload_paused = true;
                output.samples = 0;
                self->config.on_speech_end(generation);
            }
            // If inference is catching up after a transient stall, let idle and
            // lower-priority UI/network work run between frames.
            vTaskDelay(pdMS_TO_TICKS(1));
        }
        std::vector<int16_t>().swap(recognition);
        self->fetch_exited.store(true);
        vTaskDeleteWithCaps(nullptr);
    }
};

bool SpeechFrontend::Start(const SpeechFrontendConfig &config) {
    if (impl_) return IsReady();
    if (!config.context || !config.on_audio || !config.on_wake || !config.on_speech_end ||
        !ModelPartitionValid()) {
        ESP_LOGW(kTag, "ESP-SR model partition missing/invalid; preserving cloud microphone path");
        return false;
    }
    impl_ = new (std::nothrow) Impl;
    if (!impl_) return false;
    auto &self = *impl_;
    self.config = config;
    const auto fail = [this]() { Stop(); return false; };
    self.models = esp_srmodel_init("model");
    if (!self.models) return fail();
    char *vad = esp_srmodel_filter(self.models, ESP_VADN_PREFIX, nullptr);
    char *mn = esp_srmodel_filter(self.models, "mn5q8", "en");
    if (!vad || !mn) { ESP_LOGE(kTag, "VADNet and mn5q8_en models are required"); return fail(); }
    auto *cfg = afe_config_init("M", self.models, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
    if (!cfg) return fail();
    cfg->aec_init = false; // No synchronized hardware playback-reference channel.
    cfg->se_init = false;  // Single microphone, not a microphone array.
#ifdef CONFIG_DOMOS_SR_NOISE_SUPPRESSION
    cfg->ns_init = true;
#else
    cfg->ns_init = false;
#endif
    cfg->afe_ns_mode = AFE_NS_MODE_WEBRTC;
    cfg->vad_init = true;
    cfg->vad_model_name = vad;
    cfg->vad_mode = VAD_MODE_0;
    cfg->vad_min_noise_ms = 64;
    cfg->vad_min_speech_ms = 128;
    cfg->wakenet_init = false; // Custom phrase uses MultiNet, not a renamed WakeNet.
    cfg->agc_init = false;
    cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    cfg->afe_perferred_core = 1;
    cfg->afe_perferred_priority = 5;
    self.afe = esp_afe_handle_from_config(cfg);
    if (self.afe) self.afe_data = self.afe->create_from_config(cfg);
    afe_config_free(cfg);
    if (!self.afe_data || self.afe->get_feed_channel_num(self.afe_data) != 1) return fail();

    self.mn = esp_mn_handle_from_name(mn);
    if (self.mn) self.mn_data = self.mn->create(mn, 3000);
    if (!self.mn_data) return fail();
    if (self.mn->get_samp_chunksize(self.mn_data) != self.afe->get_fetch_chunksize(self.afe_data)) {
        ESP_LOGE(kTag, "AFE/MultiNet frame sizes disagree");
        return fail();
    }
    self.mn->set_det_threshold(self.mn_data, CONFIG_DOMOS_WAKE_THRESHOLD / 100.0f);
    if (esp_mn_commands_alloc(self.mn, self.mn_data) != ESP_OK) return fail();
    self.commands_allocated = true;
    // MultiNet5 English requires phonemes, not the literal display string.
    // Accept both English and Vietnamese-style "Dom" and the shorter wake
    // MultiNet5 consumes compact English phonemes. Include the common
    // Vietnamese-accented vowels heard for "Dom" (AA, AH, AO and OW) plus
    // "hi" for speakers whose "hey" lands closer to HH AY. Every phrase maps
    // to the same wake action; the confidence threshold still rejects noise.
    static constexpr const char *kWakePhonemes[] = {
        "hd DnM", "hd DcM", "hd DeM", "hd DbM", "hd DnN",
        "hi DnM", "hi DcM", "hd", "DnM", "DcM", "DeM", "DbM",
    };
    for (const char *phonemes : kWakePhonemes) {
        if (esp_mn_commands_add(1, phonemes) != ESP_OK) return fail();
    }
    if (esp_mn_commands_update() != nullptr) return fail();
    self.mn->print_active_speech_commands(self.mn_data);
    esp_mn_commands_print();

    self.queue_storage = static_cast<uint8_t *>(heap_caps_malloc(
        kInputDepth * sizeof(Impl::Frame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!self.queue_storage) return fail();
    self.queue = xQueueCreateStatic(kInputDepth, sizeof(Impl::Frame), self.queue_storage, &self.queue_control);
    if (!self.queue) return fail();
    self.running.store(true);
    TaskHandle_t task;
    self.feed_exited.store(false);
    if (xTaskCreatePinnedToCoreWithCaps(Impl::FeedTask, "sr_feed", 6144, impl_, 5, &task, 1,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        self.feed_exited.store(true); return fail();
    }
    self.fetch_exited.store(false);
    if (xTaskCreatePinnedToCoreWithCaps(Impl::FetchTask, "sr_fetch", 8192, impl_, 5, &task, 0,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        self.fetch_exited.store(true); return fail();
    }
    ready_.store(true);
    self.afe->print_pipeline(self.afe_data);
    ESP_LOGI(kTag, "ESP-SR ready: VADNet + MultiNet wake phrases (threshold=%d%% gain=%d%%); AEC off, Vietnamese TTS on gateway",
             CONFIG_DOMOS_WAKE_THRESHOLD, CONFIG_DOMOS_SR_LINEAR_GAIN_PERCENT);
    return true;
}

bool SpeechFrontend::Stop() {
    ready_.store(false);
    if (!impl_) return true;
    impl_->running.store(false);
    for (int i = 0; i < 200 && (!impl_->feed_exited.load() || !impl_->fetch_exited.load()); ++i)
        vTaskDelay(pdMS_TO_TICKS(10));
    if (!impl_->feed_exited.load() || !impl_->fetch_exited.load()) {
        ESP_LOGE(kTag, "AFE shutdown timed out; retaining resources for worker safety");
        return false;
    }
    delete impl_;
    impl_ = nullptr;
    return true;
}

bool SpeechFrontend::Submit(const int16_t *pcm, size_t samples) {
    if (!IsReady() || !pcm || samples == 0 || samples > kFrameSamples) return false;
    Impl::Frame frame;
    frame.samples = samples;
    memcpy(frame.pcm, pcm, samples * sizeof(int16_t));
    if (xQueueSend(impl_->queue, &frame, 0) == pdPASS) return true;
    impl_->dropped.fetch_add(1);
    return false;
}
#else
bool SpeechFrontend::Start(const SpeechFrontendConfig &) { return false; }
bool SpeechFrontend::Stop() { return true; }
bool SpeechFrontend::Submit(const int16_t *, size_t) { return false; }
#endif

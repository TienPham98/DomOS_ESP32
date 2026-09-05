#include "app/launcher/app_manager.h"
#include "board/es3c28p/board_es3c28p.h"
#include "esp_log.h"
#include "kernel/event_bus.h"
#include "kernel/storage_manager.h"
#include "kernel/task_manager.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "services/assistant/assistant_service.h"
#include "services/filesystem/upload_server.h"
#include "services/media/media_service.h"
#include "services/mqtt/mqtt_service.h"
#include "services/ota/ota_service.h"
#include "services/wifi/wifi_service.h"
#include "nvs_flash.h"
#include <cstring>

namespace {
void EventDispatcher(void *context)
{
    auto *events = static_cast<EventBus *>(context);
    while (true) {
        events->DispatchPending();
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

struct DeferredNetworkContext {
    WifiService *wifi;
    MqttService *mqtt;
    EventBus *events;
    AssistantService *assistant;
};

void StartNetworkClientsWhenWifiReady(void *context)
{
    auto *services = static_cast<DeferredNetworkContext *>(context);
    while (!services->wifi->Connected()) {
        vTaskDelay(pdMS_TO_TICKS(100));
    }

    if (!services->mqtt->Start(services->events)) {
        ESP_LOGW("domos", "MQTT service did not start");
    }
    // Voice belongs to the device, not the Assistant screen. WsClient handles
    // reconnects after Wi-Fi/server outages without opening another session.
    if (services->assistant != nullptr) services->assistant->OpenAudioChannel();
    vTaskDelete(nullptr);
}
} // namespace

extern "C" void app_main(void)
{
    // Initialize NVS (required for WiFi + config storage)
    esp_err_t nvs_ret = nvs_flash_init();
    if (nvs_ret == ESP_ERR_NVS_NO_FREE_PAGES || nvs_ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    static ES3C28PBoard board;
    static EventBus     events;
    static TaskManager  tasks;
    static StorageManager storage;
    static WifiService  wifi;
    static MqttService  mqtt;
    static OtaService   ota;
    static MediaService media;
    static WallpaperUploadServer upload;
    static AppManager   apps;
    static AssistantService assistant;

    if (!board.Init()) {
        ESP_LOGE("domos", "board initialisation failed");
        return;
    }
    if (!events.Init() || !storage.EnsureLayout()) {
        ESP_LOGE("domos", "kernel initialisation failed");
        return;
    }

    if (!wifi.Start()) ESP_LOGW("domos", "Wi-Fi service did not start");
    ota.Start(&events);
    media.Start(&events);
    wifi.SetUploadServer(&upload);
    if (!upload.Start(&wifi, &apps)) ESP_LOGW("domos", "wallpaper upload server did not start");

    // ── AI Assistant ──────────────────────────────────────────────────────
    AssistantConfig asst_cfg = {};
#ifdef CONFIG_DOMOS_AI_WS_URI
    asst_cfg.ws_uri = CONFIG_DOMOS_AI_WS_URI;
#else
    asst_cfg.ws_uri = "";
#endif
    asst_cfg.auth_token = nullptr;  // auto-filled from MAC
#ifdef CONFIG_DOMOS_AI_AUTH_TOKEN
    asst_cfg.auth_token = (strlen(CONFIG_DOMOS_AI_AUTH_TOKEN) > 0) ? CONFIG_DOMOS_AI_AUTH_TOKEN : nullptr;
#endif
    asst_cfg.device_id  = nullptr;  // auto-filled from MAC
    asst_cfg.client_id  = "domos-es3c28p-001";

    const bool assistant_ready = assistant.Start(&board, &events, asst_cfg);
    if (!assistant_ready) {
        ESP_LOGW("domos", "AssistantService did not start");
    }
    if (!apps.Start(&board, &wifi, &mqtt, &ota, &media, &storage, &assistant)) {
        ESP_LOGE("domos", "AppManager did not start");
        return;
    }
    assistant.SetAppManager(&apps);
    events.Subscribe(EventType::AppLaunchRequested, [](const DomosEvent &event, void *context) {
        static_cast<AppManager *>(context)->RequestLaunch(event.data);
    }, &apps);
    tasks.Start("event_bus", EventDispatcher, &events, 4096, 4);

    // Starting a TCP client at boot would let lwIP transmit while the Wi-Fi
    // link is still negotiating either its fixed DomOS IP or a DHCP lease.
    // Defer network clients until the station is fully associated and IP_EVENT_GOT_IP has
    // been received. This also guarantees AppManager has installed its message
    // handler before the broker can deliver commands.
    static DeferredNetworkContext network_context{&wifi, &mqtt, &events, assistant_ready ? &assistant : nullptr};
    if (!tasks.Start("network_start", StartNetworkClientsWhenWifiReady, &network_context, 4096, 3)) {
        ESP_LOGW("domos", "Network deferred-start task did not start");
    }

    board.StartLvglTask();
}

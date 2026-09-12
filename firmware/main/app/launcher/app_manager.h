#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

class ES3C28PBoard;
class WifiService;
class MqttService;
class OtaService;
class MediaService;
class StorageManager;
class AssistantService;
class IApp;

class AppManager {
public:
    bool Start(ES3C28PBoard *board, WifiService *wifi, MqttService *mqtt, OtaService *ota,
               MediaService *media, StorageManager *storage, AssistantService *assistant = nullptr);
    void Launch(const std::string &app);
    void RequestLaunch(const std::string &app);
    bool RequestClockSettings(const std::string &style, uint32_t color_hex,
                              const std::string &mode);
    bool RequestWallpaperUrl(const std::string &url, const std::string &name = "");
    bool RequestWallpaperSync();
    // Persist and display app data received over the assistant WebSocket.
    // Safe to call from the WebSocket client task.
    void UpdateTrackingData(const char *resource, const char *json, bool ok);
    void CloseCurrent();
    void ShowNextHomePage();
    void ApplyClockSettings(const std::string &style, uint32_t color_hex, const std::string &mode = "");
    void GetClockSettings(std::string &style, uint32_t &color_hex, std::string &mode);
    void SetWallpaperUrl(const std::string &url, const std::string &name = "");
    void GetWallpaperUrl(std::string &url, std::string &name);
    void StartSlideshow(int interval_sec = 30);
    void StopSlideshow();
    bool IsSlideshowActive() const;
    void SetSlideshowInterval(int interval_sec);
    int GetSlideshowInterval() const;
    void TriggerNextSlide();
    void TriggerPrevSlide();
    void SyncWallpapersWithServer();



    ES3C28PBoard *Board() const { return board_; }
    WifiService *Wifi() const { return wifi_; }
    MqttService *Mqtt() const { return mqtt_; }
    OtaService *Ota() const { return ota_; }
    MediaService *Media() const { return media_; }
    StorageManager *Storage() const { return storage_; }
    AssistantService *Assistant() const { return assistant_; }

private:
    bool Register(IApp *app);

    struct AppEntry {
        IApp *app = nullptr;
        bool created = false;
    };
    static constexpr size_t kMaxApps = 16;
    AppEntry apps_[kMaxApps]{};
    size_t app_count_ = 0;
    IApp *current_ = nullptr;
    void *launch_queue_ = nullptr;
    ES3C28PBoard *board_ = nullptr;
    WifiService *wifi_ = nullptr;
    MqttService *mqtt_ = nullptr;
    OtaService *ota_ = nullptr;
    MediaService *media_ = nullptr;
    StorageManager *storage_ = nullptr;
    AssistantService *assistant_ = nullptr;
};

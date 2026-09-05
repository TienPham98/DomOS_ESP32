#pragma once

#include <cstdint>

class WifiService {
public:
    bool Start();
    bool ConnectTo(const char *ssid, const char *password);
    bool Connected() const;
    bool ConnectionFailed() const;
    int LastDisconnectReason() const;
    const char *Ssid() const;
    const char *IpAddress() const;
    int8_t Rssi() const;
    void SetUploadServer(class WallpaperUploadServer *upload);
};


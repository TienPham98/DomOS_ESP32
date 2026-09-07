#pragma once

#include <atomic>
#include <cstdint>

#include "board/es3c28p/es8311.h"
#include "esp_err.h"
#include "esp_lcd_panel_ops.h"
#include "driver/i2c_master.h"

struct TouchPoint {
    bool pressed;
    uint16_t x;
    uint16_t y;
};

class ES3C28PBoard {
public:
    using MenuSwipeHandler = void (*)(void *context);

    bool Init();
    bool InitDisplay();
    bool InitTouch();
    bool InitAudio();
    bool InitStorage();
    bool InitLvgl();
    void StartLvglTask();

    void SetBrightness(uint8_t percent);
    uint8_t GetBrightness() const { return brightness_.load(); }
    bool ReadTouch(TouchPoint *point);
    void SetMenuSwipeHandler(MenuSwipeHandler handler, void *context);
    esp_lcd_panel_handle_t Panel() const { return panel_; }

    // Audio
    void SetPAEnabled(bool enabled);
    esp_err_t I2S_ReadPCM(int16_t *buf, size_t samples);
    esp_err_t I2S_WritePCM(const int16_t *buf, size_t samples);
    ES8311Codec *GetCodec();

private:
    esp_lcd_panel_handle_t panel_ = nullptr;
    i2c_master_bus_handle_t i2c_bus_ = nullptr;
    i2c_master_dev_handle_t touch_device_ = nullptr;
    std::atomic<uint8_t> brightness_{75};
    bool touch_ready_ = false;
    bool audio_ready_ = false;
};

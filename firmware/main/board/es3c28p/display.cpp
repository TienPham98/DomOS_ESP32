#include "board_es3c28p.h"

#include <cstdlib>

#include "board_config.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_ili9341.h"
#include "esp_lcd_io_spi.h"
#include "esp_lcd_panel_io.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"

namespace {
constexpr char TAG[] = "display";
constexpr spi_host_device_t LCD_HOST = SPI2_HOST;
constexpr size_t DRAW_LINES = 20; // 20 lines per buffer (optimal balance of DMA speed and internal SRAM)
constexpr uint16_t MENU_SWIPE_START_Y = 200;
constexpr uint16_t MENU_SWIPE_END_Y = 120;
constexpr uint16_t MENU_SWIPE_MIN_TRAVEL = 80;

lv_disp_drv_t s_display_driver;
lv_indev_drv_t s_touch_driver;
lv_color_t *s_draw_buffer_1 = nullptr;
lv_color_t *s_draw_buffer_2 = nullptr;
lv_disp_draw_buf_t s_draw_buffer_desc;
ES3C28PBoard *s_board = nullptr;
ES3C28PBoard::MenuSwipeHandler s_menu_swipe_handler = nullptr;
void *s_menu_swipe_context = nullptr;

struct MenuSwipeState {
    bool active = false;
    bool eligible = false;
    uint16_t start_x = 0;
    uint16_t start_y = 0;
    uint16_t last_x = 0;
    uint16_t last_y = 0;
};

MenuSwipeState s_menu_swipe;


bool Check(esp_err_t err, const char *operation)
{
    if (err == ESP_OK) return true;
    ESP_LOGE(TAG, "%s failed: %s", operation, esp_err_to_name(err));
    return false;
}

bool FlushReady(esp_lcd_panel_io_handle_t, esp_lcd_panel_io_event_data_t *, void *user_ctx)
{
    lv_disp_flush_ready(static_cast<lv_disp_drv_t *>(user_ctx));
    return false;
}

void Flush(lv_disp_drv_t *driver, const lv_area_t *area, lv_color_t *pixels)
{
    if (esp_lcd_panel_draw_bitmap(s_board->Panel(), area->x1, area->y1,
                                  area->x2 + 1, area->y2 + 1, pixels) != ESP_OK) {
        // Avoid locking LVGL forever if a transfer cannot be queued.
        lv_disp_flush_ready(driver);
    }
}

void ReadLvglTouch(lv_indev_drv_t *, lv_indev_data_t *data)
{
    TouchPoint point{};
    if (s_board->ReadTouch(&point) && point.pressed) {
        if (!s_menu_swipe.active) {
            s_menu_swipe.active = true;
            s_menu_swipe.eligible = point.y >= MENU_SWIPE_START_Y;
            s_menu_swipe.start_x = point.x;
            s_menu_swipe.start_y = point.y;
        }
        s_menu_swipe.last_x = point.x;
        s_menu_swipe.last_y = point.y;
        data->point.x = point.x;
        data->point.y = point.y;
        data->state = LV_INDEV_STATE_PR;
    } else {
        if (s_menu_swipe.active && s_menu_swipe.eligible && s_menu_swipe_handler != nullptr) {
            const int rise = static_cast<int>(s_menu_swipe.start_y) - s_menu_swipe.last_y;
            const int horizontal = std::abs(static_cast<int>(s_menu_swipe.start_x) - s_menu_swipe.last_x);
            if (s_menu_swipe.last_y <= MENU_SWIPE_END_Y &&
                rise >= MENU_SWIPE_MIN_TRAVEL && horizontal <= rise) {
                ESP_LOGI(TAG, "Bottom swipe detected: rise=%dpx, horizontal=%dpx", rise, horizontal);
                s_menu_swipe_handler(s_menu_swipe_context);
            }
        }
        s_menu_swipe = {};
        data->state = LV_INDEV_STATE_REL;
    }
}

void LvglTick(void *)
{
    lv_tick_inc(2);
}

void LvglTask(void *)
{
    ESP_LOGI(TAG, "LvglTask started");
    while (true) {
        lv_timer_handler();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

} // namespace

void ES3C28PBoard::SetMenuSwipeHandler(MenuSwipeHandler handler, void *context)
{
    s_menu_swipe_handler = handler;
    s_menu_swipe_context = context;
    s_menu_swipe = {};
}

bool ES3C28PBoard::InitDisplay()
{
    ledc_timer_config_t timer{};
    timer.speed_mode = LEDC_LOW_SPEED_MODE;
    timer.duty_resolution = LEDC_TIMER_10_BIT;
    timer.timer_num = LEDC_TIMER_0;
    timer.freq_hz = 5000;
    timer.clk_cfg = LEDC_AUTO_CLK;

    ledc_channel_config_t channel{};
    channel.gpio_num = TFT_BL;
    channel.speed_mode = LEDC_LOW_SPEED_MODE;
    channel.channel = LEDC_CHANNEL_0;
    channel.timer_sel = LEDC_TIMER_0;
    channel.duty = 0;
    channel.hpoint = 0;
    if (!Check(ledc_timer_config(&timer), "backlight timer") ||
        !Check(ledc_channel_config(&channel), "backlight channel")) return false;

    spi_bus_config_t bus_config{};
    bus_config.mosi_io_num = TFT_MOSI;
    bus_config.miso_io_num = TFT_MISO;
    bus_config.sclk_io_num = TFT_SCLK;
    bus_config.quadwp_io_num = -1;
    bus_config.quadhd_io_num = -1;
    bus_config.max_transfer_sz = TFT_WIDTH * DRAW_LINES * sizeof(uint16_t);
    if (!Check(spi_bus_initialize(LCD_HOST, &bus_config, SPI_DMA_CH_AUTO), "LCD SPI bus")) return false;

    esp_lcd_panel_io_handle_t io_handle = nullptr;
    esp_lcd_panel_io_spi_config_t io_config{};
    io_config.cs_gpio_num = TFT_CS;
    io_config.dc_gpio_num = TFT_DC;
    io_config.spi_mode = 0;
    io_config.pclk_hz = 40 * 1000 * 1000;
    io_config.trans_queue_depth = 10;
    io_config.lcd_cmd_bits = 8;
    io_config.lcd_param_bits = 8;
    if (!Check(esp_lcd_new_panel_io_spi(LCD_HOST, &io_config, &io_handle), "LCD SPI I/O")) return false;
    const esp_lcd_panel_io_callbacks_t io_callbacks = {
        .on_color_trans_done = FlushReady,
    };
    if (!Check(esp_lcd_panel_io_register_event_callbacks(io_handle, &io_callbacks, &s_display_driver),
               "LCD transfer callback")) return false;

    esp_lcd_panel_dev_config_t panel_config{};
    panel_config.reset_gpio_num = static_cast<gpio_num_t>(-1); // LCD reset is shared with the ESP32-S3 EN pin.
    panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR;
    panel_config.bits_per_pixel = 16;
    if (!Check(esp_lcd_new_panel_ili9341(io_handle, &panel_config, &panel_), "ILI9341 panel") ||
        !Check(esp_lcd_panel_reset(panel_), "ILI9341 reset") ||
        !Check(esp_lcd_panel_init(panel_), "ILI9341 init")) return false;

    // DomOS uses a 320x240 landscape canvas on the physical 240x320 panel.
    esp_lcd_panel_invert_color(panel_, true);
    esp_lcd_panel_swap_xy(panel_, true);

    esp_lcd_panel_mirror(panel_, false, false);
    if (!Check(esp_lcd_panel_disp_on_off(panel_, true), "ILI9341 display on")) return false;

    SetBrightness(75);
    ESP_LOGI(TAG, "ILI9341 ready; brightness test values: 0, 25, 50, 75, 100%%");
    return true;
}

void ES3C28PBoard::SetBrightness(uint8_t percent)
{
    if (percent > 100) percent = 100;
    brightness_.store(percent);
    const uint32_t duty = (static_cast<uint32_t>(percent) * 1023U + 50U) / 100U;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}

bool ES3C28PBoard::InitLvgl()
{
    s_board = this;
    lv_init();

    size_t lines = DRAW_LINES;
    size_t buf_size = TFT_WIDTH * lines * sizeof(lv_color_t);
    s_draw_buffer_1 = static_cast<lv_color_t *>(heap_caps_malloc(buf_size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
    if (s_draw_buffer_1 == nullptr) {
        lines = 30;
        buf_size = TFT_WIDTH * lines * sizeof(lv_color_t);
        s_draw_buffer_1 = static_cast<lv_color_t *>(heap_caps_malloc(buf_size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
    }
    if (s_draw_buffer_1 == nullptr) {
        lines = 20;
        buf_size = TFT_WIDTH * lines * sizeof(lv_color_t);
        s_draw_buffer_1 = static_cast<lv_color_t *>(heap_caps_malloc(buf_size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
    }
    if (s_draw_buffer_1 == nullptr) {
        lines = 10;
        buf_size = TFT_WIDTH * lines * sizeof(lv_color_t);
        s_draw_buffer_1 = static_cast<lv_color_t *>(heap_caps_malloc(buf_size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
    }
    if (s_draw_buffer_1 == nullptr) {
        ESP_LOGE(TAG, "unable to allocate LVGL DMA buffer");
        return false;
    }
    s_draw_buffer_2 = static_cast<lv_color_t *>(heap_caps_malloc(buf_size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
    if (s_draw_buffer_2 == nullptr) {
        ESP_LOGW(TAG, "LVGL DMA buffer 2 allocation failed, using single buffer (%u lines)", lines);
    } else {
        ESP_LOGI(TAG, "LVGL double DMA buffers allocated (%u lines, %u bytes each)", lines, buf_size);
    }

    lv_disp_draw_buf_init(&s_draw_buffer_desc, s_draw_buffer_1, s_draw_buffer_2, TFT_WIDTH * lines);

    lv_disp_drv_init(&s_display_driver);
    s_display_driver.hor_res = TFT_WIDTH;
    s_display_driver.ver_res = TFT_HEIGHT;
    s_display_driver.flush_cb = Flush;
    s_display_driver.draw_buf = &s_draw_buffer_desc;
    lv_disp_drv_register(&s_display_driver);

    lv_indev_drv_init(&s_touch_driver);
    s_touch_driver.type = LV_INDEV_TYPE_POINTER;
    s_touch_driver.read_cb = ReadLvglTouch;
    lv_indev_drv_register(&s_touch_driver);

    esp_timer_create_args_t tick_timer_config{};
    tick_timer_config.callback = LvglTick;
    tick_timer_config.name = "lvgl_tick";
    esp_timer_handle_t tick_timer = nullptr;
    if (!Check(esp_timer_create(&tick_timer_config, &tick_timer), "LVGL tick timer") ||
        !Check(esp_timer_start_periodic(tick_timer, 2000), "LVGL tick timer start")) return false;
    return true;
}

void ES3C28PBoard::StartLvglTask()
{
    xTaskCreatePinnedToCore(LvglTask, "lvgl", 8192, nullptr, 6, nullptr, 0);
}

#define LV_USE_SJPG 1
#include "app_manager.h"
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <dirent.h>
#include <sys/stat.h>

#include "app/i_app.h"
#include "board/es3c28p/board_es3c28p.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "cJSON.h"
#include "lvgl.h"
#include "extra/libs/sjpg/tjpgd.h"
#include "services/assistant/assistant_service.h"
#include "services/media/media_service.h"
#include "services/mqtt/mqtt_service.h"
#include "services/ota/ota_service.h"
#include "services/wifi/wifi_service.h"
#include "services/filesystem/upload_server.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <vector>


namespace {

enum class AppRequestType : uint8_t {
    Launch,
    ClockSettings,
};

struct AppLaunchRequest {
    AppRequestType type = AppRequestType::Launch;
    char app[24];
    char style[16];
    char mode[8];
    uint32_t color = 0;
};

struct PendingWallpaperCommand {
    AppManager *manager;
    bool sync;
    std::string url;
    std::string name;
};

// Voice remains resident. Only one widget HTTP/LittleFS worker may consume
// another internal-RAM stack at a time; the other app retries on its UI timer.
std::atomic<bool> s_widget_fetch_busy{false};
std::atomic<bool> s_wallpaper_command_busy{false};
// HTTPS certificate verification overflows the old 4 KiB HTTP-only stack.
// Widget downloads are serialized, so only one such internal stack is live.
constexpr uint32_t kWidgetFetchStackBytes = 8192;

void ProcessWallpaperCommand(void *context)
{
    auto *command = static_cast<PendingWallpaperCommand *>(context);
    if (command != nullptr) {
        if (command->sync) command->manager->SyncWallpapersWithServer();
        else command->manager->SetWallpaperUrl(command->url, command->name);
        delete command;
    }
    s_wallpaper_command_busy = false;
    vTaskDelete(nullptr);
}

struct PendingMqttCommand {
    AppManager *manager;
    std::string topic;
    std::string payload;
};

void ProcessMqttCommand(void *context)
{
    auto *command = static_cast<PendingMqttCommand *>(context);
    if (command == nullptr) return;

    cJSON *root = cJSON_Parse(command->payload.c_str());
    if (root != nullptr) {
        const cJSON *action = cJSON_GetObjectItemCaseSensitive(root, "action");
        const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
        const cJSON *state = cJSON_GetObjectItemCaseSensitive(root, "state");

        if (cJSON_IsString(action) && strcmp(action->valuestring, "sync_wallpapers") == 0) {
            command->manager->SyncWallpapersWithServer();
        } else if (cJSON_IsString(type) && strcmp(type->valuestring, "light") == 0 &&
                   cJSON_IsString(state)) {
            char response[128];
            snprintf(response, sizeof(response),
                     "{\"online\":true,\"state\":{\"light\":\"%s\"}}",
                     strcmp(state->valuestring, "on") == 0 ? "on" : "off");
            command->manager->Mqtt()->Publish("domos/device/es3c28p-01/state", response);
            AddSystemLog("INFO", "mqtt", "Smart-home light command: %s", state->valuestring);
        }
        cJSON_Delete(root);
    } else {
        ESP_LOGW("apps", "Ignoring invalid MQTT JSON on %s", command->topic.c_str());
    }
    delete command;
}

static std::string VietnameseToAscii(const std::string &src)
{
    std::string out;
    out.reserve(src.size());
    for (size_t i = 0; i < src.size(); ) {
        unsigned char c = static_cast<unsigned char>(src[i]);
        if (c < 0x80) {
            out.push_back(static_cast<char>(c));
            i++;
        } else if (c == 0xC3) {
            if (i + 1 < src.size()) {
                unsigned char c2 = static_cast<unsigned char>(src[i + 1]);
                switch (c2) {
                    case 0x80: case 0x81: case 0x82: case 0x83: case 0x84: case 0x85: out.push_back('A'); break;
                    case 0xA0: case 0xA1: case 0xA2: case 0xA3: case 0xA4: case 0xA5: out.push_back('a'); break;
                    case 0x88: case 0x89: case 0x8A: case 0x8B: out.push_back('E'); break;
                    case 0xA8: case 0xA9: case 0xAA: case 0xAB: out.push_back('e'); break;
                    case 0x8C: case 0x8D: case 0x8E: case 0x8F: out.push_back('I'); break;
                    case 0xAC: case 0xAD: case 0xAE: case 0xAF: out.push_back('i'); break;
                    case 0x92: case 0x93: case 0x94: case 0x95: case 0x96: out.push_back('O'); break;
                    case 0xB2: case 0xB3: case 0xB4: case 0xB5: case 0xB6: out.push_back('o'); break;
                    case 0x99: case 0x9A: case 0x9B: case 0x9C: out.push_back('U'); break;
                    case 0xB9: case 0xBA: case 0xBB: case 0xBC: out.push_back('u'); break;
                    case 0x9D: out.push_back('Y'); break;
                    case 0xBD: case 0xBF: out.push_back('y'); break;
                    case 0x90: out.push_back('D'); break;
                    default: break;
                }
                i += 2;
            } else { i++; }
        } else if (c == 0xC4 || c == 0xC5) {
            if (i + 1 < src.size()) {
                unsigned char c2 = static_cast<unsigned char>(src[i + 1]);
                if (c == 0xC4) {
                    if (c2 == 0x82 || c2 == 0x84) out.push_back('A');
                    else if (c2 == 0x83 || c2 == 0x85) out.push_back('a');
                    else if (c2 == 0x90) out.push_back('D');
                    else if (c2 == 0x91) out.push_back('d');
                    else if (c2 == 0x92) out.push_back('E');
                    else if (c2 == 0x93) out.push_back('e');
                    else if (c2 == 0xA8 || c2 == 0xAA) out.push_back('I');
                    else if (c2 == 0xA9 || c2 == 0xAB) out.push_back('i');
                } else if (c == 0xC5) {
                    if (c2 == 0xA8 || c2 == 0xAA || c2 == 0xAC) out.push_back('U');
                    else if (c2 == 0xA9 || c2 == 0xAB || c2 == 0xAD) out.push_back('u');
                }
                i += 2;
            } else { i++; }
        } else if (c == 0xE1) {
            if (i + 2 < src.size()) {
                unsigned char c2 = static_cast<unsigned char>(src[i + 1]);
                unsigned char c3 = static_cast<unsigned char>(src[i + 2]);
                if (c2 == 0xBA) {
                    if (c3 <= 0x93) out.push_back((c3 % 2 == 0) ? 'A' : 'a');
                    else if (c3 <= 0xB9) out.push_back((c3 % 2 == 0) ? 'E' : 'e');
                    else if (c3 <= 0xBF) out.push_back((c3 % 2 == 0) ? 'I' : 'i');
                } else if (c2 == 0xBB) {
                    if (c3 <= 0xB1) out.push_back((c3 % 2 == 0) ? 'O' : 'o');
                    else if (c3 <= 0xC7) out.push_back((c3 % 2 == 0) ? 'U' : 'u');
                    else if (c3 <= 0xD9) out.push_back((c3 % 2 == 0) ? 'Y' : 'y');
                }
                i += 3;
            } else { i++; }
        } else {
            i++;
        }
    }
    return out;
}

lv_obj_t *Label(lv_obj_t *parent, const char *text, lv_align_t alignment, int x, int y, const lv_font_t *font = nullptr)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_label_set_text(label, text);
    if (font != nullptr) lv_obj_set_style_text_font(label, font, 0);
    lv_obj_align(label, alignment, x, y);
    return label;
}

lv_obj_t *Button(lv_obj_t *parent, const char *text, lv_align_t alignment, int x, int y, lv_event_cb_t callback,
                 void *context)
{
    lv_obj_t *button = lv_btn_create(parent);
    lv_obj_set_size(button, 138, 48);
    lv_obj_align(button, alignment, x, y);
    lv_obj_set_style_bg_color(button, lv_color_hex(0x2196F3), 0);
    lv_obj_set_style_bg_grad_color(button, lv_color_hex(0x1976D2), 0);
    lv_obj_set_style_bg_grad_dir(button, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_radius(button, 12, 0);
    lv_obj_set_style_shadow_width(button, 8, 0);
    lv_obj_set_style_shadow_opa(button, LV_OPA_30, 0);
    lv_obj_add_event_cb(button, callback, LV_EVENT_CLICKED, context);
    Label(button, text, LV_ALIGN_CENTER, 0, 0);
    return button;
}

void ToWallpaper(lv_event_t *event) { static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("wallpaper"); }
void ToAssistant(lv_event_t *event) { static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("assistant"); }
void ToSmartHome(lv_event_t *event) { static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("smart-home"); }
void ToSettings(lv_event_t *event) { static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("settings"); }
void ToOta(lv_event_t *event) { static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("ota"); }
void ToManUtd(lv_event_t *event) { static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("man-utd"); }
void ToCodexCredit(lv_event_t *event) { static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("codex-credit"); }

class ScreenApp : public IApp {
public:
    explicit ScreenApp(AppManager &manager) : manager_(manager) {}
    void Destroy() override
    {
        if (screen_ != nullptr) lv_obj_del(screen_);
        screen_ = nullptr;
    }
    void Show() override { lv_scr_load(screen_); }
    void Hide() override {}

protected:
    void AddBackButton()
    {
        lv_obj_t *top_btn = lv_btn_create(screen_);
        lv_obj_set_size(top_btn, 48, 24);
        lv_obj_align(top_btn, LV_ALIGN_BOTTOM_LEFT, 6, -6);
        lv_obj_set_style_bg_color(top_btn, lv_color_hex(0x37474F), 0);
        lv_obj_set_style_bg_grad_color(top_btn, lv_color_hex(0x263238), 0);
        lv_obj_set_style_bg_grad_dir(top_btn, LV_GRAD_DIR_VER, 0);
        lv_obj_set_style_radius(top_btn, 6, 0);
        lv_obj_set_style_shadow_width(top_btn, 0, 0);
        lv_obj_set_style_pad_all(top_btn, 0, 0);
        lv_obj_add_event_cb(top_btn, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->CloseCurrent();
        }, LV_EVENT_CLICKED, &manager_);
        lv_obj_t *lbl = lv_label_create(top_btn);
        lv_label_set_text(lbl, LV_SYMBOL_LEFT);
        lv_obj_set_style_text_font(lbl, &lv_font_montserrat_14, 0);
        lv_obj_align(lbl, LV_ALIGN_CENTER, 0, 0);
    }
    AppManager &manager_;
    lv_obj_t *screen_ = nullptr;
};

class LauncherApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "launcher"; }
    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        lv_obj_set_style_bg_color(screen_, lv_color_hex(0x121212), 0);
        Label(screen_, "DomOS", LV_ALIGN_TOP_LEFT, 16, 12);
        Label(screen_, "Home", LV_ALIGN_TOP_RIGHT, -16, 14);
        lv_obj_t *buttons[] = {
            Button(screen_, "Wallpaper", LV_ALIGN_CENTER, -75, -43, ToWallpaper, &manager_),
            Button(screen_, "AI", LV_ALIGN_CENTER, 75, -43, ToAssistant, &manager_),
            Button(screen_, "Smart Home", LV_ALIGN_CENTER, -75, 3, ToSmartHome, &manager_),
            Button(screen_, "Settings", LV_ALIGN_CENTER, 75, 3, ToSettings, &manager_),
            Button(screen_, "Man Utd", LV_ALIGN_CENTER, -75, 49, ToManUtd, &manager_),
            Button(screen_, "Codex", LV_ALIGN_CENTER, 75, 49, ToCodexCredit, &manager_),
        };
        for (lv_obj_t *button : buttons) lv_obj_set_size(button, 136, 38);
        lv_obj_add_event_cb(screen_, [](lv_event_t *event) {
            lv_indev_t *indev = lv_indev_get_act();
            if (indev != nullptr && lv_indev_get_gesture_dir(indev) == LV_DIR_LEFT) {
                static_cast<AppManager *>(lv_event_get_user_data(event))->ShowNextHomePage();
            }
        }, LV_EVENT_GESTURE, &manager_);
        AddBackButton();

    }
};

namespace {
const char* NumberToWord(int n) {
    static const char* words[] = {
        "ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
        "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN", "SEVENTEEN", "EIGHTEEN", "NINETEEN",
        "TWENTY", "TWENTY-ONE", "TWENTY-TWO", "TWENTY-THREE", "TWENTY-FOUR", "TWENTY-FIVE", "TWENTY-SIX", "TWENTY-SEVEN", "TWENTY-EIGHT", "TWENTY-NINE",
        "THIRTY", "THIRTY-ONE", "THIRTY-TWO", "THIRTY-THREE", "THIRTY-FOUR", "THIRTY-FIVE", "THIRTY-SIX", "THIRTY-SEVEN", "THIRTY-EIGHT", "THIRTY-NINE",
        "FORTY", "FORTY-ONE", "FORTY-TWO", "FORTY-THREE", "FORTY-FOUR", "FORTY-FIVE", "FORTY-SIX", "FORTY-SEVEN", "FORTY-EIGHT", "FORTY-NINE",
        "FIFTY", "FIFTY-ONE", "FIFTY-TWO", "FIFTY-THREE", "FIFTY-FOUR", "FIFTY-FIVE", "FIFTY-SIX", "FIFTY-SEVEN", "FIFTY-EIGHT", "FIFTY-NINE"
    };
    if (n >= 0 && n < 60) return words[n];
    return "";
}
} // namespace

class ClockApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "clock"; }
    void Create() override
    {
        LoadNvs();
        const bool is_light = (mode_ == "light");
        const lv_color_t bg_col = is_light ? lv_color_hex(0xF1F5F9) : lv_color_hex(0x000000);
        const lv_color_t sub_col = is_light ? lv_color_hex(0x475569) : lv_color_hex(0x94a3b8);

        screen_ = lv_obj_create(nullptr);
        lv_obj_set_style_bg_color(screen_, bg_col, 0);
        lv_obj_clear_flag(screen_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scrollbar_mode(screen_, LV_SCROLLBAR_MODE_OFF);
        Label(screen_, "DomOS", LV_ALIGN_TOP_RIGHT, -12, 10);

        // 1. Digital / Minimal Text Labels
        time_ = Label(screen_, "--:--", LV_ALIGN_CENTER, 0, -32);
        lv_obj_set_style_text_font(time_, &lv_font_montserrat_48, 0);
        lv_obj_set_style_text_color(time_, color_, 0);

        separator_ = lv_obj_create(screen_);
        lv_obj_set_size(separator_, 48, 2);
        lv_obj_align(separator_, LV_ALIGN_CENTER, 0, 8);
        lv_obj_set_style_bg_color(separator_, color_, 0);
        lv_obj_set_style_border_width(separator_, 0, 0);

        date_ = Label(screen_, "Waiting for Wi-Fi time", LV_ALIGN_CENTER, 0, 28);
        lv_obj_set_style_text_color(date_, sub_col, 0);

        // 2. Futuristic Arc Gauge (Replaces Analog)
        arc_container_ = lv_obj_create(screen_);
        lv_obj_set_size(arc_container_, 165, 165);
        lv_obj_align(arc_container_, LV_ALIGN_CENTER, 0, -18);
        lv_obj_set_style_bg_opa(arc_container_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(arc_container_, 0, 0);
        lv_obj_set_style_pad_all(arc_container_, 0, 0);
        lv_obj_clear_flag(arc_container_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scrollbar_mode(arc_container_, LV_SCROLLBAR_MODE_OFF);

        arc_sec_ = lv_arc_create(arc_container_);
        lv_obj_set_size(arc_sec_, 156, 156);
        lv_obj_align(arc_sec_, LV_ALIGN_CENTER, 0, 0);
        lv_arc_set_rotation(arc_sec_, 270);
        lv_arc_set_bg_angles(arc_sec_, 0, 360);
        lv_arc_set_range(arc_sec_, 0, 60);
        lv_obj_remove_style(arc_sec_, nullptr, LV_PART_KNOB);
        lv_obj_clear_flag(arc_sec_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_clear_flag(arc_sec_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_arc_width(arc_sec_, 6, LV_PART_MAIN);
        lv_obj_set_style_arc_width(arc_sec_, 6, LV_PART_INDICATOR);
        lv_obj_set_style_arc_color(arc_sec_, lv_color_hex(0x0f172a), LV_PART_MAIN);
        lv_obj_set_style_arc_color(arc_sec_, color_, LV_PART_INDICATOR);

        arc_min_ = lv_arc_create(arc_container_);
        lv_obj_set_size(arc_min_, 138, 138);
        lv_obj_align(arc_min_, LV_ALIGN_CENTER, 0, 0);
        lv_arc_set_rotation(arc_min_, 270);
        lv_arc_set_bg_angles(arc_min_, 0, 360);
        lv_arc_set_range(arc_min_, 0, 60);
        lv_obj_remove_style(arc_min_, nullptr, LV_PART_KNOB);
        lv_obj_clear_flag(arc_min_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_clear_flag(arc_min_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_arc_width(arc_min_, 4, LV_PART_MAIN);
        lv_obj_set_style_arc_width(arc_min_, 4, LV_PART_INDICATOR);
        lv_obj_set_style_arc_color(arc_min_, lv_color_hex(0x1e293b), LV_PART_MAIN);
        lv_obj_set_style_arc_color(arc_min_, lv_color_hex(0x475569), LV_PART_INDICATOR);

        arc_time_ = Label(arc_container_, "--:--", LV_ALIGN_CENTER, 0, -8);
        lv_obj_set_style_text_font(arc_time_, &lv_font_montserrat_48, 0);
        lv_obj_set_style_text_color(arc_time_, color_, 0);

        arc_sec_lbl_ = Label(arc_container_, "-- SEC", LV_ALIGN_CENTER, 0, 24);
        lv_obj_set_style_text_color(arc_sec_lbl_, lv_color_hex(0x94a3b8), 0);

        // 3. Flip Clock Container
        flip_container_ = lv_obj_create(screen_);
        lv_obj_set_size(flip_container_, 230, 64);
        lv_obj_align(flip_container_, LV_ALIGN_CENTER, 0, -18);
        lv_obj_set_style_bg_opa(flip_container_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(flip_container_, 0, 0);
        lv_obj_set_style_pad_all(flip_container_, 0, 0);
        lv_obj_clear_flag(flip_container_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scrollbar_mode(flip_container_, LV_SCROLLBAR_MODE_OFF);

        const int flip_offsets[4] = {-88, -44, 44, 88};
        for (int i = 0; i < 4; ++i) {
            flip_cards_[i] = lv_obj_create(flip_container_);
            lv_obj_set_size(flip_cards_[i], 38, 52);
            lv_obj_align(flip_cards_[i], LV_ALIGN_CENTER, flip_offsets[i], 0);
            lv_obj_set_style_bg_color(flip_cards_[i], lv_color_hex(0x0f172a), 0);
            lv_obj_set_style_border_color(flip_cards_[i], lv_color_hex(0x334155), 0);
            lv_obj_set_style_border_width(flip_cards_[i], 1, 0);
            lv_obj_set_style_radius(flip_cards_[i], 8, 0);
            lv_obj_clear_flag(flip_cards_[i], LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_scrollbar_mode(flip_cards_[i], LV_SCROLLBAR_MODE_OFF);

            flip_labels_[i] = Label(flip_cards_[i], "0", LV_ALIGN_CENTER, 0, 0);
            lv_obj_set_style_text_color(flip_labels_[i], color_, 0);
        }
        flip_colon_ = Label(flip_container_, ":", LV_ALIGN_CENTER, 0, -2);
        lv_obj_set_style_text_color(flip_colon_, lv_color_hex(0x64748b), 0);

        // 4. Word Clock Container
        word_container_ = lv_obj_create(screen_);
        lv_obj_set_size(word_container_, 250, 110);
        lv_obj_align(word_container_, LV_ALIGN_CENTER, 0, -18);
        lv_obj_set_style_bg_opa(word_container_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(word_container_, 0, 0);
        lv_obj_set_style_pad_all(word_container_, 0, 0);
        lv_obj_clear_flag(word_container_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scrollbar_mode(word_container_, LV_SCROLLBAR_MODE_OFF);

        word_l1_ = Label(word_container_, "IT IS", LV_ALIGN_TOP_MID, 0, 0);
        lv_obj_set_style_text_color(word_l1_, lv_color_hex(0x64748b), 0);

        word_l2_ = Label(word_container_, "--", LV_ALIGN_TOP_MID, 0, 20);
        lv_obj_set_style_text_color(word_l2_, color_, 0);

        word_l3_ = Label(word_container_, "MINUTES PAST", LV_ALIGN_TOP_MID, 0, 42);
        lv_obj_set_style_text_color(word_l3_, lv_color_hex(0x64748b), 0);

        word_l4_ = Label(word_container_, "--", LV_ALIGN_TOP_MID, 0, 62);
        lv_obj_set_style_text_color(word_l4_, color_, 0);

        // 5. Binary Clock Container
        binary_container_ = lv_obj_create(screen_);
        lv_obj_set_size(binary_container_, 150, 110);
        lv_obj_align(binary_container_, LV_ALIGN_CENTER, 0, -18);
        lv_obj_set_style_bg_opa(binary_container_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(binary_container_, 0, 0);
        lv_obj_set_style_pad_all(binary_container_, 0, 0);
        lv_obj_clear_flag(binary_container_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scrollbar_mode(binary_container_, LV_SCROLLBAR_MODE_OFF);

        for (int i = 0; i < 5; ++i) {
            hour_bits_[i] = lv_obj_create(binary_container_);
            lv_obj_set_size(hour_bits_[i], 14, 14);
            lv_obj_align(hour_bits_[i], LV_ALIGN_TOP_LEFT, 30, i * 17);
            lv_obj_set_style_radius(hour_bits_[i], 3, 0);
            lv_obj_set_style_bg_color(hour_bits_[i], lv_color_hex(0x1e293b), 0);
            lv_obj_set_style_border_width(hour_bits_[i], 0, 0);
            lv_obj_clear_flag(hour_bits_[i], LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_scrollbar_mode(hour_bits_[i], LV_SCROLLBAR_MODE_OFF);
        }
        for (int i = 0; i < 6; ++i) {
            min_bits_[i] = lv_obj_create(binary_container_);
            lv_obj_set_size(min_bits_[i], 14, 14);
            lv_obj_align(min_bits_[i], LV_ALIGN_TOP_RIGHT, -30, i * 15);
            lv_obj_set_style_radius(min_bits_[i], 3, 0);
            lv_obj_set_style_bg_color(min_bits_[i], lv_color_hex(0x1e293b), 0);
            lv_obj_set_style_border_width(min_bits_[i], 0, 0);
            lv_obj_clear_flag(min_bits_[i], LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_scrollbar_mode(min_bits_[i], LV_SCROLLBAR_MODE_OFF);
        }

        // Menu button at bottom left to launch App List (Launcher)
        menu_btn_ = lv_btn_create(screen_);
        lv_obj_set_size(menu_btn_, 48, 24);
        lv_obj_align(menu_btn_, LV_ALIGN_BOTTOM_LEFT, 6, -6);
        lv_obj_set_style_bg_color(menu_btn_, is_light ? lv_color_hex(0xe2e8f0) : lv_color_hex(0x1e293b), 0);
        lv_obj_set_style_radius(menu_btn_, 6, 0);
        lv_obj_set_style_pad_all(menu_btn_, 0, 0);
        lv_obj_add_event_cb(menu_btn_, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("launcher");
        }, LV_EVENT_CLICKED, &manager_);
        lv_obj_t *menu_lbl = lv_label_create(menu_btn_);
        lv_label_set_text(menu_lbl, LV_SYMBOL_LIST);
        lv_obj_set_style_text_color(menu_lbl, is_light ? lv_color_hex(0x0f172a) : lv_color_hex(0xf8fafc), 0);
        lv_obj_set_style_text_font(menu_lbl, &lv_font_montserrat_14, 0);
        lv_obj_align(menu_lbl, LV_ALIGN_CENTER, 0, 0);

        lv_obj_add_flag(screen_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(screen_, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("launcher");
        }, LV_EVENT_CLICKED, &manager_);

        timer_ = lv_timer_create([](lv_timer_t *timer) { static_cast<ClockApp *>(timer->user_data)->Refresh(); }, 1000, this);
        ApplyLayout();
    }

    std::string GetStyle() const { return style_name_; }
    uint32_t GetColorHex() const { return color_hex_; }
    std::string GetMode() const { return mode_; }

    void LoadNvs()
    {
        nvs_handle_t nvs;
        if (nvs_open("clock_cfg", NVS_READONLY, &nvs) == ESP_OK) {
            char buf[32] = {};
            size_t len = sizeof(buf);
            if (nvs_get_str(nvs, "style", buf, &len) == ESP_OK && buf[0] != '\0') {
                style_name_ = buf;
            }
            uint32_t val = 0;
            if (nvs_get_u32(nvs, "color", &val) == ESP_OK) {
                color_hex_ = val;
                color_ = lv_color_hex(color_hex_);
            }
            char mode_buf[16] = {};
            len = sizeof(mode_buf);
            if (nvs_get_str(nvs, "mode", mode_buf, &len) == ESP_OK && mode_buf[0] != '\0') {
                mode_ = mode_buf;
            }
            nvs_close(nvs);
        }
    }

    void SaveNvs()
    {
        nvs_handle_t nvs;
        if (nvs_open("clock_cfg", NVS_READWRITE, &nvs) == ESP_OK) {
            nvs_set_str(nvs, "style", style_name_.c_str());
            nvs_set_u32(nvs, "color", color_hex_);
            nvs_set_str(nvs, "mode", mode_.c_str());
            nvs_commit(nvs);
            nvs_close(nvs);
        }
    }

    void SetStyle(const std::string &style, uint32_t color_hex, const std::string &mode = "")
    {
        if (!style.empty()) style_name_ = style;
        if (color_hex != 0) {
            color_hex_ = color_hex;
            color_ = lv_color_hex(color_hex_);
        }
        if (!mode.empty()) mode_ = mode;

        const bool is_light = (mode_ == "light");
        const lv_color_t bg_col = is_light ? lv_color_hex(0xF1F5F9) : lv_color_hex(0x000000);
        const lv_color_t sub_col = is_light ? lv_color_hex(0x475569) : lv_color_hex(0x94a3b8);

        if (screen_ != nullptr) lv_obj_set_style_bg_color(screen_, bg_col, 0);

        if (time_ != nullptr) lv_obj_set_style_text_color(time_, color_, 0);
        if (separator_ != nullptr) lv_obj_set_style_bg_color(separator_, color_, 0);
        if (date_ != nullptr) lv_obj_set_style_text_color(date_, sub_col, 0);

        if (arc_sec_ != nullptr) lv_obj_set_style_arc_color(arc_sec_, color_, LV_PART_INDICATOR);
        if (arc_time_ != nullptr) lv_obj_set_style_text_color(arc_time_, color_, 0);
        if (arc_sec_lbl_ != nullptr) lv_obj_set_style_text_color(arc_sec_lbl_, sub_col, 0);

        if (word_l2_ != nullptr) lv_obj_set_style_text_color(word_l2_, color_, 0);
        if (word_l4_ != nullptr) lv_obj_set_style_text_color(word_l4_, color_, 0);

        for (int i = 0; i < 4; ++i) {
            if (flip_cards_[i] != nullptr) lv_obj_set_style_bg_color(flip_cards_[i], is_light ? lv_color_hex(0xffffff) : lv_color_hex(0x0f172a), 0);
            if (flip_labels_[i] != nullptr) lv_obj_set_style_text_color(flip_labels_[i], color_, 0);
        }
        if (menu_btn_ != nullptr) {
            lv_obj_set_style_bg_color(menu_btn_, is_light ? lv_color_hex(0xe2e8f0) : lv_color_hex(0x1e293b), 0);
        }

        ApplyLayout();
        Refresh();
        SaveNvs();
    }

    void Destroy() override
    {
        if (timer_ != nullptr) lv_timer_del(timer_);
        timer_ = nullptr;
        ScreenApp::Destroy();
    }

    void Show() override
    {
        ScreenApp::Show();
        Refresh();
    }

private:
    void ApplyLayout()
    {
        if (screen_ == nullptr) return;
        lv_obj_add_flag(arc_container_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(flip_container_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(word_container_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(binary_container_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(time_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(separator_, LV_OBJ_FLAG_HIDDEN);

        if (style_name_ == "analog" || style_name_ == "arc" || style_name_ == "gauge") {
            lv_obj_clear_flag(arc_container_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_align(date_, LV_ALIGN_CENTER, 0, 68);
        } else if (style_name_ == "flip") {
            lv_obj_clear_flag(flip_container_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_align(date_, LV_ALIGN_CENTER, 0, 32);
        } else if (style_name_ == "word") {
            lv_obj_clear_flag(word_container_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_align(date_, LV_ALIGN_CENTER, 0, 48);
        } else if (style_name_ == "binary") {
            lv_obj_clear_flag(binary_container_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_align(date_, LV_ALIGN_CENTER, 0, 45);
        } else if (style_name_ == "minimal") {
            lv_obj_clear_flag(time_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_clear_flag(separator_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_align(time_, LV_ALIGN_CENTER, 0, -30);
            lv_obj_align(date_, LV_ALIGN_CENTER, 0, 25);
        } else { // digital / default
            lv_obj_clear_flag(time_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_align(time_, LV_ALIGN_CENTER, 0, -25);
            lv_obj_align(date_, LV_ALIGN_CENTER, 0, 15);
        }
    }

    void Refresh()
    {
        time_t now = time(nullptr);
        struct tm time_info{};
        localtime_r(&now, &time_info);

        char time_text[12] = "--:--";
        char date_text[32] = "Waiting for Wi-Fi time";

        if (time_info.tm_year >= 120) {
            strftime(time_text, sizeof(time_text), "%H:%M", &time_info);
            if (style_name_ == "minimal") {
                static const char *const days[] = {"SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"};
                std::strncpy(date_text, days[time_info.tm_wday % 7], sizeof(date_text) - 1);
            } else if (style_name_ == "digital") {
                char day_buf[8] = {}, month_buf[8] = {};
                strftime(day_buf, sizeof(day_buf), "%a", &time_info);
                strftime(month_buf, sizeof(month_buf), "%b", &time_info);
                for (auto &c : day_buf) c = std::toupper(c);
                for (auto &c : month_buf) c = std::toupper(c);
                std::snprintf(date_text, sizeof(date_text), "%s . %s %d", day_buf, month_buf, time_info.tm_mday);
            } else {
                strftime(date_text, sizeof(date_text), "%a, %b %d", &time_info);
            }

            if (style_name_ == "flip" && flip_labels_[0] != nullptr) {
                char h1[2] = {static_cast<char>('0' + (time_info.tm_hour / 10)), '\0'};
                char h2[2] = {static_cast<char>('0' + (time_info.tm_hour % 10)), '\0'};
                char m1[2] = {static_cast<char>('0' + (time_info.tm_min / 10)), '\0'};
                char m2[2] = {static_cast<char>('0' + (time_info.tm_min % 10)), '\0'};
                lv_label_set_text(flip_labels_[0], h1);
                lv_label_set_text(flip_labels_[1], h2);
                lv_label_set_text(flip_labels_[2], m1);
                lv_label_set_text(flip_labels_[3], m2);
            } else if (style_name_ == "word" && word_l2_ != nullptr) {
                lv_label_set_text(word_l2_, NumberToWord(time_info.tm_min));
                lv_label_set_text(word_l4_, NumberToWord(time_info.tm_hour % 12 == 0 ? 12 : time_info.tm_hour % 12));
            } else if (style_name_ == "binary" && hour_bits_[0] != nullptr) {
                const int h = time_info.tm_hour;
                const int m = time_info.tm_min;
                for (int i = 0; i < 5; ++i) {
                    const bool bit_on = (h & (1 << (4 - i))) != 0;
                    lv_obj_set_style_bg_color(hour_bits_[i], bit_on ? color_ : lv_color_hex(0x1e293b), 0);
                }
                for (int i = 0; i < 6; ++i) {
                    const bool bit_on = (m & (1 << (5 - i))) != 0;
                    lv_obj_set_style_bg_color(min_bits_[i], bit_on ? color_ : lv_color_hex(0x1e293b), 0);
                }
            }
        }
        if (time_ != nullptr) lv_label_set_text(time_, time_text);
        if (date_ != nullptr) lv_label_set_text(date_, date_text);

        // Update Arc Gauge values if arc/gauge/analog mode
        if ((style_name_ == "analog" || style_name_ == "arc" || style_name_ == "gauge") && arc_sec_ != nullptr) {
            lv_arc_set_value(arc_sec_, time_info.tm_sec);
            lv_arc_set_value(arc_min_, time_info.tm_min);
            lv_label_set_text(arc_time_, time_text);
            char sec_buf[16];
            std::snprintf(sec_buf, sizeof(sec_buf), "%02d SEC", time_info.tm_sec);
            lv_label_set_text(arc_sec_lbl_, sec_buf);
        }
    }

    lv_obj_t *time_ = nullptr;
    lv_obj_t *separator_ = nullptr;
    lv_obj_t *date_ = nullptr;

    lv_obj_t *arc_container_ = nullptr;
    lv_obj_t *arc_sec_ = nullptr;
    lv_obj_t *arc_min_ = nullptr;
    lv_obj_t *arc_time_ = nullptr;
    lv_obj_t *arc_sec_lbl_ = nullptr;

    lv_obj_t *flip_container_ = nullptr;
    lv_obj_t *flip_cards_[4]{};
    lv_obj_t *flip_labels_[4]{};
    lv_obj_t *flip_colon_ = nullptr;

    lv_obj_t *word_container_ = nullptr;
    lv_obj_t *word_l1_ = nullptr;
    lv_obj_t *word_l2_ = nullptr;
    lv_obj_t *word_l3_ = nullptr;
    lv_obj_t *word_l4_ = nullptr;

    lv_obj_t *binary_container_ = nullptr;
    lv_obj_t *hour_bits_[5]{};
    lv_obj_t *min_bits_[6]{};

    lv_timer_t *timer_ = nullptr;
    std::string style_name_ = "digital";
    uint32_t color_hex_ = 0x06b6d4;
    std::string mode_ = "light";
    lv_color_t color_ = lv_color_hex(0x06b6d4);
    lv_obj_t *menu_btn_ = nullptr;
};

static std::string s_active_wallpaper_url;
static std::string s_active_wallpaper_name;
static bool s_slideshow_active = false;
static int s_slideshow_interval_sec = 30;
static int s_current_slot_idx = 0;
constexpr size_t NUM_WALLPAPER_SLOTS = 10;
static const char *s_slot_filepaths[NUM_WALLPAPER_SLOTS] = {
    "/littlefs/wallpapers/wp_0.jpg",
    "/littlefs/wallpapers/wp_1.jpg",
    "/littlefs/wallpapers/wp_2.jpg",
    "/littlefs/wallpapers/wp_3.jpg",
    "/littlefs/wallpapers/wp_4.jpg",
    "/littlefs/wallpapers/wp_5.jpg",
    "/littlefs/wallpapers/wp_6.jpg",
    "/littlefs/wallpapers/wp_7.jpg",
    "/littlefs/wallpapers/wp_8.jpg",
    "/littlefs/wallpapers/wp_9.jpg"
};
static std::vector<std::string> s_playlist_urls;
static size_t s_playlist_idx = 0;

static lv_color_t *s_wallpaper_buf = nullptr;
static lv_img_dsc_t s_wallpaper_dsc = {
    .header = {
        .cf = LV_IMG_CF_TRUE_COLOR,
        .always_zero = 0,
        .reserved = 0,
        .w = 320,
        .h = 240,
    },
    .data_size = 320 * 240 * sizeof(lv_color_t),
    .data = nullptr,
};

namespace {
struct JpegBufferCtx {
    FILE *fp;
    lv_color_t *out_buf;
};

static size_t tjpgd_file_in_func(JDEC *jd, uint8_t *buff, size_t nbyte)
{
    auto *ctx = static_cast<JpegBufferCtx *>(jd->device);
    if (ctx == nullptr || ctx->fp == nullptr) return 0;
    if (buff == nullptr) {
        std::fseek(ctx->fp, nbyte, SEEK_CUR);
        return nbyte;
    }
    return std::fread(buff, 1, nbyte, ctx->fp);
}

static int tjpgd_memory_out_func(JDEC *jd, void *bitmap, JRECT *rect)
{
    auto *ctx = static_cast<JpegBufferCtx *>(jd->device);
    if (ctx == nullptr || ctx->out_buf == nullptr) return 0;

    const uint8_t *src_rgb888 = static_cast<const uint8_t *>(bitmap);
    const uint32_t box_w = rect->right - rect->left + 1;
    const uint32_t box_h = rect->bottom - rect->top + 1;

    for (uint32_t y = 0; y < box_h; ++y) {
        uint32_t dst_y = rect->top + y;
        if (dst_y >= 240) break;
        const uint8_t *src_row = src_rgb888 + (y * box_w * 3);
        lv_color_t *dst_row = ctx->out_buf + (dst_y * 320);
        for (uint32_t x = 0; x < box_w; ++x) {
            uint32_t dst_x = rect->left + x;
            if (dst_x >= 320) break;
            uint8_t r = src_row[x * 3 + 0];
            uint8_t g = src_row[x * 3 + 1];
            uint8_t b = src_row[x * 3 + 2];
            dst_row[dst_x] = lv_color_make(r, g, b);
        }
    }
    return 1;
}

static bool DecodeJpegFileToBuffer(const char *filepath)
{
    if (filepath == nullptr) return false;
    FILE *fp = std::fopen(filepath, "rb");
    if (fp == nullptr) {
        ESP_LOGW("wallpaper", "File not found: %s", filepath);
        return false;
    }

    if (s_wallpaper_buf == nullptr) {
        s_wallpaper_buf = static_cast<lv_color_t *>(heap_caps_malloc(320 * 240 * sizeof(lv_color_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (s_wallpaper_buf == nullptr) {
            s_wallpaper_buf = static_cast<lv_color_t *>(malloc(320 * 240 * sizeof(lv_color_t)));
        }
        if (s_wallpaper_buf == nullptr) {
            ESP_LOGE("wallpaper", "Failed to allocate wallpaper buffer");
            std::fclose(fp);
            return false;
        }
        s_wallpaper_dsc.data = reinterpret_cast<const uint8_t *>(s_wallpaper_buf);
    }

    uint8_t *work_buf = static_cast<uint8_t *>(heap_caps_malloc(4096, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (work_buf == nullptr) {
        work_buf = static_cast<uint8_t *>(malloc(4096));
        if (work_buf == nullptr) {
            std::fclose(fp);
            return false;
        }
    }

    JpegBufferCtx decode_ctx{fp, s_wallpaper_buf};
    JDEC jdec{};
    jdec.device = &decode_ctx;

    JRESULT res = jd_prepare(&jdec, tjpgd_file_in_func, work_buf, 4096, &decode_ctx);
    if (res == JDR_OK) {
        res = jd_decomp(&jdec, tjpgd_memory_out_func, 0);
        if (res == JDR_OK) {
            ESP_LOGI("wallpaper", "JPEG stream decode successful for %s", filepath);
            AddSystemLog("INFO", "wallpaper", "Decoded JPEG %s to buffer (%ux%u)", filepath, jdec.width, jdec.height);
        } else {
            ESP_LOGE("wallpaper", "jd_decomp failed: %d", res);
            AddSystemLog("ERROR", "wallpaper", "jd_decomp err: %d for %s", res, filepath);
        }
    } else {
        ESP_LOGE("wallpaper", "jd_prepare failed: %d", res);
        AddSystemLog("ERROR", "wallpaper", "jd_prepare err: %d for %s", res, filepath);
    }
    free(work_buf);
    std::fclose(fp);
    return res == JDR_OK;
}

static bool DownloadUrlToFile(const std::string &url, const char *dest_path)
{
    if (url.empty() || dest_path == nullptr) return false;
    esp_http_client_config_t http_cfg{};
    http_cfg.url = url.c_str();
    http_cfg.timeout_ms = 10000;
    http_cfg.buffer_size = 1024;
    http_cfg.buffer_size_tx = 512;
    http_cfg.crt_bundle_attach = esp_crt_bundle_attach;
    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    if (client == nullptr) {
        AddSystemLog("ERROR", "http", "Client init failed (internal=%u, psram=%u): %s",
                     static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                     static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)), dest_path);
        return false;
    }

    bool ok = false;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        int content_len = esp_http_client_fetch_headers(client);
        const int status_code = esp_http_client_get_status_code(client);
        if (status_code >= 200 && status_code < 300 && content_len < 2 * 1024 * 1024) {
            FILE *fp = std::fopen(dest_path, "wb");
            if (fp != nullptr) {
                char buf[1024];
                int read_bytes = 0;
                int total = 0;
                while ((read_bytes = esp_http_client_read(client, buf, sizeof(buf))) > 0) {
                    std::fwrite(buf, 1, read_bytes, fp);
                    total += read_bytes;
                }
                std::fclose(fp);
                ok = (total > 0);
                if (ok) {
                    AddSystemLog("INFO", "http", "Downloaded %d bytes: %s", total, dest_path);
                } else {
                    AddSystemLog("WARN", "http", "Downloaded 0 bytes: %s", dest_path);
                }
            } else {
                AddSystemLog("ERROR", "http", "Failed to open dest_path for write: %s", dest_path);
            }
        } else {
            AddSystemLog("ERROR", "http", "HTTP status=%d length=%d: %s",
                         status_code, content_len, dest_path);
        }
    } else {
        AddSystemLog("ERROR", "http", "HTTP GET failed (%s): %s", esp_err_to_name(err), dest_path);
    }
    esp_http_client_cleanup(client);
    return ok;
}


} // namespace

class WallpaperApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "wallpaper"; }

    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        lv_obj_set_style_bg_color(screen_, lv_color_hex(0x0f172a), 0);
        lv_obj_set_style_bg_opa(screen_, LV_OPA_COVER, 0);
        lv_obj_clear_flag(screen_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scrollbar_mode(screen_, LV_SCROLLBAR_MODE_OFF);
        lv_obj_add_flag(screen_, LV_OBJ_FLAG_CLICKABLE);

        // 1. Wallpaper Image Widget (covers full 320x240 LCD)
        img_obj_ = lv_img_create(screen_);
        lv_obj_set_size(img_obj_, 320, 240);
        lv_obj_align(img_obj_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_clear_flag(img_obj_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(img_obj_, LV_OBJ_FLAG_HIDDEN);

        // 2. Empty State Container (shown when no wallpaper is decoded)
        empty_container_ = lv_obj_create(screen_);
        lv_obj_set_size(empty_container_, 290, 170);
        lv_obj_align(empty_container_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_set_style_bg_color(empty_container_, lv_color_hex(0x1e293b), 0);
        lv_obj_set_style_border_color(empty_container_, lv_color_hex(0x334155), 0);
        lv_obj_set_style_border_width(empty_container_, 1, 0);
        lv_obj_set_style_radius(empty_container_, 14, 0);
        lv_obj_clear_flag(empty_container_, LV_OBJ_FLAG_SCROLLABLE);

        lv_obj_t *e_icon = lv_label_create(empty_container_);
        lv_label_set_text(e_icon, LV_SYMBOL_IMAGE);
        lv_obj_set_style_text_font(e_icon, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(e_icon, lv_color_hex(0x06b6d4), 0);
        lv_obj_align(e_icon, LV_ALIGN_TOP_MID, 0, 8);

        lv_obj_t *e_title = lv_label_create(empty_container_);
        lv_label_set_text(e_title, "No Wallpaper Loaded");
        lv_obj_set_style_text_font(e_title, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(e_title, lv_color_hex(0xffffff), 0);
        lv_obj_align(e_title, LV_ALIGN_TOP_MID, 0, 42);

        lv_obj_t *e_desc = lv_label_create(empty_container_);
        lv_label_set_text(e_desc, "Push from the Web Dashboard\nWallpaper page");
        lv_obj_set_style_text_align(e_desc, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(e_desc, lv_color_hex(0x94a3b8), 0);
        lv_obj_align(e_desc, LV_ALIGN_BOTTOM_MID, 0, -14);

        // 3. Back Button (Top Left)
        back_btn_ = lv_btn_create(screen_);
        lv_obj_set_size(back_btn_, 40, 28);
        lv_obj_align(back_btn_, LV_ALIGN_TOP_LEFT, 6, 6);
        lv_obj_set_style_bg_color(back_btn_, lv_color_hex(0x0f172a), 0);
        lv_obj_set_style_bg_opa(back_btn_, LV_OPA_80, 0);
        lv_obj_set_style_border_color(back_btn_, lv_color_hex(0x334155), 0);
        lv_obj_set_style_border_width(back_btn_, 1, 0);
        lv_obj_set_style_radius(back_btn_, 8, 0);
        lv_obj_set_style_shadow_width(back_btn_, 0, 0);
        lv_obj_set_style_pad_all(back_btn_, 0, 0);
        lv_obj_add_flag(back_btn_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_event_cb(back_btn_, [](lv_event_t *event) {
            auto *wp_app = static_cast<WallpaperApp *>(lv_event_get_user_data(event));
            if (wp_app != nullptr) {
                uint32_t now = esp_log_timestamp();
                if (wp_app->last_back_click_ms_ != 0 && now - wp_app->last_back_click_ms_ < 600) {
                    wp_app->last_back_click_ms_ = 0;
                    if (wp_app->auto_hide_timer_) lv_timer_pause(wp_app->auto_hide_timer_);
                    wp_app->manager_.CloseCurrent();
                } else {
                    wp_app->last_back_click_ms_ = now;
                    wp_app->ResetAutoHideTimer();
                }
            }
        }, LV_EVENT_CLICKED, this);

        lv_obj_t *b_lbl = lv_label_create(back_btn_);
        lv_label_set_text(b_lbl, LV_SYMBOL_LEFT);
        lv_obj_set_style_text_font(b_lbl, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(b_lbl, lv_color_hex(0x38bdf8), 0);
        lv_obj_align(b_lbl, LV_ALIGN_CENTER, 0, 0);

        // 3b. Slideshow Button (Top Right — Opposite to Back button)
        slideshow_btn_ = lv_btn_create(screen_);
        lv_obj_set_size(slideshow_btn_, 40, 28);
        lv_obj_align(slideshow_btn_, LV_ALIGN_TOP_RIGHT, -6, 6);
        lv_obj_set_style_bg_color(slideshow_btn_, lv_color_hex(0x0f172a), 0);
        lv_obj_set_style_bg_opa(slideshow_btn_, LV_OPA_80, 0);
        lv_obj_set_style_border_color(slideshow_btn_, lv_color_hex(0x334155), 0);
        lv_obj_set_style_border_width(slideshow_btn_, 1, 0);
        lv_obj_set_style_radius(slideshow_btn_, 8, 0);
        lv_obj_set_style_shadow_width(slideshow_btn_, 0, 0);
        lv_obj_set_style_pad_all(slideshow_btn_, 0, 0);
        lv_obj_add_flag(slideshow_btn_, LV_OBJ_FLAG_HIDDEN);

        slideshow_lbl_ = lv_label_create(slideshow_btn_);
        lv_label_set_text(slideshow_lbl_, LV_SYMBOL_PLAY);
        lv_obj_set_style_text_font(slideshow_lbl_, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(slideshow_lbl_, lv_color_hex(0x38bdf8), 0);
        lv_obj_align(slideshow_lbl_, LV_ALIGN_CENTER, 0, 0);

        lv_obj_add_event_cb(slideshow_btn_, [](lv_event_t *event) {
            auto *wp_app = static_cast<WallpaperApp *>(lv_event_get_user_data(event));
            if (wp_app != nullptr) {
                wp_app->ToggleSlideshow();
                wp_app->ResetAutoHideTimer();
            }
        }, LV_EVENT_CLICKED, this);

        // 4. Prev Arrow Button (<)
        prev_btn_ = lv_btn_create(screen_);
        lv_obj_set_size(prev_btn_, 32, 32);
        lv_obj_align(prev_btn_, LV_ALIGN_LEFT_MID, 6, 0);
        lv_obj_set_style_bg_color(prev_btn_, lv_color_hex(0x0f172a), 0);
        lv_obj_set_style_bg_opa(prev_btn_, LV_OPA_80, 0);
        lv_obj_set_style_border_color(prev_btn_, lv_color_hex(0x334155), 0);
        lv_obj_set_style_border_width(prev_btn_, 1, 0);
        lv_obj_set_style_radius(prev_btn_, 8, 0);
        lv_obj_set_style_shadow_width(prev_btn_, 0, 0);
        lv_obj_set_style_pad_all(prev_btn_, 0, 0);
        lv_obj_add_flag(prev_btn_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_event_cb(prev_btn_, [](lv_event_t *e) {
            auto *wp_app = static_cast<WallpaperApp *>(lv_event_get_user_data(e));
            if (wp_app != nullptr) {
                wp_app->manager_.TriggerPrevSlide();
                wp_app->RenderActiveSlot();
                wp_app->ResetAutoHideTimer();
            }
        }, LV_EVENT_CLICKED, this);
        lv_obj_t *p_lbl = lv_label_create(prev_btn_);
        lv_label_set_text(p_lbl, LV_SYMBOL_LEFT);
        lv_obj_set_style_text_font(p_lbl, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(p_lbl, lv_color_hex(0x38bdf8), 0);
        lv_obj_align(p_lbl, LV_ALIGN_CENTER, 0, 0);

        // 5. Next Arrow Button (>)
        next_btn_ = lv_btn_create(screen_);
        lv_obj_set_size(next_btn_, 32, 32);
        lv_obj_align(next_btn_, LV_ALIGN_RIGHT_MID, -6, 0);
        lv_obj_set_style_bg_color(next_btn_, lv_color_hex(0x0f172a), 0);
        lv_obj_set_style_bg_opa(next_btn_, LV_OPA_80, 0);
        lv_obj_set_style_border_color(next_btn_, lv_color_hex(0x334155), 0);
        lv_obj_set_style_border_width(next_btn_, 1, 0);
        lv_obj_set_style_radius(next_btn_, 8, 0);
        lv_obj_set_style_shadow_width(next_btn_, 0, 0);
        lv_obj_set_style_pad_all(next_btn_, 0, 0);
        lv_obj_add_flag(next_btn_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_event_cb(next_btn_, [](lv_event_t *e) {
            auto *wp_app = static_cast<WallpaperApp *>(lv_event_get_user_data(e));
            if (wp_app != nullptr) {
                wp_app->manager_.TriggerNextSlide();
                wp_app->RenderActiveSlot();
                wp_app->ResetAutoHideTimer();
            }
        }, LV_EVENT_CLICKED, this);

        lv_obj_t *n_lbl = lv_label_create(next_btn_);
        lv_label_set_text(n_lbl, LV_SYMBOL_RIGHT);
        lv_obj_set_style_text_font(n_lbl, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(n_lbl, lv_color_hex(0x38bdf8), 0);
        lv_obj_align(n_lbl, LV_ALIGN_CENTER, 0, 0);

        // 6. Screen click unhides navigation controls & sets 3-second auto-hide timer
        lv_obj_add_event_cb(screen_, [](lv_event_t *e) {
            auto *wp_app = static_cast<WallpaperApp *>(lv_event_get_user_data(e));
            if (wp_app != nullptr && wp_app->back_btn_ != nullptr) {
                lv_obj_t *target = lv_event_get_target(e);
                if (target == wp_app->back_btn_ || target == wp_app->slideshow_btn_ || target == wp_app->prev_btn_ || target == wp_app->next_btn_) {
                    return;
                }
                lv_obj_clear_flag(wp_app->back_btn_, LV_OBJ_FLAG_HIDDEN);
                if (wp_app->slideshow_btn_) lv_obj_clear_flag(wp_app->slideshow_btn_, LV_OBJ_FLAG_HIDDEN);
                if (wp_app->prev_btn_) lv_obj_clear_flag(wp_app->prev_btn_, LV_OBJ_FLAG_HIDDEN);
                if (wp_app->next_btn_) lv_obj_clear_flag(wp_app->next_btn_, LV_OBJ_FLAG_HIDDEN);
                wp_app->ResetAutoHideTimer();
            }
        }, LV_EVENT_CLICKED, this);

        // Gesture handling (swipe left/right)
        lv_obj_add_event_cb(screen_, [](lv_event_t *e) {
            auto *wp_app = static_cast<WallpaperApp *>(lv_event_get_user_data(e));
            lv_indev_t *indev = lv_indev_get_act();
            if (wp_app != nullptr && indev != nullptr) {
                lv_dir_t dir = lv_indev_get_gesture_dir(indev);
                if (dir == LV_DIR_LEFT) {
                    wp_app->manager_.TriggerNextSlide();
                    wp_app->RenderActiveSlot();
                } else if (dir == LV_DIR_RIGHT) {
                    wp_app->manager_.TriggerPrevSlide();
                    wp_app->RenderActiveSlot();
                }
            }
        }, LV_EVENT_GESTURE, this);

        Refresh();
    }

    void Show() override
    {
        ScreenApp::Show();
        if (back_btn_ != nullptr) lv_obj_add_flag(back_btn_, LV_OBJ_FLAG_HIDDEN);
        if (slideshow_btn_ != nullptr) lv_obj_add_flag(slideshow_btn_, LV_OBJ_FLAG_HIDDEN);
        if (prev_btn_ != nullptr) lv_obj_add_flag(prev_btn_, LV_OBJ_FLAG_HIDDEN);
        if (next_btn_ != nullptr) lv_obj_add_flag(next_btn_, LV_OBJ_FLAG_HIDDEN);

        // Sync button icon with active slideshow status
        if (slideshow_lbl_ != nullptr) {
            if (manager_.IsSlideshowActive()) {
                lv_label_set_text(slideshow_lbl_, LV_SYMBOL_PAUSE);
                lv_obj_set_style_text_color(slideshow_lbl_, lv_color_hex(0x10b981), 0);
            } else {
                lv_label_set_text(slideshow_lbl_, LV_SYMBOL_PLAY);
                lv_obj_set_style_text_color(slideshow_lbl_, lv_color_hex(0x38bdf8), 0);
            }
        }
        RenderActiveSlot();
    }

    void Hide() override
    {
        ScreenApp::Hide();
        if (slideshow_timer_ != nullptr) {
            lv_timer_pause(slideshow_timer_);
        }
    }

    void ToggleSlideshow()
    {
        bool is_active = manager_.IsSlideshowActive();
        if (is_active) {
            manager_.StopSlideshow();
            if (slideshow_timer_ != nullptr) {
                lv_timer_pause(slideshow_timer_);
            }
            if (slideshow_lbl_ != nullptr) {
                lv_label_set_text(slideshow_lbl_, LV_SYMBOL_PLAY);
                lv_obj_set_style_text_color(slideshow_lbl_, lv_color_hex(0x38bdf8), 0);
            }
        } else {
            manager_.StartSlideshow(5);
            if (slideshow_lbl_ != nullptr) {
                lv_label_set_text(slideshow_lbl_, LV_SYMBOL_PAUSE);
                lv_obj_set_style_text_color(slideshow_lbl_, lv_color_hex(0x10b981), 0);
            }
            if (slideshow_timer_ == nullptr) {
                slideshow_timer_ = lv_timer_create([](lv_timer_t *t) {
                    auto *wp_app = static_cast<WallpaperApp *>(t->user_data);
                    if (wp_app != nullptr && wp_app->manager_.IsSlideshowActive()) {
                        wp_app->manager_.TriggerNextSlide();
                        wp_app->RenderActiveSlot();
                    }
                }, 5000, this);
            } else {
                lv_timer_set_period(slideshow_timer_, 5000);
                lv_timer_reset(slideshow_timer_);
                lv_timer_resume(slideshow_timer_);
            }
            RenderActiveSlot();
        }
    }

    uint32_t last_back_click_ms_ = 0;
    lv_obj_t *img_obj_ = nullptr;
    lv_obj_t *empty_container_ = nullptr;
    lv_obj_t *back_btn_ = nullptr;
    lv_obj_t *slideshow_btn_ = nullptr;
    lv_obj_t *slideshow_lbl_ = nullptr;
    lv_obj_t *prev_btn_ = nullptr;
    lv_obj_t *next_btn_ = nullptr;
    lv_timer_t *auto_hide_timer_ = nullptr;
    lv_timer_t *slideshow_timer_ = nullptr;

    void ResetAutoHideTimer()
    {
        if (auto_hide_timer_ != nullptr) {
            lv_timer_reset(auto_hide_timer_);
            lv_timer_resume(auto_hide_timer_);
        } else {
            auto_hide_timer_ = lv_timer_create([](lv_timer_t *t) {
                auto *wp_app = static_cast<WallpaperApp *>(t->user_data);
                if (wp_app != nullptr) {
                    if (wp_app->back_btn_) lv_obj_add_flag(wp_app->back_btn_, LV_OBJ_FLAG_HIDDEN);
                    if (wp_app->slideshow_btn_) lv_obj_add_flag(wp_app->slideshow_btn_, LV_OBJ_FLAG_HIDDEN);
                    if (wp_app->prev_btn_) lv_obj_add_flag(wp_app->prev_btn_, LV_OBJ_FLAG_HIDDEN);
                    if (wp_app->next_btn_) lv_obj_add_flag(wp_app->next_btn_, LV_OBJ_FLAG_HIDDEN);
                    if (wp_app->auto_hide_timer_) lv_timer_pause(wp_app->auto_hide_timer_);
                }
            }, 3000, this);
        }
    }

    void RenderActiveSlot()
    {
        const char *current_path = s_slot_filepaths[s_current_slot_idx];
        bool decoded = DecodeJpegFileToBuffer(current_path);
        if (!decoded) {
            std::string url, name;
            manager_.GetWallpaperUrl(url, name);
            if (!url.empty()) {
                if (DownloadUrlToFile(url, current_path)) {
                    decoded = DecodeJpegFileToBuffer(current_path);
                }
            }
        }

        if (decoded && s_wallpaper_buf != nullptr && img_obj_ != nullptr) {
            lv_img_set_src(img_obj_, &s_wallpaper_dsc);
            lv_obj_clear_flag(img_obj_, LV_OBJ_FLAG_HIDDEN);
            if (empty_container_ != nullptr) lv_obj_add_flag(empty_container_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_invalidate(img_obj_);
        } else {
            if (img_obj_ != nullptr) lv_obj_add_flag(img_obj_, LV_OBJ_FLAG_HIDDEN);
            if (empty_container_ != nullptr) lv_obj_clear_flag(empty_container_, LV_OBJ_FLAG_HIDDEN);
        }
    }

    void Refresh()
    {
        RenderActiveSlot();
    }
};






class ManchesterUnitedApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "man-utd"; }

    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        lv_obj_set_style_bg_color(screen_, lv_color_hex(0x39000d), 0);
        lv_obj_clear_flag(screen_, LV_OBJ_FLAG_SCROLLABLE);

        background_ = lv_img_create(screen_);
        lv_obj_set_size(background_, 320, 240);
        lv_obj_align(background_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_add_flag(background_, LV_OBJ_FLAG_HIDDEN);

        lv_obj_t *details = lv_obj_create(screen_);
        lv_obj_set_size(details, 320, 88);
        lv_obj_align(details, LV_ALIGN_BOTTOM_MID, 0, 0);
        lv_obj_set_style_bg_color(details, lv_color_hex(0x160005), 0);
        lv_obj_set_style_bg_opa(details, LV_OPA_80, 0);
        lv_obj_set_style_border_width(details, 0, 0);
        lv_obj_set_style_radius(details, 0, 0);
        lv_obj_clear_flag(details, LV_OBJ_FLAG_SCROLLABLE);

        competition_ = Label(details, "UPDATING SCHEDULE...", LV_ALIGN_TOP_MID, 0, 6,
                             &lv_font_montserrat_14);
        lv_obj_set_style_text_color(competition_, lv_color_hex(0xffd34e), 0);

        matchup_ = Label(details, "Manchester United vs TBD", LV_ALIGN_TOP_MID, 0, 27,
                         &lv_font_montserrat_16);
        lv_obj_set_width(matchup_, 300);
        lv_label_set_long_mode(matchup_, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_align(matchup_, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(matchup_, lv_color_white(), 0);

        kickoff_ = Label(details, "--/--/----  -  --:--", LV_ALIGN_TOP_MID, 0, 53,
                         &lv_font_montserrat_14);
        lv_obj_set_style_text_color(kickoff_, lv_color_hex(0xf8c8d0), 0);

        status_ = Label(screen_, "Daily live fixture", LV_ALIGN_TOP_LEFT, 8, 38,
                        &lv_font_montserrat_14);
        lv_obj_set_style_text_color(status_, lv_color_white(), 0);

        lv_obj_t *refresh = lv_btn_create(screen_);
        lv_obj_set_size(refresh, 34, 26);
        lv_obj_align(refresh, LV_ALIGN_BOTTOM_RIGHT, -6, -6);
        lv_obj_set_style_bg_color(refresh, lv_color_hex(0xda291c), 0);
        lv_obj_set_style_radius(refresh, 7, 0);
        lv_obj_set_style_pad_all(refresh, 0, 0);
        lv_obj_add_event_cb(refresh, [](lv_event_t *event) {
            static_cast<ManchesterUnitedApp *>(lv_event_get_user_data(event))->StartRefresh(true);
        }, LV_EVENT_CLICKED, this);
        Label(refresh, LV_SYMBOL_REFRESH, LV_ALIGN_CENTER, 0, 0, &lv_font_montserrat_14);

        AddBackButton();
        RenderCache();
        refresh_timer_ = lv_timer_create([](lv_timer_t *timer) {
            auto *app = static_cast<ManchesterUnitedApp *>(timer->user_data);
            if (app->visible_) app->StartRefresh(false);
        }, 60U * 60U * 1000U, this);
    }

    void Show() override
    {
        visible_ = true;
        ScreenApp::Show();
        RenderCache();
        StartRefresh(false);
    }

    void Hide() override { visible_ = false; }

private:
    struct FetchContext {
        ManchesterUnitedApp *app;
        bool force;
    };

    struct FetchResult {
        ManchesterUnitedApp *app;
        bool schedule_ok;
    };

    static constexpr const char *kSchedulePath = "/littlefs/manutd_schedule.json";
    static constexpr const char *kScheduleTempPath = "/littlefs/manutd_schedule.tmp";
    static constexpr const char *kBackgroundPath = "/littlefs/manutd_background_v2.jpg";
    static constexpr const char *kBackgroundTempPath = "/littlefs/manutd_background.tmp";

    void StartRefresh(bool force)
    {
        bool expected = false;
        if (!refresh_running_.compare_exchange_strong(expected, true)) return;
        if (CONFIG_DOMOS_AI_HTTP_BASE[0] == '\0') {
            refresh_running_ = false;
            lv_label_set_text(status_, "Gateway URL not configured");
            return;
        }
        lv_label_set_text(status_, force ? "Refreshing now..." : "Checking daily update...");
        bool available = false;
        if (!s_widget_fetch_busy.compare_exchange_strong(available, true)) {
            refresh_running_ = false;
            deferred_force_ = deferred_force_ || force;
            lv_label_set_text(status_, "Waiting for other update...");
            lv_timer_set_period(refresh_timer_, 1000);
            return;
        }
        force = force || deferred_force_;
        deferred_force_ = false;
        lv_timer_set_period(refresh_timer_, 60U * 60U * 1000U);
        auto *context = new FetchContext{this, force};
        // This task writes LittleFS. Its stack must remain in internal RAM
        // because SPI flash operations temporarily disable the PSRAM cache.
        if (xTaskCreatePinnedToCore(
                FetchTask, "manutd_fetch", kWidgetFetchStackBytes, context, 3, nullptr, 0) != pdPASS) {
            delete context;
            s_widget_fetch_busy = false;
            refresh_running_ = false;
            deferred_force_ = force;
            lv_timer_set_period(refresh_timer_, 1000);
            lv_label_set_text(status_, "Unable to start update");
            AddSystemLog("ERROR", "man-utd", "Fetch task create failed (internal=%u, psram=%u)",
                         static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                         static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        }
    }

    static void FetchTask(void *data)
    {
        auto *context = static_cast<FetchContext *>(data);
        auto *app = context->app;
        const std::string base = CONFIG_DOMOS_AI_HTTP_BASE;
        std::string schedule_url = base + "/api/football/manchester-united";
        if (context->force) schedule_url += "?force=true";
        delete context;

        bool schedule_ok = DownloadUrlToFile(schedule_url, kScheduleTempPath);
        if (schedule_ok) {
            std::remove(kSchedulePath);
            schedule_ok = std::rename(kScheduleTempPath, kSchedulePath) == 0;
        } else {
            std::remove(kScheduleTempPath);
        }

        struct stat background_stat{};
        bool background_ok = stat(kBackgroundPath, &background_stat) == 0 && background_stat.st_size > 0;
        if (!background_ok) {
            background_ok = DownloadUrlToFile(
                base + "/api/football/manchester-united/background.jpg", kBackgroundTempPath);
            if (background_ok) {
                std::remove(kBackgroundPath);
                background_ok = std::rename(kBackgroundTempPath, kBackgroundPath) == 0;
            } else {
                std::remove(kBackgroundTempPath);
            }
        }

        AddSystemLog(schedule_ok ? "INFO" : "WARN", "man-utd",
                     "Refresh %s (stack free=%u)", schedule_ok ? "OK" : "failed",
                     static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
        auto *result = new FetchResult{app, schedule_ok};
        if (lv_async_call([](void *value) {
                auto *completed = static_cast<FetchResult *>(value);
                completed->app->refresh_running_ = false;
                if (completed->app->visible_) {
                    completed->app->RenderCache();
                    if (!completed->schedule_ok) {
                        lv_label_set_text(completed->app->status_, "Offline - showing cached fixture");
                    }
                }
                delete completed;
            }, result) != LV_RES_OK) {
            app->refresh_running_ = false;
            delete result;
        }
        s_widget_fetch_busy = false;
        vTaskDelete(nullptr);
    }

    void RenderCache()
    {
        if (DecodeJpegFileToBuffer(kBackgroundPath) && s_wallpaper_buf != nullptr) {
            lv_img_set_src(background_, &s_wallpaper_dsc);
            lv_obj_clear_flag(background_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_invalidate(background_);
        }

        FILE *fp = std::fopen(kSchedulePath, "rb");
        if (fp == nullptr) return;
        std::fseek(fp, 0, SEEK_END);
        const long size = std::ftell(fp);
        std::fseek(fp, 0, SEEK_SET);
        if (size <= 0 || size > 16384) {
            std::fclose(fp);
            return;
        }

        std::vector<char> json(static_cast<size_t>(size) + 1, 0);
        const size_t read = std::fread(json.data(), 1, static_cast<size_t>(size), fp);
        std::fclose(fp);
        if (read != static_cast<size_t>(size)) return;

        cJSON *root = cJSON_Parse(json.data());
        cJSON *match = root ? cJSON_GetObjectItemCaseSensitive(root, "next_match") : nullptr;
        if (!cJSON_IsObject(match)) {
            cJSON_Delete(root);
            return;
        }

        auto value = [match](const char *key, const char *fallback) {
            cJSON *item = cJSON_GetObjectItemCaseSensitive(match, key);
            return cJSON_IsString(item) ? item->valuestring : fallback;
        };
        const std::string competition = VietnameseToAscii(value("competition", "Football"));
        const std::string home = VietnameseToAscii(value("home_team", "Manchester United"));
        const std::string away = VietnameseToAscii(value("away_team", "TBD"));
        char matchup[128];
        char kickoff[64];
        std::snprintf(matchup, sizeof(matchup), "%s vs %s", home.c_str(), away.c_str());
        std::snprintf(kickoff, sizeof(kickoff), "%s  -  %s", value("local_date", "--/--/----"),
                      value("local_time", "--:--"));
        lv_label_set_text(competition_, competition.c_str());
        lv_label_set_text(matchup_, matchup);
        lv_label_set_text(kickoff_, kickoff);

        cJSON *stale = cJSON_GetObjectItemCaseSensitive(root, "stale");
        lv_label_set_text(status_, cJSON_IsTrue(stale) ? "Cached fixture" : "");
        cJSON_Delete(root);
    }

    lv_obj_t *background_ = nullptr;
    lv_obj_t *competition_ = nullptr;
    lv_obj_t *matchup_ = nullptr;
    lv_obj_t *kickoff_ = nullptr;
    lv_obj_t *status_ = nullptr;
    lv_timer_t *refresh_timer_ = nullptr;
    std::atomic<bool> refresh_running_{false};
    bool deferred_force_ = false;
    bool visible_ = false;
};


class CodexCreditApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "codex-credit"; }

    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        lv_obj_set_style_bg_color(screen_, lv_color_hex(0x07111f), 0);
        lv_obj_clear_flag(screen_, LV_OBJ_FLAG_SCROLLABLE);

        lv_obj_t *title = Label(screen_, "GENERAL USAGE LIMITS", LV_ALIGN_TOP_MID, 0, 7,
                                &lv_font_montserrat_16);
        lv_obj_set_style_text_color(title, lv_color_hex(0x7dd3fc), 0);
        CreateWindowCard(32, "5 HOUR", five_hour_percent_, five_hour_reset_, five_hour_bar_);
        CreateWindowCard(111, "WEEKLY", weekly_percent_, weekly_reset_, weekly_bar_);

        status_ = Label(screen_, "Waiting for usage data", LV_ALIGN_BOTTOM_MID, 0, -33,
                        &lv_font_montserrat_14);
        lv_obj_set_style_text_color(status_, lv_color_hex(0x94a3b8), 0);

        lv_obj_t *refresh = lv_btn_create(screen_);
        lv_obj_set_size(refresh, 34, 26);
        lv_obj_align(refresh, LV_ALIGN_BOTTOM_RIGHT, -6, -6);
        lv_obj_set_style_bg_color(refresh, lv_color_hex(0x0369a1), 0);
        lv_obj_set_style_radius(refresh, 7, 0);
        lv_obj_set_style_pad_all(refresh, 0, 0);
        lv_obj_add_event_cb(refresh, [](lv_event_t *event) {
            static_cast<CodexCreditApp *>(lv_event_get_user_data(event))->StartRefresh(true);
        }, LV_EVENT_CLICKED, this);
        Label(refresh, LV_SYMBOL_REFRESH, LV_ALIGN_CENTER, 0, 0, &lv_font_montserrat_14);

        AddBackButton();
        RenderCache();
        refresh_timer_ = lv_timer_create([](lv_timer_t *timer) {
            auto *app = static_cast<CodexCreditApp *>(timer->user_data);
            if (app->visible_) app->StartRefresh(false);
        }, 60U * 1000U, this);
    }

    void Show() override
    {
        visible_ = true;
        ScreenApp::Show();
        RenderCache();
        StartRefresh(false);
    }

    void Hide() override { visible_ = false; }

private:
    struct FetchContext {
        CodexCreditApp *app;
        bool force;
    };

    struct FetchResult {
        CodexCreditApp *app;
        bool ok;
    };

    static constexpr const char *kUsagePath = "/littlefs/codex_usage.json";
    static constexpr const char *kUsageTempPath = "/littlefs/codex_usage.tmp";

    static void StyleCard(lv_obj_t *card)
    {
        lv_obj_set_style_bg_color(card, lv_color_hex(0x111f33), 0);
        lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(card, lv_color_hex(0x263b55), 0);
        lv_obj_set_style_border_width(card, 1, 0);
        lv_obj_set_style_radius(card, 9, 0);
        lv_obj_set_style_pad_all(card, 7, 0);
        lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
    }

    void CreateWindowCard(int y, const char *heading, lv_obj_t *&percent,
                          lv_obj_t *&reset, lv_obj_t *&bar)
    {
        lv_obj_t *card = lv_obj_create(screen_);
        // Full-width rows keep reset dates readable on the 320x240 display.
        lv_obj_set_size(card, 308, 74);
        lv_obj_align(card, LV_ALIGN_TOP_MID, 0, y);
        StyleCard(card);
        lv_obj_t *heading_label = Label(card, heading, LV_ALIGN_TOP_LEFT, 0, -2,
                                        &lv_font_montserrat_14);
        lv_obj_set_style_text_color(heading_label, lv_color_hex(0x94a3b8), 0);
        percent = Label(card, "--% LEFT", LV_ALIGN_TOP_RIGHT, 0, -3,
                        &lv_font_montserrat_14);
        lv_obj_set_style_text_color(percent, lv_color_white(), 0);
        bar = lv_bar_create(card);
        lv_obj_set_size(bar, 292, 8);
        lv_obj_align(bar, LV_ALIGN_TOP_MID, 0, 28);
        lv_bar_set_range(bar, 0, 100);
        lv_bar_set_value(bar, 0, LV_ANIM_OFF);
        lv_obj_set_style_bg_color(bar, lv_color_hex(0x263b55), LV_PART_MAIN);
        lv_obj_set_style_bg_color(bar, lv_color_hex(0x22c55e), LV_PART_INDICATOR);
        reset = Label(card, "Reset --", LV_ALIGN_BOTTOM_LEFT, 0, 1,
                      &lv_font_montserrat_14);
        lv_obj_set_style_text_color(reset, lv_color_hex(0xcbd5e1), 0);
    }

    void StartRefresh(bool force)
    {
        bool expected = false;
        if (!refresh_running_.compare_exchange_strong(expected, true)) return;
        if (CONFIG_DOMOS_AI_HTTP_BASE[0] == '\0') {
            refresh_running_ = false;
            lv_label_set_text(status_, "Gateway URL not configured");
            return;
        }
        lv_label_set_text(status_, force ? "Refreshing..." : "Checking usage...");
        bool available = false;
        if (!s_widget_fetch_busy.compare_exchange_strong(available, true)) {
            refresh_running_ = false;
            deferred_force_ = deferred_force_ || force;
            lv_label_set_text(status_, "Waiting for other update...");
            lv_timer_set_period(refresh_timer_, 1000);
            return;
        }
        force = force || deferred_force_;
        deferred_force_ = false;
        lv_timer_set_period(refresh_timer_, 60U * 1000U);
        auto *context = new FetchContext{this, force};
        // This task writes LittleFS. Its stack must remain in internal RAM
        // because SPI flash operations temporarily disable the PSRAM cache.
        if (xTaskCreatePinnedToCore(
                FetchTask, "codex_fetch", kWidgetFetchStackBytes, context, 3, nullptr, 0) != pdPASS) {
            delete context;
            s_widget_fetch_busy = false;
            refresh_running_ = false;
            deferred_force_ = force;
            lv_timer_set_period(refresh_timer_, 1000);
            lv_label_set_text(status_, "Unable to start refresh");
            AddSystemLog("ERROR", "codex", "Fetch task create failed (internal=%u, psram=%u)",
                         static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                         static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        }
    }

    static void FetchTask(void *data)
    {
        auto *context = static_cast<FetchContext *>(data);
        CodexCreditApp *app = context->app;
        std::string url = std::string(CONFIG_DOMOS_AI_HTTP_BASE) + "/api/codex/usage";
        if (context->force) url += "?force=true";
        delete context;

        bool ok = DownloadUrlToFile(url, kUsageTempPath);
        if (ok) {
            std::remove(kUsagePath);
            ok = std::rename(kUsageTempPath, kUsagePath) == 0;
        } else {
            std::remove(kUsageTempPath);
        }

        AddSystemLog(ok ? "INFO" : "WARN", "codex",
                     "Refresh %s (stack free=%u)", ok ? "OK" : "failed",
                     static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
        auto *result = new FetchResult{app, ok};
        if (lv_async_call([](void *value) {
                auto *completed = static_cast<FetchResult *>(value);
                completed->app->refresh_running_ = false;
                if (completed->app->visible_) {
                    if (completed->ok) completed->app->RenderCache();
                    else lv_label_set_text(completed->app->status_, "Offline - cached usage");
                }
                delete completed;
            }, result) != LV_RES_OK) {
            app->refresh_running_ = false;
            delete result;
        }
        s_widget_fetch_busy = false;
        vTaskDelete(nullptr);
    }

    static const char *JsonText(cJSON *object, const char *name, const char *fallback)
    {
        cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
        return cJSON_IsString(item) ? item->valuestring : fallback;
    }

    static void RenderWindow(cJSON *window, lv_obj_t *percent, lv_obj_t *reset,
                             lv_obj_t *bar)
    {
        if (!cJSON_IsObject(window)) return;
        cJSON *remaining_item = cJSON_GetObjectItemCaseSensitive(window, "remaining_percent");
        int remaining = cJSON_IsNumber(remaining_item) ? remaining_item->valueint : 0;
        remaining = std::clamp(remaining, 0, 100);
        char percentage[24];
        char reset_text[48];
        std::snprintf(percentage, sizeof(percentage), "%d%% LEFT", remaining);
        std::snprintf(reset_text, sizeof(reset_text), "Reset %s",
                      JsonText(window, "resets_label", "--"));
        lv_label_set_text(percent, percentage);
        lv_label_set_text(reset, reset_text);
        lv_bar_set_value(bar, remaining, LV_ANIM_ON);
        const uint32_t color = remaining <= 20 ? 0xef4444 : (remaining <= 50 ? 0xf59e0b : 0x22c55e);
        lv_obj_set_style_bg_color(bar, lv_color_hex(color), LV_PART_INDICATOR);
    }

    void RenderCache()
    {
        FILE *fp = std::fopen(kUsagePath, "rb");
        if (fp == nullptr) return;
        std::fseek(fp, 0, SEEK_END);
        const long size = std::ftell(fp);
        std::fseek(fp, 0, SEEK_SET);
        if (size <= 0 || size > 8192) {
            std::fclose(fp);
            return;
        }
        std::vector<char> json(static_cast<size_t>(size) + 1, 0);
        const size_t read = std::fread(json.data(), 1, static_cast<size_t>(size), fp);
        std::fclose(fp);
        if (read != static_cast<size_t>(size)) return;

        cJSON *root = cJSON_Parse(json.data());
        if (root == nullptr) return;
        RenderWindow(cJSON_GetObjectItemCaseSensitive(root, "five_hour"),
                     five_hour_percent_, five_hour_reset_, five_hour_bar_);
        RenderWindow(cJSON_GetObjectItemCaseSensitive(root, "weekly"),
                     weekly_percent_, weekly_reset_, weekly_bar_);

        cJSON *stale = cJSON_GetObjectItemCaseSensitive(root, "stale");
        const char *updated = JsonText(root, "updated_label", "--");
        char status[48];
        std::snprintf(status, sizeof(status), "%s%s",
                      cJSON_IsTrue(stale) ? "Cached " : "Updated ", updated);
        lv_label_set_text(status_, status);
        cJSON_Delete(root);
    }

    lv_obj_t *five_hour_percent_ = nullptr;
    lv_obj_t *five_hour_reset_ = nullptr;
    lv_obj_t *five_hour_bar_ = nullptr;
    lv_obj_t *weekly_percent_ = nullptr;
    lv_obj_t *weekly_reset_ = nullptr;
    lv_obj_t *weekly_bar_ = nullptr;
    lv_obj_t *status_ = nullptr;
    lv_timer_t *refresh_timer_ = nullptr;
    std::atomic<bool> refresh_running_{false};
    bool deferred_force_ = false;
    bool visible_ = false;
};


class DashboardApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "dashboard"; }
    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        Label(screen_, "Dashboard", LV_ALIGN_TOP_MID, 0, 10);
        stats_ = Label(screen_, "CPU: --  RAM: --\nWi-Fi: -- dBm  Battery: --\nTemperature: --", LV_ALIGN_TOP_LEFT, 16, 42);
        chart_ = lv_chart_create(screen_);
        lv_obj_set_size(chart_, 270, 70);
        lv_obj_align(chart_, LV_ALIGN_BOTTOM_MID, 0, -50);
        lv_chart_set_type(chart_, LV_CHART_TYPE_LINE);
        lv_chart_set_point_count(chart_, 30);
        lv_chart_set_range(chart_, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
        series_ = lv_chart_add_series(chart_, lv_palette_main(LV_PALETTE_BLUE), LV_CHART_AXIS_PRIMARY_Y);
        AddBackButton();
        timer_ = lv_timer_create([](lv_timer_t *timer) { static_cast<DashboardApp *>(timer->user_data)->Refresh(); }, 1000, this);
    }
    void Destroy() override
    {
        if (timer_ != nullptr) lv_timer_del(timer_);
        timer_ = nullptr;
        ScreenApp::Destroy();
    }
    void Show() override { ScreenApp::Show(); Refresh(); }

private:
    void Refresh()
    {
        const unsigned ram_kib = heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024U;
        const int rssi = manager_.Wifi()->Rssi();
        char text[128];
        std::snprintf(text, sizeof(text), "CPU: task-managed\nRAM: %u KiB free\nWi-Fi: %d dBm  Battery: --\nTemperature: --", ram_kib, rssi);
        lv_label_set_text(stats_, text);
        const int32_t point = rssi == 0 ? 0 : (rssi + 100 > 100 ? 100 : rssi + 100);
        lv_chart_set_next_value(chart_, series_, point);
    }

    lv_obj_t *stats_ = nullptr;
    lv_obj_t *chart_ = nullptr;
    lv_chart_series_t *series_ = nullptr;
    lv_timer_t *timer_ = nullptr;
};

class SettingsApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "settings"; }
    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        Label(screen_, "Settings", LV_ALIGN_TOP_MID, 0, 12);
        Label(screen_, "Brightness", LV_ALIGN_CENTER, -85, -25);
        lv_obj_t *slider = lv_slider_create(screen_);
        lv_obj_set_width(slider, 150);
        lv_obj_align(slider, LV_ALIGN_CENTER, 35, -25);
        lv_slider_set_range(slider, 0, 100);
        lv_slider_set_value(slider, 75, LV_ANIM_OFF);
        lv_obj_add_event_cb(slider, [](lv_event_t *event) {
            auto *manager = static_cast<AppManager *>(lv_event_get_user_data(event));
            int val = lv_slider_get_value(static_cast<lv_obj_t *>(lv_event_get_target(event)));
            manager->Board()->SetBrightness(val);
            AddSystemLog("INFO", "display", "Brightness set to %d%%", val);
        }, LV_EVENT_VALUE_CHANGED, &manager_);
        Label(screen_, "Wallpaper: POST JPEG to http://<IP>/api/upload", LV_ALIGN_CENTER, 0, 18);
        Button(screen_, "Wi-Fi", LV_ALIGN_CENTER, -75, 55, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->Launch("wifi-setup");
        }, &manager_);
        Button(screen_, "OTA", LV_ALIGN_CENTER, 75, 55, ToOta, &manager_);
        AddBackButton();
    }
};

class WifiSetupApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "wifi-setup"; }
    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        Label(screen_, "Wi-Fi Config", LV_ALIGN_TOP_MID, 0, 4);

        // Back Button on Top Left Header (so keyboard at bottom doesn't cover it)
        lv_obj_t *back_btn = lv_btn_create(screen_);
        lv_obj_set_size(back_btn, 42, 24);
        lv_obj_align(back_btn, LV_ALIGN_TOP_LEFT, 6, 4);
        lv_obj_set_style_bg_color(back_btn, lv_color_hex(0x37474F), 0);
        lv_obj_set_style_bg_grad_color(back_btn, lv_color_hex(0x263238), 0);
        lv_obj_set_style_bg_grad_dir(back_btn, LV_GRAD_DIR_VER, 0);
        lv_obj_set_style_radius(back_btn, 6, 0);
        lv_obj_set_style_shadow_width(back_btn, 0, 0);
        lv_obj_set_style_pad_all(back_btn, 0, 0);
        lv_obj_add_event_cb(back_btn, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->CloseCurrent();
        }, LV_EVENT_CLICKED, &manager_);
        lv_obj_t *back_lbl = lv_label_create(back_btn);
        lv_label_set_text(back_lbl, LV_SYMBOL_LEFT);
        lv_obj_set_style_text_font(back_lbl, &lv_font_montserrat_14, 0);
        lv_obj_align(back_lbl, LV_ALIGN_CENTER, 0, 0);

        ta_ssid_ = lv_textarea_create(screen_);
        lv_obj_set_size(ta_ssid_, 145, 32);
        lv_obj_align(ta_ssid_, LV_ALIGN_TOP_LEFT, 8, 30);
        lv_textarea_set_placeholder_text(ta_ssid_, "SSID");
        lv_textarea_set_one_line(ta_ssid_, true);
        if (std::strcmp(manager_.Wifi()->Ssid(), "Not configured") != 0) {
            lv_textarea_set_text(ta_ssid_, manager_.Wifi()->Ssid());
        }

        ta_pass_ = lv_textarea_create(screen_);
        lv_obj_set_size(ta_pass_, 145, 32);
        lv_obj_align(ta_pass_, LV_ALIGN_TOP_RIGHT, -8, 30);
        lv_textarea_set_placeholder_text(ta_pass_, "Password");
        lv_textarea_set_password_mode(ta_pass_, true);
        lv_textarea_set_one_line(ta_pass_, true);

        status_label_ = Label(screen_, "Tap SSID/Pass & Connect", LV_ALIGN_TOP_MID, 0, 66);

        Button(screen_, "Connect", LV_ALIGN_TOP_RIGHT, -8, 86, OnConnectClicked, this);

        kb_ = lv_keyboard_create(screen_);
        lv_obj_set_size(kb_, 315, 115);
        lv_obj_align(kb_, LV_ALIGN_BOTTOM_MID, 0, 0);
        lv_keyboard_set_textarea(kb_, ta_ssid_);

        // Keyboard completion only hides the keyboard. Connecting is an
        // explicit action so credentials are never discarded accidentally.
        lv_obj_add_event_cb(kb_, [](lv_event_t *e) {
            auto *app = static_cast<WifiSetupApp *>(lv_event_get_user_data(e));
            lv_obj_add_flag(app->kb_, LV_OBJ_FLAG_HIDDEN);
        }, LV_EVENT_READY, this);
        lv_obj_add_event_cb(kb_, [](lv_event_t *e) {
            auto *app = static_cast<WifiSetupApp *>(lv_event_get_user_data(e));
            lv_obj_add_flag(app->kb_, LV_OBJ_FLAG_HIDDEN);
        }, LV_EVENT_CANCEL, this);

        lv_obj_add_event_cb(ta_ssid_, [](lv_event_t *e) {
            auto *app = static_cast<WifiSetupApp *>(lv_event_get_user_data(e));
            lv_keyboard_set_textarea(app->kb_, app->ta_ssid_);
            lv_obj_clear_flag(app->kb_, LV_OBJ_FLAG_HIDDEN);
        }, LV_EVENT_FOCUSED, this);

        lv_obj_add_event_cb(ta_pass_, [](lv_event_t *e) {
            auto *app = static_cast<WifiSetupApp *>(lv_event_get_user_data(e));
            lv_keyboard_set_textarea(app->kb_, app->ta_pass_);
            lv_obj_clear_flag(app->kb_, LV_OBJ_FLAG_HIDDEN);
        }, LV_EVENT_FOCUSED, this);


        timer_ = lv_timer_create([](lv_timer_t *timer) { static_cast<WifiSetupApp *>(timer->user_data)->RefreshStatus(); }, 1000, this);
    }

    void Destroy() override
    {
        if (timer_ != nullptr) lv_timer_del(timer_);
        timer_ = nullptr;
        ScreenApp::Destroy();
    }

    void Show() override
    {
        ScreenApp::Show();
        RefreshStatus();
    }

private:
    static void OnConnectClicked(lv_event_t *e)
    {
        auto *app = static_cast<WifiSetupApp *>(lv_event_get_user_data(e));
        const char *ssid = lv_textarea_get_text(app->ta_ssid_);
        const char *pass = lv_textarea_get_text(app->ta_pass_);
        if (ssid != nullptr && ssid[0] != '\0') {
            const bool started = app->manager_.Wifi()->ConnectTo(
                ssid, pass != nullptr ? pass : "");
            lv_label_set_text(app->status_label_, started
                ? "Connecting to Wi-Fi..." : "Unable to start connection");
            lv_obj_add_flag(app->kb_, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_label_set_text(app->status_label_, "SSID cannot be empty!");
        }
    }

    void RefreshStatus()
    {
        if (manager_.Wifi()->Connected()) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "OK: %s (%s)", manager_.Wifi()->Ssid(), manager_.Wifi()->IpAddress());
            lv_label_set_text(status_label_, buf);
        } else if (manager_.Wifi()->ConnectionFailed()) {
            char buf[64];
            std::snprintf(buf, sizeof(buf), "Connection failed (reason %d)",
                          manager_.Wifi()->LastDisconnectReason());
            lv_label_set_text(status_label_, buf);
        }
    }

    lv_obj_t *ta_ssid_ = nullptr;
    lv_obj_t *ta_pass_ = nullptr;
    lv_obj_t *kb_ = nullptr;
    lv_obj_t *status_label_ = nullptr;
    lv_timer_t *timer_ = nullptr;
};

class SmartHomeApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "smart-home"; }
    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        Label(screen_, "Smart Home", LV_ALIGN_TOP_MID, 0, 12);
        Button(screen_, "Lamp on", LV_ALIGN_CENTER, -75, -20, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->Mqtt()->Publish("dom/lamp", "{\"state\":\"on\"}");
        }, &manager_);
        Button(screen_, "Lamp off", LV_ALIGN_CENTER, 75, -20, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->Mqtt()->Publish("dom/lamp", "{\"state\":\"off\"}");
        }, &manager_);
        Label(screen_, "MQTT: dom/lamp", LV_ALIGN_CENTER, 0, 35);
        AddBackButton();
    }
};

class AssistantApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "assistant"; }

    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        lv_obj_set_style_bg_color(screen_, lv_color_hex(0x070B19), 0);
        lv_obj_set_style_bg_opa(screen_, LV_OPA_COVER, 0);
        lv_obj_clear_flag(screen_, LV_OBJ_FLAG_SCROLLABLE);

        // Header Title
        title_lbl_ = lv_label_create(screen_);
        lv_obj_clear_flag(title_lbl_, LV_OBJ_FLAG_CLICKABLE);
        lv_label_set_text(title_lbl_, "Dom Voice Assistant");
        lv_obj_set_style_text_color(title_lbl_, lv_color_hex(0x64748B), 0);
        lv_obj_set_style_text_font(title_lbl_, &lv_font_montserrat_14, 0);
        lv_obj_align(title_lbl_, LV_ALIGN_TOP_MID, 0, 8);

        // ── Avatar Eyes ──────────────────────────────────────────────────────
        eye_left_ = lv_obj_create(screen_);
        lv_obj_clear_flag(eye_left_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(eye_left_, 32, 42);
        lv_obj_align(eye_left_, LV_ALIGN_CENTER, -40, -45);
        lv_obj_set_style_bg_color(eye_left_, lv_color_hex(0x00E5FF), 0);
        lv_obj_set_style_bg_opa(eye_left_, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(eye_left_, 16, 0);
        lv_obj_set_style_border_width(eye_left_, 0, 0);
        lv_obj_set_style_shadow_width(eye_left_, 12, 0);
        lv_obj_set_style_shadow_color(eye_left_, lv_color_hex(0x00E5FF), 0);
        lv_obj_set_style_shadow_opa(eye_left_, LV_OPA_50, 0);
        lv_obj_clear_flag(eye_left_, LV_OBJ_FLAG_SCROLLABLE);

        eye_right_ = lv_obj_create(screen_);
        lv_obj_clear_flag(eye_right_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(eye_right_, 32, 42);
        lv_obj_align(eye_right_, LV_ALIGN_CENTER, 40, -45);
        lv_obj_set_style_bg_color(eye_right_, lv_color_hex(0x00E5FF), 0);
        lv_obj_set_style_bg_opa(eye_right_, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(eye_right_, 16, 0);
        lv_obj_set_style_border_width(eye_right_, 0, 0);
        lv_obj_set_style_shadow_width(eye_right_, 12, 0);
        lv_obj_set_style_shadow_color(eye_right_, lv_color_hex(0x00E5FF), 0);
        lv_obj_set_style_shadow_opa(eye_right_, LV_OPA_50, 0);
        lv_obj_clear_flag(eye_right_, LV_OBJ_FLAG_SCROLLABLE);

        // ── Audio Spectrum Wave Bars (Listening / Speaking) ─────────────────
        for (int i = 0; i < 4; ++i) {
            wave_bars_[i] = lv_obj_create(screen_);
            lv_obj_clear_flag(wave_bars_[i], LV_OBJ_FLAG_CLICKABLE);
            lv_obj_set_size(wave_bars_[i], 6, 12);
            lv_obj_align(wave_bars_[i], LV_ALIGN_CENTER, -18 + i * 12, -8);
            lv_obj_set_style_bg_color(wave_bars_[i], lv_color_hex(0x38BDF8), 0);
            lv_obj_set_style_radius(wave_bars_[i], 3, 0);
            lv_obj_set_style_border_width(wave_bars_[i], 0, 0);
            lv_obj_set_style_shadow_width(wave_bars_[i], 0, 0);
            lv_obj_clear_flag(wave_bars_[i], LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_add_flag(wave_bars_[i], LV_OBJ_FLAG_HIDDEN);
        }

        // ── Subtitle Card (Glassmorphic Dialogue Box) ────────────────────────
        subtitle_card_ = lv_obj_create(screen_);
        lv_obj_clear_flag(subtitle_card_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(subtitle_card_, 290, 78);
        lv_obj_align(subtitle_card_, LV_ALIGN_BOTTOM_MID, 0, -10);
        lv_obj_set_style_bg_color(subtitle_card_, lv_color_hex(0x111C33), 0);
        lv_obj_set_style_bg_opa(subtitle_card_, LV_OPA_90, 0);
        lv_obj_set_style_radius(subtitle_card_, 14, 0);
        lv_obj_set_style_border_color(subtitle_card_, lv_color_hex(0x1E293B), 0);
        lv_obj_set_style_border_width(subtitle_card_, 1, 0);
        lv_obj_set_style_pad_all(subtitle_card_, 8, 0);
        lv_obj_clear_flag(subtitle_card_, LV_OBJ_FLAG_SCROLLABLE);

        ai_lbl_ = lv_label_create(subtitle_card_);
        lv_obj_clear_flag(ai_lbl_, LV_OBJ_FLAG_CLICKABLE);
        lv_label_set_text(ai_lbl_, "Waiting for Hey Dom");
        lv_obj_set_style_text_color(ai_lbl_, lv_color_hex(0x38BDF8), 0);
        lv_obj_set_style_text_font(ai_lbl_, &lv_font_montserrat_14, 0);
        lv_obj_set_width(ai_lbl_, 270);
        lv_obj_set_style_text_align(ai_lbl_, LV_TEXT_ALIGN_CENTER, 0);
        lv_label_set_long_mode(ai_lbl_, LV_LABEL_LONG_DOT);
        lv_obj_center(ai_lbl_);

        // Tap screen to toggle conversation
        lv_obj_add_flag(screen_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(screen_, [](lv_event_t *event) {
            auto *app = static_cast<AssistantApp *>(lv_event_get_user_data(event));
            app->ToggleSession();
        }, LV_EVENT_CLICKED, this);

        AddBackButton();

        cancel_btn_ = lv_btn_create(screen_);
        lv_obj_set_size(cancel_btn_, 38, 30);
        lv_obj_align(cancel_btn_, LV_ALIGN_TOP_RIGHT, -6, 3);
        lv_obj_set_style_radius(cancel_btn_, 8, 0);
        lv_obj_set_style_bg_color(cancel_btn_, lv_color_hex(0xEF4444), 0);
        lv_obj_set_style_bg_opa(cancel_btn_, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(cancel_btn_, 0, 0);
        lv_obj_set_style_shadow_width(cancel_btn_, 0, 0);
        lv_obj_add_state(cancel_btn_, LV_STATE_DISABLED);
        lv_obj_add_event_cb(cancel_btn_, [](lv_event_t *event) {
            auto *app = static_cast<AssistantApp *>(lv_event_get_user_data(event));
            app->CancelResponse();
        }, LV_EVENT_CLICKED, this);
        lv_obj_t *cancel_label = lv_label_create(cancel_btn_);
        lv_label_set_text(cancel_label, "X");
        lv_obj_set_style_text_color(cancel_label, lv_color_hex(0xFFFFFF), 0);
        lv_obj_center(cancel_label);

        // Animation timer for face & wave bars
        anim_timer_ = lv_timer_create([](lv_timer_t *timer) {
            static_cast<AssistantApp *>(timer->user_data)->OnAnimTick();
        }, 80, this);
    }

    void Destroy() override
    {
        if (anim_timer_ != nullptr) lv_timer_del(anim_timer_);
        anim_timer_ = nullptr;
        ScreenApp::Destroy();
    }

    void Show() override
    {
        ScreenApp::Show();
        WifiService *wifi = manager_.Wifi();
        AssistantService *asst = manager_.Assistant();
        const char *ip = (wifi && wifi->IpAddress()) ? wifi->IpAddress() : "--";
        if (!wifi || !wifi->Connected() || strcmp(ip, "--") == 0) {
            lv_label_set_text(ai_lbl_, "Offline");
            return;
        }
        if (asst && !asst->IsActive()) {
            lv_label_set_text(ai_lbl_, "Connecting...");
            asst->SetUiCallback([this]() {
                // UI updates handled on anim timer ticks
            });
            asst->OpenAudioChannel();
        }
    }

    void Hide() override
    {
        // The voice session belongs to AssistantService, not to this screen.
        // Keep it alive while another app is visible so a voice-triggered app
        // launch does not disconnect the gateway or interrupt its TTS reply.
    }

private:
    void CancelResponse()
    {
        AssistantService *asst = manager_.Assistant();
        if (!asst) return;
        const AssistantState state = asst->GetState();
        if (state != AssistantState::Processing && state != AssistantState::Speaking) return;
        AddSystemLog("INFO", "assistant", "Cancel button pressed in state=%d", static_cast<int>(state));
        asst->CancelResponse();
        lv_label_set_text(ai_lbl_, "Cancelled");
    }

    void ToggleSession()
    {
        WifiService *wifi = manager_.Wifi();
        AssistantService *asst = manager_.Assistant();
        if (asst == nullptr) return;

        const char *ip = (wifi && wifi->IpAddress()) ? wifi->IpAddress() : "--";
        if (!wifi || !wifi->Connected() || strcmp(ip, "--") == 0) {
            lv_label_set_text(ai_lbl_, "Offline");
            return;
        }

        const AssistantState state = asst->GetState();
        AddSystemLog("INFO", "assistant", "Screen tap in state=%d", static_cast<int>(state));
        if (state == AssistantState::Armed) {
            asst->ActivateListening();
            lv_label_set_text(ai_lbl_, "Listening...");
        } else if (state == AssistantState::Listening) {
            // A tap ends only the current utterance. Keep WebSocket alive so
            // the delayed cloud STT/TTS response can reach the board.
            asst->SubmitSpeech();
            lv_label_set_text(ai_lbl_, "Thinking...");
        } else if (state == AssistantState::Idle) {
            lv_label_set_text(ai_lbl_, "Connecting...");
            asst->OpenAudioChannel();
        } else if (state == AssistantState::Processing) {
            lv_label_set_text(ai_lbl_, "Thinking...");
        }
    }

    void OnAnimTick()
    {
        tick_count_++;
        AssistantService *asst = manager_.Assistant();
        if (!asst) return;

        const AssistantState state = asst->GetState();
        if (state == AssistantState::Processing || state == AssistantState::Speaking) {
            lv_obj_clear_state(cancel_btn_, LV_STATE_DISABLED);
        } else {
            lv_obj_add_state(cancel_btn_, LV_STATE_DISABLED);
        }

        std::string emotion = asst->GetEmotion();

        // LCD intentionally shows only the state. Full transcripts and AI
        // responses remain available in the persistent dashboard history.
        switch (state) {
        case AssistantState::Connecting:
            lv_label_set_text(ai_lbl_, "Connecting...");
            break;
        case AssistantState::Armed:
            lv_label_set_text(ai_lbl_, "Waiting for Hey Dom");
            break;
        case AssistantState::Listening:
            lv_label_set_text(ai_lbl_, "Listening...");
            break;
        case AssistantState::Processing:
            lv_label_set_text(ai_lbl_, "Thinking...");
            break;
        case AssistantState::Speaking:
            lv_label_set_text(ai_lbl_, "Responding...");
            break;
        case AssistantState::Idle:
        default:
            lv_label_set_text(ai_lbl_, "Offline");
            break;
        }

        // 2. Natural Blinking (every ~3.2s)
        bool is_blinking = (tick_count_ % 40 == 0 || tick_count_ % 40 == 1);

        // 3. Eye Expression according to Emotion
        if (emotion == "happy" || emotion == "speaking") {
            // Cheerful eyes
            lv_obj_set_size(eye_left_, 34, 10);
            lv_obj_set_size(eye_right_, 34, 10);
            lv_obj_set_style_bg_color(eye_left_, lv_color_hex(0xFACC15), 0);
            lv_obj_set_style_bg_color(eye_right_, lv_color_hex(0xFACC15), 0);
            lv_obj_set_style_shadow_color(eye_left_, lv_color_hex(0xFACC15), 0);
            lv_obj_set_style_shadow_color(eye_right_, lv_color_hex(0xFACC15), 0);
        } else if (emotion == "thinking" || emotion == "processing") {
            // Thinking eyes (purple glow)
            lv_obj_set_size(eye_left_, 26, 30);
            lv_obj_set_size(eye_right_, 26, 30);
            lv_obj_set_style_bg_color(eye_left_, lv_color_hex(0xC084FC), 0);
            lv_obj_set_style_bg_color(eye_right_, lv_color_hex(0xC084FC), 0);
            lv_obj_set_style_shadow_color(eye_left_, lv_color_hex(0xC084FC), 0);
            lv_obj_set_style_shadow_color(eye_right_, lv_color_hex(0xC084FC), 0);
        } else if (emotion == "listening") {
            // Attentive listening eyes (Cyan neon)
            int h = is_blinking ? 4 : 44;
            lv_obj_set_size(eye_left_, 34, h);
            lv_obj_set_size(eye_right_, 34, h);
            lv_obj_set_style_bg_color(eye_left_, lv_color_hex(0x00E5FF), 0);
            lv_obj_set_style_bg_color(eye_right_, lv_color_hex(0x00E5FF), 0);
            lv_obj_set_style_shadow_color(eye_left_, lv_color_hex(0x00E5FF), 0);
            lv_obj_set_style_shadow_color(eye_right_, lv_color_hex(0x00E5FF), 0);
        } else if (emotion == "sad" || emotion == "error") {
            // Disconnected / error eyes (Soft red)
            lv_obj_set_size(eye_left_, 28, 8);
            lv_obj_set_size(eye_right_, 28, 8);
            lv_obj_set_style_bg_color(eye_left_, lv_color_hex(0xF87171), 0);
            lv_obj_set_style_bg_color(eye_right_, lv_color_hex(0xF87171), 0);
            lv_obj_set_style_shadow_color(eye_left_, lv_color_hex(0xF87171), 0);
            lv_obj_set_style_shadow_color(eye_right_, lv_color_hex(0xF87171), 0);
        } else {
            // Idle / Default
            int h = is_blinking ? 4 : 38;
            lv_obj_set_size(eye_left_, 30, h);
            lv_obj_set_size(eye_right_, 30, h);
            lv_obj_set_style_bg_color(eye_left_, lv_color_hex(0x38BDF8), 0);
            lv_obj_set_style_bg_color(eye_right_, lv_color_hex(0x38BDF8), 0);
            lv_obj_set_style_shadow_color(eye_left_, lv_color_hex(0x38BDF8), 0);
            lv_obj_set_style_shadow_color(eye_right_, lv_color_hex(0x38BDF8), 0);
        }

        // 4. Animate Sound Wave Bars (Listening / Speaking)
        bool show_wave = (emotion == "listening" || emotion == "speaking");
        for (int i = 0; i < 4; ++i) {
            if (show_wave) {
                lv_obj_clear_flag(wave_bars_[i], LV_OBJ_FLAG_HIDDEN);
                int bar_h = 6 + (int)(14.0f * (1.0f + sinf((tick_count_ * 0.4f) + i * 1.2f)) / 2.0f);
                lv_obj_set_size(wave_bars_[i], 6, bar_h);
            } else {
                lv_obj_add_flag(wave_bars_[i], LV_OBJ_FLAG_HIDDEN);
            }
        }
    }

    lv_obj_t *title_lbl_      = nullptr;
    lv_obj_t *eye_left_       = nullptr;
    lv_obj_t *eye_right_      = nullptr;
    lv_obj_t *wave_bars_[4]   = {nullptr};
    lv_obj_t *subtitle_card_  = nullptr;
    lv_obj_t *ai_lbl_         = nullptr;
    lv_obj_t *cancel_btn_     = nullptr;
    lv_timer_t *anim_timer_   = nullptr;
    uint32_t tick_count_      = 0;
};

class OtaApp final : public ScreenApp {
public:
    using ScreenApp::ScreenApp;
    const char *Id() const override { return "ota"; }
    void Create() override
    {
        screen_ = lv_obj_create(nullptr);
        Label(screen_, "OTA", LV_ALIGN_TOP_MID, 0, 12);
        Label(screen_, "HTTPS firmware update", LV_ALIGN_CENTER, 0, -20);
        Button(screen_, "Update", LV_ALIGN_CENTER, 0, 25, [](lv_event_t *event) {
            static_cast<AppManager *>(lv_event_get_user_data(event))->Ota()->UpdateFromUrl(CONFIG_DOMOS_OTA_FIRMWARE_URL);
        }, &manager_);
        AddBackButton();
    }
};
} // namespace

bool AppManager::Register(IApp *app)
{
    if (app == nullptr || app_count_ == kMaxApps) return false;
    apps_[app_count_++].app = app;
    return true;
}

bool AppManager::Start(ES3C28PBoard *board, WifiService *wifi, MqttService *mqtt, OtaService *ota,
                       MediaService *media, StorageManager *storage, AssistantService *assistant)
{
    board_     = board;
    wifi_      = wifi;
    mqtt_      = mqtt;
    ota_       = ota;
    media_     = media;
    storage_   = storage;
    assistant_ = assistant;
    launch_queue_ = xQueueCreate(8, sizeof(AppLaunchRequest));
    if (launch_queue_ == nullptr) return false;
    if (lv_timer_create([](lv_timer_t *timer) {
            auto *manager = static_cast<AppManager *>(timer->user_data);
            AppLaunchRequest request{};
            // Bounded work on the LVGL task; producers never touch LVGL.
            for (unsigned i = 0; i < 8 &&
                 xQueueReceive(static_cast<QueueHandle_t>(manager->launch_queue_), &request, 0) == pdTRUE; ++i) {
                if (request.type == AppRequestType::ClockSettings) {
                    manager->ApplyClockSettings(request.style, request.color, request.mode);
                    manager->Launch("clock");
                } else {
                    manager->Launch(request.app);
                }
            }
        }, 20, this) == nullptr) {
        vQueueDelete(static_cast<QueueHandle_t>(launch_queue_));
        launch_queue_ = nullptr;
        return false;
    }
    if (mqtt_ != nullptr) {
        mqtt_->SetMessageHandler([this](const char *topic, size_t topic_len,
                                        const char *payload, size_t payload_len) {
            auto *command = new PendingMqttCommand{
                this,
                std::string(topic, topic_len),
                std::string(payload, payload_len),
            };
            if (lv_async_call(ProcessMqttCommand, command) != LV_RES_OK) {
                delete command;
            }
        });
    }
    static LauncherApp launcher(*this);
    static ClockApp clock(*this);
    static WallpaperApp wallpaper(*this);
    static ManchesterUnitedApp man_utd(*this);
    static CodexCreditApp codex_credit(*this);
    static DashboardApp dashboard(*this);
    static SettingsApp settings(*this);
    static WifiSetupApp wifi_setup(*this);
    static SmartHomeApp smart_home(*this);
    static AssistantApp assistant_app(*this);
    static OtaApp ota_app(*this);
    Register(&launcher);
    Register(&clock);
    Register(&wallpaper);
    Register(&man_utd);
    Register(&codex_credit);
    Register(&dashboard);
    Register(&settings);
    Register(&wifi_setup);
    Register(&smart_home);
    Register(&assistant_app);
    Register(&ota_app);
    Launch("launcher");
    return true;
}

void AppManager::Launch(const std::string &app)
{
    for (size_t index = 0; index < app_count_; ++index) {
        AppEntry &entry = apps_[index];
        if (app != entry.app->Id()) continue;
        if (current_ != nullptr && current_ != entry.app) current_->Hide();
        if (!entry.created) {
            entry.app->Create();
            entry.created = true;
        }
        current_ = entry.app;
        current_->Show();
        const unsigned free_heap_kb = esp_get_free_heap_size() / 1024U;
        ESP_LOGI("apps", "Launched '%s' [heap: %uKB free]", app.c_str(), free_heap_kb);
        AddSystemLog("INFO", "apps", "Launched '%s' [heap: %uKB free]", app.c_str(), free_heap_kb);
        return;
    }
}

void AppManager::RequestLaunch(const std::string &app)
{
    AppLaunchRequest request{};
    if (launch_queue_ == nullptr || app.empty() || app.size() >= sizeof(request.app)) return;
    request.type = AppRequestType::Launch;
    std::memcpy(request.app, app.c_str(), app.size() + 1);
    if (xQueueSend(static_cast<QueueHandle_t>(launch_queue_), &request, 0) != pdTRUE) {
        ESP_LOGE("apps", "Unable to queue launch for '%s'", app.c_str());
    }
}

void AppManager::CloseCurrent()
{
    if (current_ != nullptr && std::string(current_->Id()) != "clock") {
        AddSystemLog("INFO", "apps", "Back button pressed -> Close '%s'", current_->Id());
        current_->Hide();
    }
    Launch("clock");
}

bool AppManager::RequestClockSettings(const std::string &style, uint32_t color_hex,
                                      const std::string &mode)
{
    AppLaunchRequest request{};
    if (launch_queue_ == nullptr || style.empty() || style.size() >= sizeof(request.style) ||
        mode.empty() || mode.size() >= sizeof(request.mode)) return false;
    request.type = AppRequestType::ClockSettings;
    request.color = color_hex;
    std::memcpy(request.style, style.c_str(), style.size() + 1);
    std::memcpy(request.mode, mode.c_str(), mode.size() + 1);
    if (xQueueSend(static_cast<QueueHandle_t>(launch_queue_), &request, 0) != pdTRUE) {
        ESP_LOGE("apps", "Unable to queue clock settings");
        return false;
    }
    return true;
}

bool AppManager::RequestWallpaperUrl(const std::string &url, const std::string &name)
{
    if (url.empty()) return false;
    bool available = false;
    if (!s_wallpaper_command_busy.compare_exchange_strong(available, true)) return false;
    auto *command = new PendingWallpaperCommand{this, false, url, name};
    if (xTaskCreatePinnedToCore(ProcessWallpaperCommand, "wallpaper_set", kWidgetFetchStackBytes,
                                command, 3, nullptr, 0) != pdPASS) {
        delete command;
        s_wallpaper_command_busy = false;
        return false;
    }
    return true;
}

bool AppManager::RequestWallpaperSync()
{
    bool available = false;
    if (!s_wallpaper_command_busy.compare_exchange_strong(available, true)) return false;
    auto *command = new PendingWallpaperCommand{this, true, "", ""};
    if (xTaskCreatePinnedToCore(ProcessWallpaperCommand, "wallpaper_sync", kWidgetFetchStackBytes,
                                command, 3, nullptr, 0) != pdPASS) {
        delete command;
        s_wallpaper_command_busy = false;
        return false;
    }
    return true;
}

void AppManager::ShowNextHomePage()
{
    static const char *pages[] = {"clock", "dashboard", "smart-home", "assistant", "man-utd", "codex-credit"};
    size_t current_page = 0;
    for (size_t index = 0; index < sizeof(pages) / sizeof(pages[0]); ++index) {
        if (current_ != nullptr && std::string(current_->Id()) == pages[index]) current_page = index;
    }
    Launch(pages[(current_page + 1) % (sizeof(pages) / sizeof(pages[0]))]);
}

void AppManager::ApplyClockSettings(const std::string &style, uint32_t color_hex, const std::string &mode)
{
    for (size_t index = 0; index < app_count_; ++index) {
        if (std::string(apps_[index].app->Id()) == "clock") {
            auto *clock_app = static_cast<ClockApp *>(apps_[index].app);
            if (!apps_[index].created) {
                clock_app->Create();
                apps_[index].created = true;
            }
            clock_app->SetStyle(style, color_hex, mode);
            AddSystemLog("INFO", "display", "Clock style set to '%s', color: #%06X, mode: %s", style.c_str(), color_hex, mode.c_str());
            return;
        }
    }
}

void AppManager::GetClockSettings(std::string &style, uint32_t &color_hex, std::string &mode)
{
    for (size_t index = 0; index < app_count_; ++index) {
        if (std::string(apps_[index].app->Id()) == "clock") {
            auto *clock_app = static_cast<ClockApp *>(apps_[index].app);
            if (!apps_[index].created) {
                clock_app->Create();
                apps_[index].created = true;
            }
            style = clock_app->GetStyle();
            color_hex = clock_app->GetColorHex();
            mode = clock_app->GetMode();
            return;
        }
    }
    style = "digital";
    color_hex = 0x06b6d4;
    mode = "dark";
}

void AppManager::SetWallpaperUrl(const std::string &url, const std::string &name)
{
    if (!url.empty()) s_active_wallpaper_url = url;
    if (!name.empty()) s_active_wallpaper_name = name;
    AddSystemLog("INFO", "wallpaper", "Active wallpaper set to: %s (%s)", s_active_wallpaper_name.c_str(), s_active_wallpaper_url.c_str());
    if (!url.empty()) {
        s_current_slot_idx = (s_current_slot_idx + 1) % NUM_WALLPAPER_SLOTS;
        const char *current_path = s_slot_filepaths[s_current_slot_idx];
        DownloadUrlToFile(url, current_path);
        RequestLaunch("wallpaper");
    }
}



void AppManager::GetWallpaperUrl(std::string &url, std::string &name)
{
    url = s_active_wallpaper_url;
    name = s_active_wallpaper_name;
}

void AppManager::StartSlideshow(int interval_sec)
{
    if (interval_sec <= 0) interval_sec = 30;
    s_slideshow_interval_sec = interval_sec;
    s_slideshow_active = true;
    AddSystemLog("INFO", "slideshow", "Slideshow started (interval=%ds, 3-slot preloader active)", interval_sec);
    TriggerNextSlide();
}

void AppManager::StopSlideshow()
{
    s_slideshow_active = false;
    AddSystemLog("INFO", "slideshow", "Slideshow stopped");
}

bool AppManager::IsSlideshowActive() const
{
    return s_slideshow_active;
}

void AppManager::SetSlideshowInterval(int interval_sec)
{
    if (interval_sec > 0) s_slideshow_interval_sec = interval_sec;
}

int AppManager::GetSlideshowInterval() const
{
    return s_slideshow_interval_sec;
}

void AppManager::TriggerNextSlide()
{
    s_current_slot_idx = (s_current_slot_idx + 1) % NUM_WALLPAPER_SLOTS;
    const char *current_path = s_slot_filepaths[s_current_slot_idx];
    DecodeJpegFileToBuffer(current_path);

    if (!s_playlist_urls.empty()) {
        s_playlist_idx = (s_playlist_idx + 1) % s_playlist_urls.size();
        int next_slot = (s_current_slot_idx + 1) % NUM_WALLPAPER_SLOTS;
        DownloadUrlToFile(s_playlist_urls[s_playlist_idx], s_slot_filepaths[next_slot]);
    }
}

void AppManager::TriggerPrevSlide()
{
    s_current_slot_idx = (s_current_slot_idx + NUM_WALLPAPER_SLOTS - 1) % NUM_WALLPAPER_SLOTS;
    const char *current_path = s_slot_filepaths[s_current_slot_idx];
    DecodeJpegFileToBuffer(current_path);

    if (!s_playlist_urls.empty()) {
        s_playlist_idx = (s_playlist_idx + s_playlist_urls.size() - 1) % s_playlist_urls.size();
        int prev_slot = (s_current_slot_idx + NUM_WALLPAPER_SLOTS - 1) % NUM_WALLPAPER_SLOTS;
        DownloadUrlToFile(s_playlist_urls[s_playlist_idx], s_slot_filepaths[prev_slot]);
    }
}

void AppManager::SyncWallpapersWithServer()
{
    const char *list_file = "/littlefs/wallpapers/list.json";
    if (CONFIG_DOMOS_AI_HTTP_BASE[0] == '\0') {
        AddSystemLog("WARN", "wallpaper", "AI Gateway HTTP URL is not configured");
        return;
    }
    std::string slideshow_url = std::string(CONFIG_DOMOS_AI_HTTP_BASE) + "/api/wallpapers/slideshow";
    bool ok = DownloadUrlToFile(slideshow_url, list_file);
    if (!ok) {
        AddSystemLog("WARN", "wallpaper", "Sync failed to download slideshow list from server");
        return;
    }

    std::vector<std::string> server_urls;
    FILE *fp = std::fopen(list_file, "rb");
    if (fp != nullptr) {
        std::fseek(fp, 0, SEEK_END);
        long sz = std::ftell(fp);
        std::fseek(fp, 0, SEEK_SET);
        if (sz > 0 && sz < 32768) {
            std::string body;
            body.resize(sz);
            std::fread(&body[0], 1, sz, fp);
            const char *w_pos = std::strstr(body.c_str(), "\"wallpapers\":[");
            if (w_pos != nullptr) {
                w_pos += 14;
                const char *end_bracket = std::strchr(w_pos, ']');
                while (*w_pos && (!end_bracket || w_pos < end_bracket)) {
                    const char *quote_start = std::strchr(w_pos, '"');
                    if (!quote_start || (end_bracket && quote_start > end_bracket)) break;
                    quote_start++;
                    const char *quote_end = std::strchr(quote_start, '"');
                    if (!quote_end) break;
                    server_urls.emplace_back(quote_start, quote_end - quote_start);
                    w_pos = quote_end + 1;
                }
            }
        }
        std::fclose(fp);
        remove(list_file);
    }

    s_playlist_urls = server_urls;
    s_playlist_idx = 0;

    if (server_urls.empty()) {
        for (size_t i = 0; i < NUM_WALLPAPER_SLOTS; ++i) {
            remove(s_slot_filepaths[i]);
        }
        s_active_wallpaper_url.clear();
        s_active_wallpaper_name.clear();
        AddSystemLog("INFO", "wallpaper", "Server has 0 wallpapers. Cleared all local cached slots.");
    } else {
        AddSystemLog("INFO", "wallpaper", "Synced %d wallpapers from server", static_cast<int>(server_urls.size()));
        bool found = false;
        for (const auto &u : server_urls) {
            if (u == s_active_wallpaper_url) {
                found = true;
                break;
            }
        }
        if (!found) {
            s_active_wallpaper_url = server_urls[0];
            const char *current_path = s_slot_filepaths[s_current_slot_idx];
            DownloadUrlToFile(s_active_wallpaper_url, current_path);
            AddSystemLog("INFO", "wallpaper", "Active wallpaper synchronized to: %s", s_active_wallpaper_url.c_str());
        }
    }

    RequestLaunch("wallpaper");
}

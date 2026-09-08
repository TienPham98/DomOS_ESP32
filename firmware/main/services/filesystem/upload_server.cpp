#include "upload_server.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <ctime>

#include <sys/stat.h>
#include "app/launcher/app_manager.h"
#include "esp_app_desc.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "services/wifi/wifi_service.h"

namespace {
constexpr char TAG[] = "upload";
constexpr size_t MAX_WALLPAPER_BYTES = 3 * 1024 * 1024;
constexpr char WALLPAPER_PATH[] = "/littlefs/wallpapers/upload.jpg";

static AppManager *s_apps = nullptr;


esp_err_t UploadHandler(httpd_req_t *request)
{
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_type(request, "application/json");
    httpd_resp_sendstr(request, "{\"ok\":true,\"info\":\"Wallpapers are managed in the DomOS Dashboard\"}");
    return ESP_OK;
}

esp_err_t ClockApiHandler(httpd_req_t *request)
{
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Headers", "*");

    if (request->method == HTTP_OPTIONS) {
        httpd_resp_send(request, nullptr, 0);
        return ESP_OK;
    }

    char buf[256] = {};
    int ret = httpd_req_recv(request, buf, sizeof(buf) - 1);
    if (ret > 0 && s_apps != nullptr) {
        buf[ret] = '\0';
        std::string style;
        uint32_t color = 0;
        std::string mode;
        s_apps->GetClockSettings(style, color, mode);

        if (std::strstr(buf, "\"minimal\"") || std::strstr(buf, "minimal")) style = "minimal";
        else if (std::strstr(buf, "\"analog\"") || std::strstr(buf, "analog")) style = "analog";
        else if (std::strstr(buf, "\"flip\"") || std::strstr(buf, "flip")) style = "flip";
        else if (std::strstr(buf, "\"binary\"") || std::strstr(buf, "binary")) style = "binary";
        else if (std::strstr(buf, "\"word\"") || std::strstr(buf, "word")) style = "word";
        else if (std::strstr(buf, "\"digital\"") || std::strstr(buf, "digital")) style = "digital";

        const char *cpos = std::strstr(buf, "\"color\":");
        if (cpos == nullptr) cpos = std::strstr(buf, "\"primary_color\":");
        if (cpos == nullptr) cpos = std::strstr(buf, "#");

        if (cpos != nullptr) {
            const char *hex_start = (cpos[0] == '#') ? cpos + 1 : std::strchr(cpos, ':');
            if (hex_start != nullptr) {
                while (*hex_start && (*hex_start == ':' || *hex_start == ' ' || *hex_start == '"' || *hex_start == '#')) {
                    hex_start++;
                }
                unsigned int parsed_color = 0;
                if (std::sscanf(hex_start, "%x", &parsed_color) == 1) {
                    color = parsed_color;
                }
            }
        }

        if (std::strstr(buf, "\"light\"") || std::strstr(buf, "light")) mode = "light";
        else if (std::strstr(buf, "\"dark\"") || std::strstr(buf, "dark")) mode = "dark";

        if (std::strstr(buf, "\"wallpaper_url\"") || std::strstr(buf, "\"wallpaper\"") || std::strstr(buf, "wallpaper")) {
            std::string url, name;
            const char *u_pos = std::strstr(buf, "\"wallpaper_url\":");
            if (u_pos == nullptr) u_pos = std::strstr(buf, "\"url\":");
            if (u_pos != nullptr) {
                const char *val_start = std::strchr(u_pos, ':');
                if (val_start != nullptr) {
                    val_start = std::strchr(val_start, '"');
                    if (val_start != nullptr) {
                        val_start++;
                        const char *val_end = std::strchr(val_start, '"');
                        if (val_end != nullptr) url.assign(val_start, val_end - val_start);
                    }
                }
            }
            const char *n_pos = std::strstr(buf, "\"name\":");
            if (n_pos == nullptr) n_pos = std::strstr(buf, "\"filename\":");
            if (n_pos != nullptr) {
                const char *val_start = std::strchr(n_pos, ':');
                if (val_start != nullptr) {
                    val_start = std::strchr(val_start, '"');
                    if (val_start != nullptr) {
                        val_start++;
                        const char *val_end = std::strchr(val_start, '"');
                        if (val_end != nullptr) name.assign(val_start, val_end - val_start);
                    }
                }
            }
            s_apps->SetWallpaperUrl(url, name);
            s_apps->Launch("wallpaper");
            httpd_resp_set_type(request, "application/json");
            httpd_resp_sendstr(request, "{\"ok\":true}");
            return ESP_OK;
        }

        s_apps->ApplyClockSettings(style, color, mode);
        s_apps->Launch("clock");
    }

    httpd_resp_set_type(request, "application/json");
    httpd_resp_sendstr(request, "{\"ok\":true}");
    return ESP_OK;
}

esp_err_t SlideshowApiHandler(httpd_req_t *request)
{
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Headers", "*");

    if (request->method == HTTP_OPTIONS) {
        httpd_resp_send(request, nullptr, 0);
        return ESP_OK;
    }

    char buf[512] = {};
    int ret = httpd_req_recv(request, buf, sizeof(buf) - 1);
    if (ret > 0 && s_apps != nullptr) {
        buf[ret] = '\0';
        int interval_sec = 30;
        const char *i_pos = std::strstr(buf, "\"interval\":");
        if (i_pos != nullptr) {
            std::sscanf(i_pos + 11, "%d", &interval_sec);
        }
        if (std::strstr(buf, "\"enabled\":false") || std::strstr(buf, "\"action\":\"stop\"")) {
            s_apps->StopSlideshow();
        } else {
            s_apps->StartSlideshow(interval_sec);
            s_apps->Launch("wallpaper");
        }
    }

    httpd_resp_set_type(request, "application/json");
    httpd_resp_sendstr(request, "{\"ok\":true}");
    return ESP_OK;
}

esp_err_t WallpaperApiHandler(httpd_req_t *request)

{
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Headers", "*");

    if (request->method == HTTP_OPTIONS) {
        httpd_resp_send(request, nullptr, 0);
        return ESP_OK;
    }

    char buf[512] = {};
    int ret = httpd_req_recv(request, buf, sizeof(buf) - 1);
    if (ret > 0 && s_apps != nullptr) {
        buf[ret] = '\0';
        if (std::strstr(buf, "\"action\":\"sync\"") || std::strstr(buf, "\"sync\"") || request->method == HTTP_DELETE) {
            s_apps->SyncWallpapersWithServer();
            httpd_resp_set_type(request, "application/json");
            httpd_resp_sendstr(request, "{\"ok\":true,\"synced\":true}");
            return ESP_OK;
        }

        std::string url, name;
        const char *u_pos = std::strstr(buf, "\"wallpaper_url\":");
        if (u_pos == nullptr) u_pos = std::strstr(buf, "\"url\":");
        if (u_pos != nullptr) {
            const char *val_start = std::strchr(u_pos, ':');
            if (val_start != nullptr) {
                val_start = std::strchr(val_start, '"');
                if (val_start != nullptr) {
                    val_start++;
                    const char *val_end = std::strchr(val_start, '"');
                    if (val_end != nullptr) url.assign(val_start, val_end - val_start);
                }
            }
        }
        const char *n_pos = std::strstr(buf, "\"name\":");
        if (n_pos == nullptr) n_pos = std::strstr(buf, "\"filename\":");
        if (n_pos != nullptr) {
            const char *val_start = std::strchr(n_pos, ':');
            if (val_start != nullptr) {
                val_start = std::strchr(val_start, '"');
                if (val_start != nullptr) {
                    val_start++;
                    const char *val_end = std::strchr(val_start, '"');
                    if (val_end != nullptr) name.assign(val_start, val_end - val_start);
                }
            }
        }
        s_apps->SetWallpaperUrl(url, name);
        s_apps->Launch("wallpaper");
    }

    httpd_resp_set_type(request, "application/json");
    httpd_resp_sendstr(request, "{\"ok\":true}");
    return ESP_OK;
}


esp_err_t LaunchApiHandler(httpd_req_t *request)
{
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Headers", "*");

    if (request->method == HTTP_OPTIONS) {
        httpd_resp_send(request, nullptr, 0);
        return ESP_OK;
    }

    char buf[128] = {};
    int ret = httpd_req_recv(request, buf, sizeof(buf) - 1);
    if (ret > 0 && s_apps != nullptr) {
        std::string app = "clock";
        if (std::strstr(buf, "assistant")) app = "assistant";
        else if (std::strstr(buf, "launcher")) app = "launcher";
        else if (std::strstr(buf, "dashboard")) app = "dashboard";
        else if (std::strstr(buf, "settings")) app = "settings";
        else if (std::strstr(buf, "wallpaper")) app = "wallpaper";
        else if (std::strstr(buf, "tracking-status")) app = "tracking-status";
        else if (std::strstr(buf, "man-utd")) app = "man-utd";
        else if (std::strstr(buf, "codex-credit")) app = "codex-credit";
        else if (std::strstr(buf, "smart-home")) app = "smart-home";
        else if (std::strstr(buf, "wifi-setup") || std::strstr(buf, "wifi_setup")) app = "wifi-setup";
        else if (std::strstr(buf, "ota")) app = "ota";
        else if (std::strstr(buf, "clock")) app = "clock";
        // HTTP handlers run outside the LVGL task. Queue the screen change so
        // all UI mutations stay on LVGL's owning thread.
        s_apps->RequestLaunch(app);
    }

    httpd_resp_set_type(request, "application/json");
    httpd_resp_sendstr(request, "{\"ok\":true}");
    return ESP_OK;
}


esp_err_t StatusHandler(httpd_req_t *request)
{
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Headers", "*");

    auto *wifi = static_cast<WifiService *>(request->user_ctx);
    const unsigned free_heap = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    const unsigned min_free_heap = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    const unsigned free_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    const esp_app_desc_t *app = esp_app_get_description();
    char response[512];
    std::snprintf(response, sizeof(response),
                  "{\"id\":\"es3c28p-01\",\"name\":\"ES3C28P Desk Terminal\",\"board\":\"ES3C28P\",\"mac\":\"B8:1F:3F:C3:97:54\","
                  "\"firmware\":\"0.3.5\",\"firmware_build\":\"%s %s %s\","
                  "\"online\":true,\"wifi\":{\"ssid\":\"%s\",\"ip\":\"%s\",\"rssi\":%d},"
                  "\"free_heap\":%u,\"min_free_heap\":%u,\"free_psram\":%u,\"uptime_ms\":%llu,"
                  "\"storage_used\":40960,\"storage_total\":7340032}",
                  app->version, app->date, app->time,
                  wifi->Ssid(), wifi->IpAddress(), wifi->Rssi(), free_heap,
                  min_free_heap, free_psram,
                  static_cast<unsigned long long>(esp_timer_get_time() / 1000));
    httpd_resp_set_type(request, "application/json");
    httpd_resp_sendstr(request, response);
    return ESP_OK;
}

struct LogRecord {
    char ts[12];
    char level[8];
    char source[16];
    char msg[96];
};

constexpr size_t LOG_BUFFER_SIZE = 50;
static LogRecord s_logs[LOG_BUFFER_SIZE];
static size_t s_log_head = 0;
static size_t s_log_count = 0;
static httpd_handle_t s_server_handle = nullptr;

void BroadcastWsLog(const LogRecord &rec)
{
    if (s_server_handle == nullptr) return;

    char json_buf[192];
    int len = std::snprintf(json_buf, sizeof(json_buf),
                            "{\"type\":\"log\",\"ts\":\"%s\",\"level\":\"%s\",\"source\":\"%s\",\"msg\":\"%s\"}",
                            rec.ts, rec.level, rec.source, rec.msg);

    size_t clients = 8;
    int client_fds[8] = {};
    if (httpd_get_client_list(s_server_handle, &clients, client_fds) == ESP_OK) {
        for (size_t i = 0; i < clients; ++i) {
            if (httpd_ws_get_fd_info(s_server_handle, client_fds[i]) == HTTPD_WS_CLIENT_WEBSOCKET) {
                httpd_ws_frame_t ws_pkt{};
                ws_pkt.type = HTTPD_WS_TYPE_TEXT;
                ws_pkt.payload = reinterpret_cast<uint8_t *>(json_buf);
                ws_pkt.len = len;
                httpd_ws_send_frame_async(s_server_handle, client_fds[i], &ws_pkt);
            }
        }
    }
}
} // namespace

void AddSystemLog(const char *level, const char *source, const char *fmt, ...)
{
    LogRecord &rec = s_logs[s_log_head];
    s_log_head = (s_log_head + 1) % LOG_BUFFER_SIZE;
    if (s_log_count < LOG_BUFFER_SIZE) s_log_count++;

    time_t now = time(nullptr);
    struct tm timeinfo{};
    localtime_r(&now, &timeinfo);
    std::strftime(rec.ts, sizeof(rec.ts), "%H:%M:%S", &timeinfo);

    std::strncpy(rec.level, level, sizeof(rec.level) - 1);
    rec.level[sizeof(rec.level) - 1] = '\0';

    std::strncpy(rec.source, source, sizeof(rec.source) - 1);
    rec.source[sizeof(rec.source) - 1] = '\0';

    va_list args;
    va_start(args, fmt);
    std::vsnprintf(rec.msg, sizeof(rec.msg), fmt, args);
    va_end(args);

    BroadcastWsLog(rec);
}

namespace {

esp_err_t LogsHandler(httpd_req_t *request)
{
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Headers", "*");
    httpd_resp_set_type(request, "application/json");

    httpd_resp_send_chunk(request, "[", 1);

    char chunk[256];
    for (size_t i = 0; i < s_log_count; ++i) {
        size_t idx = (s_log_head + LOG_BUFFER_SIZE - s_log_count + i) % LOG_BUFFER_SIZE;
        const LogRecord &rec = s_logs[idx];
        int len = std::snprintf(chunk, sizeof(chunk),
                                "%s{\"ts\":\"%s\",\"level\":\"%s\",\"source\":\"%s\",\"msg\":\"%s\"}",
                                i > 0 ? "," : "", rec.ts, rec.level, rec.source, rec.msg);
        if (len > 0) {
            httpd_resp_send_chunk(request, chunk, len);
        }
    }

    httpd_resp_send_chunk(request, "]", 1);
    httpd_resp_send_chunk(request, nullptr, 0);
    return ESP_OK;
}


esp_err_t WebSocketHandler(httpd_req_t *request)
{
    if (request->method == HTTP_GET) return ESP_OK; // WebSocket handshake
    httpd_ws_frame_t frame{};
    frame.type = HTTPD_WS_TYPE_TEXT;
    if (httpd_ws_recv_frame(request, &frame, 0) != ESP_OK) return ESP_FAIL;
    const char reply[] = "{\"event\":\"domos_connected\"}";
    frame.payload = reinterpret_cast<uint8_t *>(const_cast<char *>(reply));
    frame.len = sizeof(reply) - 1;
    return httpd_ws_send_frame(request, &frame);
}
} // namespace

bool WallpaperUploadServer::Start(WifiService *wifi, AppManager *apps)
{
    if (wifi == nullptr) return false;
    if (apps != nullptr) s_apps = apps;
    if (s_server_handle != nullptr) return true; // Already running

    httpd_handle_t server = nullptr;
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 16;
    config.stack_size = 10240;
    if (httpd_start(&server, &config) != ESP_OK) return false;
    s_server_handle = server;

    const httpd_uri_t upload_route = {
        .uri = "/upload",
        .method = HTTP_POST,
        .handler = UploadHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    if (httpd_register_uri_handler(server, &upload_route) != ESP_OK) return false;

    const httpd_uri_t slideshow_route = {
        .uri = "/api/slideshow",
        .method = HTTP_POST,
        .handler = SlideshowApiHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t slideshow_options_route = {
        .uri = "/api/slideshow",
        .method = HTTP_OPTIONS,
        .handler = SlideshowApiHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t api_upload_route = {
        .uri = "/api/upload",
        .method = HTTP_POST,
        .handler = UploadHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t status_route = {
        .uri = "/api/status",
        .method = HTTP_GET,
        .handler = StatusHandler,
        .user_ctx = wifi,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t logs_route = {
        .uri = "/api/logs",
        .method = HTTP_GET,
        .handler = LogsHandler,
        .user_ctx = wifi,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t clock_route = {
        .uri = "/api/clock",
        .method = HTTP_POST,
        .handler = ClockApiHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t clock_options_route = {
        .uri = "/api/clock",
        .method = HTTP_OPTIONS,
        .handler = ClockApiHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t wallpaper_route = {
        .uri = "/api/wallpaper",
        .method = HTTP_POST,
        .handler = WallpaperApiHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t wallpaper_options_route = {
        .uri = "/api/wallpaper",
        .method = HTTP_OPTIONS,
        .handler = WallpaperApiHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t launch_route = {
        .uri = "/api/launch",
        .method = HTTP_POST,
        .handler = LaunchApiHandler,
        .user_ctx = nullptr,
        .is_websocket = false,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    const httpd_uri_t websocket_route = {
        .uri = "/ws",
        .method = HTTP_GET,
        .handler = WebSocketHandler,
        .user_ctx = nullptr,
        .is_websocket = true,
        .handle_ws_control_frames = false,
        .supported_subprotocol = nullptr,
    };
    if (httpd_register_uri_handler(server, &slideshow_route) != ESP_OK ||
        httpd_register_uri_handler(server, &slideshow_options_route) != ESP_OK ||
        httpd_register_uri_handler(server, &api_upload_route) != ESP_OK ||
        httpd_register_uri_handler(server, &status_route) != ESP_OK ||
        httpd_register_uri_handler(server, &logs_route) != ESP_OK ||
        httpd_register_uri_handler(server, &clock_route) != ESP_OK ||
        httpd_register_uri_handler(server, &clock_options_route) != ESP_OK ||
        httpd_register_uri_handler(server, &wallpaper_route) != ESP_OK ||
        httpd_register_uri_handler(server, &wallpaper_options_route) != ESP_OK ||
        httpd_register_uri_handler(server, &launch_route) != ESP_OK ||
        httpd_register_uri_handler(server, &websocket_route) != ESP_OK) return false;


    ESP_LOGI(TAG, "HTTP API: POST /api/upload, POST /api/clock, POST /api/wallpaper, POST /api/launch, GET /api/status, GET /api/logs, WS /ws");
    AddSystemLog("INFO", "system", "ES3C28P DomOS v0.2.1 initialized");
    AddSystemLog("INFO", "display", "ILI9341 LCD 320x240 @ SPI2_HOST active");
    AddSystemLog("INFO", "touch", "FT6336 I2C Touch driver active");
    AddSystemLog("INFO", "http", "Upload & Log server online on port 80");
    return true;
}


# Phase 1 — ES3C28P hardware bring-up

Phase 1 là nền tảng phần cứng đã được triển khai cho đúng board ES3C28P.

## Phạm vi đã hoàn thành

| Hạng mục | Cấu hình hiện tại | Kiểm tra chấp nhận |
|---|---|---|
| ESP-IDF | 5.3.1, target ESP32-S3, C++17 | `build.bat` thành công |
| Flash/PSRAM | 16 MB flash, 8 MB Octal PSRAM | boot log và `/api/status` |
| LCD | ILI9341, SPI2 40 MHz, 320×240 landscape | Launcher đúng màu/hướng |
| Touch | FT6336G, I2C0 400 kHz, addr `0x38` | Bốn góc chạm đúng |
| Backlight | GPIO45 LEDC 5 kHz/10-bit | brightness 0–100 qua MCP |
| Audio | ES8311, I2S0 16 kHz, PA GPIO1 active-low | mic/loa hoạt động |
| LittleFS | partition 7 MB | wallpaper/cache/config |
| Wi-Fi | `Dom_12` dùng static `<DEVICE_IP>`; SSID khác dùng DHCP | board API truy cập được |
| HTTP server | port 80 | status/log/app API |
| Launcher | LVGL app manager | mở clock/wallpaper/assistant |

## Pin quan trọng

```text
LCD:   MOSI=11 MISO=13 SCLK=12 CS=10 DC=46 BL=45
Touch: SDA=16 SCL=15 RST=18 INT=17
Audio: MCLK=4 BCLK=5 WS=7 DIN=6 DOUT=8 PA=1
```

Nguồn chuẩn là `firmware/main/board/es3c28p/board_config.h`.

## Build và flash

```powershell
cd "D:\Work space\DomOS\firmware"
.\build.bat
.\flash.bat
```

Hoặc trong ESP-IDF shell:

```powershell
idf.py menuconfig
idf.py build
idf.py -p COM5 -b 460800 flash monitor
```

## Checklist sau flash

1. Serial log không báo lỗi init display/touch/ES8311.
2. Board kết nối SSID đã lưu; `Dom_12` dùng `<DEVICE_IP>`, mạng khác nhận IP qua DHCP.
3. `GET http://<DEVICE_IP>/api/status` trả firmware `0.3.5`.
4. `GET /api/logs` trả JSON array.
5. Launcher phản hồi touch.
6. Speaker PA mặc định mute cho tới TTS.
7. Mic RMS tăng khi nói; không stream mic trong lúc loa phát.

## Thay đổi so với tài liệu Phase 1 cũ

- Firmware dùng ESP-IDF 5.3.1, không phải 5.4.
- SPI là SPI2 theo code hiện tại.
- `/upload` không còn ghi raw JPEG; chỉ trả acknowledgement/hướng dẫn.
- Wallpaper chính đi qua Go storage → Dashboard → `/api/wallpaper`/slideshow sync.
- Launcher mặc định mở trước, không phải Clock.

Xem [`../firmware/README.md`](../firmware/README.md) và [`../firmware/DISPLAY_CONFIG.md`](../firmware/DISPLAY_CONFIG.md).

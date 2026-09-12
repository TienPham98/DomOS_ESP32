# ES3C28P Display and Touch

## Pin map

### ILI9341 LCD

| Signal | GPIO | Notes |
|---|---:|---|
| MOSI | 11 | SPI2 |
| MISO | 13 | SPI2 |
| SCLK | 12 | 40 MHz |
| CS | 10 | Chip select |
| DC | 46 | Data/command |
| BL | 45 | LEDC PWM |

### FT6336G touch

| Signal | GPIO | Notes |
|---|---:|---|
| SDA | 16 | I2C0, 400 kHz |
| SCL | 15 | I2C0, 400 kHz |
| RST | 18 | Hardware reset |
| INT | 17 | Interrupt |
| Address | — | `0x38` |

Touch and ES8311 share I2C0. Do not create a second bus on the same pins.

## Display geometry

- Physical panel: 240 × 320.
- LVGL canvas: 320 × 240 landscape.
- Pixel format: RGB565, BGR order, color inversion enabled.
- X/Y swap enabled; axis mirror disabled.
- Touch coordinates are rotated and clamped to the landscape canvas.

If panel rotation changes, update and test touch mapping in the same change.

## UI behavior

- Back controls use the enlarged touch target defined by the current AppManager implementation.
- App controls auto-hide after inactivity.
- Automatic Tracking Status rotation does not reveal hidden controls.
- An upward swipe from the lower area to the middle returns to the launcher.
- Wallpaper empty-state text is shown only when no valid image is decoded.

## Memory and thread safety

- LVGL runs on core 0; only its safe UI context may create, update, or delete objects.
- SPI draw buffers require internal DMA-capable memory.
- Large RGB565/JPEG buffers use PSRAM with allocation checks.
- HTTP and network callbacks update protected state and notify the UI through EventBus.
- Avoid large stack allocations in HTTP handlers and touch callbacks.

## Validation checklist

1. LCD boots with correct orientation, colors, and full 320 × 240 coverage.
2. Touch matches all four corners and swipe navigation.
3. Back and refresh controls hide and restore correctly.
4. Brightness changes smoothly through the MCP tool.
5. Wallpaper decode does not exhaust internal memory.
6. Assistant and Tracking Status transitions do not crash LVGL.

Authoritative files:

- `main/board/es3c28p/board_config.h`
- `main/board/es3c28p/display.cpp`
- `main/board/es3c28p/touch.cpp`
- `main/app/launcher/app_manager.cpp`

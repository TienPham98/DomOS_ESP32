# DomOS Go Core Backend

Fiber/GORM service for users, device registry, themes, wallpapers, OTA metadata, MQTT dispatch, and dashboard events.

## Architecture

```text
Dashboard REST/JWT -> Fiber handlers -> GORM -> PostgreSQL
                              |             `-> SQLite development fallback
                              +-> MQTT -> ESP32
                              `-> WebSocket -> Dashboard
```

```text
backend-go/
|-- cmd/server/             application entrypoint
|-- internal/auth/          registration, login, JWT middleware
|-- internal/devices/       device CRUD and MQTT commands
|-- internal/themes/        theme CRUD
|-- internal/wallpapers/    uploads and slideshow metadata
|-- internal/ota/           firmware metadata and broadcast
|-- internal/mqtt/          broker client and topics
|-- internal/websocket/     dashboard event hub
|-- pkg/config/             environment loader
|-- pkg/database/           database connection and migration
|-- migrations/             SQL reference migrations
`-- docker-compose.yml      local PostgreSQL, MQTT, and backend
```

## Private configuration

Copy the root `.env.example` to `.env`. Configure these names in local or deployment secrets without documenting their real values:

| Area | Variables |
|---|---|
| Runtime | `APP_ENV`, `APP_PORT`, `PUBLIC_BASE_URL`, `UPLOAD_DIR` |
| Database | `DATABASE_URL` |
| Authentication | `JWT_SECRET`, `JWT_EXPIRY_HOURS` |
| MQTT | `MQTT_BROKER`, `MQTT_CLIENT_ID`, `MQTT_USERNAME`, `MQTT_PASSWORD` |

Production requires a strong JWT secret, a protected PostgreSQL account, authenticated TLS MQTT, restricted CORS, signed firmware, and protected upload/delete routes.

## Run

```powershell
go mod download
go run .\cmd\server
```

Build a local binary:

```powershell
go build -o server.exe .\cmd\server
.\server.exe
```

Start the complete local data stack:

```powershell
docker compose up --build
```

If PostgreSQL is unavailable, development falls back to `domos.db`. Use persistent PostgreSQL and persistent upload storage in production.

## Route groups

| Group | Paths | Access |
|---|---|---|
| Health | `/`, `/healthz` | Public |
| Authentication | `/api/auth/*` | Login/register public; profile protected |
| Devices | `/api/devices`, `/api/device/:id` | JWT |
| Themes | `/api/themes`, `/api/theme/:id` | JWT |
| OTA | `/api/ota*` | JWT |
| Firmware discovery | `/api/firmware/latest` | Device-readable |
| Wallpapers | `/api/wallpaper*`, `/api/wallpapers*` | Current route policy |
| Realtime | `/ws` | Dashboard events |

The backend subscribes to device state topics, stores current status, and broadcasts normalized events to dashboard clients. Voice audio does not travel through this service.

## Test

```powershell
go test .\...
go vet .\...
go build .\...
```

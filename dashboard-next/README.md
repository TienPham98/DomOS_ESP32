# DomOS Dashboard

Next.js 16 and React 19 dashboard for device status, assistant history, board controls, themes, wallpapers, OTA, logs, and smart-home views.

## Data flow

```text
Browser
  |-- Core API client -> Go backend
  |-- Assistant history -> Voice Gateway
  `-- same-origin server routes -> Voice Gateway -> connected ESP32
```

Board-control requests use server-side routes under `app/api/`. The private control token remains on the deployment server and is never sent to the browser. Network identifiers and connection settings are intentionally hidden from the dashboard UI.

## Structure

```text
dashboard-next/
|-- app/                    App Router pages and server routes
|-- components/             shared UI and sidebar
|-- hooks/use-board.ts      cloud board polling
|-- lib/api.ts              Go backend client
|-- lib/board-api.ts        same-origin board status/control client
|-- lib/wallpaper-api.ts    wallpaper proxy client
`-- *.test.ts(x)            Vitest and Testing Library tests
```

Main pages: `/`, `/devices`, `/assistant`, `/wallpaper`, `/themes`, `/ota`, `/logs`, `/smart-home`, `/settings`, and `/clock`.

## Configuration

Create a private `.env.local` or use deployment secrets:

| Visibility | Variables |
|---|---|
| Browser-visible | `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_AI_GATEWAY_URL` |
| Server-only | `AI_GATEWAY_URL`, `BOARD_CONTROL_AUTH_TOKEN` |

Anything prefixed with `NEXT_PUBLIC_` is included in the browser bundle. It may contain a public API origin, but never a token, password, private device address, or internal-only hostname.

## Run and verify

```powershell
npm install
npm run dev
```

```powershell
npm test
npm run lint
npm run build
npm run start
```

The board hook polls the cloud gateway every 10 seconds. Volume, brightness, clock, wallpaper, and other board actions must go through the same-origin API proxy when the dashboard is deployed publicly.

## Deployment rules

- Store `BOARD_CONTROL_AUTH_TOKEN` only as a server-side secret and use the same value in the Voice Gateway.
- Configure real service origins in the deployment environment, not in source or Markdown.
- Do not expose Wi-Fi SSID, MQTT broker, device IP, MAC, API key, or authentication state in public pages.
- Keep direct-LAN helpers limited to local diagnostics; production control uses the cloud gateway session.

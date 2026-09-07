const gatewayUrl =
  process.env.AI_GATEWAY_URL ??
  process.env.NEXT_PUBLIC_AI_GATEWAY_URL ??
  "http://localhost:8000";

function gatewayHeaders(): HeadersInit | null {
  const token = process.env.BOARD_CONTROL_AUTH_TOKEN;
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : null;
}

async function proxy(path: string, init?: RequestInit): Promise<Response> {
  const headers = gatewayHeaders();
  if (!headers) {
    return Response.json(
      { error: "Dashboard device control is not configured" },
      { status: 503 },
    );
  }

  try {
    const upstream = await fetch(`${gatewayUrl.replace(/\/$/, "")}${path}`, {
      ...init,
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(8_000),
    });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch (error) {
    console.error("[api/board] gateway request failed", { path, error: String(error) });
    return Response.json({ error: "AI gateway is unavailable" }, { status: 502 });
  }
}

export async function GET() {
  return proxy("/api/device/status");
}

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    const parsed: unknown = await request.json();
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("invalid");
    body = parsed as Record<string, unknown>;
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const command = typeof body.command === "string" ? body.command : "settings";
  const paths: Record<string, string> = {
    settings: "/api/device/settings",
    clock: "/api/device/clock",
    wallpaper: "/api/device/wallpaper",
  };
  const path = paths[command];
  if (!path) {
    return Response.json({ error: "Unsupported board command" }, { status: 400 });
  }
  const { command: _command, ...payload } = body;
  void _command;
  return proxy(path, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

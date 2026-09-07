const gatewayUrl =
  process.env.AI_GATEWAY_URL ??
  process.env.NEXT_PUBLIC_AI_GATEWAY_URL ??
  "http://localhost:8000";

function gatewayHeaders(): HeadersInit | null {
  const token = process.env.DOMOS_AI_AUTH_TOKEN;
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
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  return proxy("/api/device/settings", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

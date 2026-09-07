const gatewayUrl =
  process.env.AI_GATEWAY_URL ??
  process.env.NEXT_PUBLIC_AI_GATEWAY_URL ??
  "http://localhost:8000";

function gatewayHeaders(contentType?: string | null): HeadersInit | null {
  const token = process.env.BOARD_CONTROL_AUTH_TOKEN;
  if (!token) return null;
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  if (contentType) headers["Content-Type"] = contentType;
  return headers;
}

async function proxy(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = gatewayHeaders(init.headers ? new Headers(init.headers).get("content-type") : null);
  if (!headers) {
    return Response.json({ error: "Dashboard content API is not configured" }, { status: 503 });
  }
  try {
    const upstream = await fetch(`${gatewayUrl.replace(/\/$/, "")}${path}`, {
      ...init,
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(30_000),
    });
    return new Response(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch (error) {
    console.error("[api/wallpapers] gateway request failed", { path, error: String(error) });
    return Response.json({ error: "AI gateway is unavailable" }, { status: 502 });
  }
}

export async function GET() {
  return proxy("/api/wallpapers");
}

export async function POST(request: Request) {
  return proxy("/api/wallpaper", {
    method: "POST",
    headers: { "Content-Type": request.headers.get("content-type") ?? "application/octet-stream" },
    body: await request.arrayBuffer(),
  });
}

export async function DELETE(request: Request) {
  const id = new URL(request.url).searchParams.get("id") ?? "";
  if (!/^[A-Za-z0-9-]{1,64}$/.test(id)) {
    return Response.json({ error: "Invalid wallpaper id" }, { status: 400 });
  }
  return proxy(`/api/wallpaper/${encodeURIComponent(id)}`, { method: "DELETE" });
}

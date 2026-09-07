import type { Wallpaper } from "./api";

interface WallpaperEnvelope {
  data?: Array<{
    id: string;
    name: string;
    url: string;
    thumbnail_url?: string;
    width?: number;
    height?: number;
    size_bytes?: number;
    created_at: string;
  }>;
  error?: string;
}

async function parseResponse<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error ?? payload.detail ?? `Request failed (${response.status})`);
  }
  return payload as T;
}

export async function fetchCloudWallpapers(signal?: AbortSignal): Promise<Wallpaper[]> {
  const response = await fetch("/api/wallpapers", { cache: "no-store", signal });
  const payload = await parseResponse<WallpaperEnvelope>(response);
  return (payload.data ?? []).map((item) => ({
    id: item.id,
    filename: item.name,
    url: item.url,
    thumbnail_url: item.thumbnail_url,
    width: item.width ?? 320,
    height: item.height ?? 240,
    size: item.size_bytes ?? 0,
    created_at: item.created_at,
  }));
}

export async function uploadCloudWallpaper(file: File): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  await parseResponse(await fetch("/api/wallpapers", { method: "POST", body: form }));
}

export async function deleteCloudWallpaper(id: string): Promise<void> {
  await parseResponse(await fetch(`/api/wallpapers?id=${encodeURIComponent(id)}`, { method: "DELETE" }));
}

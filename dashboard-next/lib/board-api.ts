export interface BoardStatus {
  id: string;
  name: string;
  board: string;
  mac: string;
  firmware: string;
  online: boolean;
  connection: "cloud";
  assistant_state?: string;
  audio_running?: boolean;
  free_heap?: number;
  storage_used?: number;
  storage_total?: number;
  volume?: number;
  brightness?: number;
}

export interface BoardSettings {
  volume?: number;
  brightness?: number;
}

export interface ClockAppearance {
  style: "digital" | "minimal" | "analog" | "flip" | "word" | "binary";
  color: string;
  mode: "dark" | "light";
}

async function parseResponse<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload.error ?? payload.detail ?? `Request failed (${response.status})`;
    throw new Error(message);
  }
  return payload as T;
}

export async function fetchBoardStatus(signal?: AbortSignal): Promise<BoardStatus> {
  const response = await fetch("/api/board", { cache: "no-store", signal });
  return parseResponse<BoardStatus>(response);
}

export async function updateBoardSettings(settings: BoardSettings): Promise<void> {
  await postBoardCommand(settings);
}

async function postBoardCommand(payload: object): Promise<void> {
  const response = await fetch("/api/board", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await parseResponse(response);
}

export async function updateClockAppearance(settings: ClockAppearance): Promise<void> {
  await postBoardCommand({ command: "clock", ...settings });
}

export async function setBoardWallpaper(wallpaperId: string): Promise<void> {
  await postBoardCommand({ command: "wallpaper", action: "set", wallpaper_id: wallpaperId });
}

export async function syncBoardWallpapers(): Promise<void> {
  await postBoardCommand({ command: "wallpaper", action: "sync" });
}

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
  const response = await fetch("/api/board", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  await parseResponse(response);
}

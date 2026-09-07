import {
  fetchBoardStatus,
  setBoardWallpaper,
  syncBoardWallpapers,
  updateBoardSettings,
  updateClockAppearance,
} from "./board-api";

describe("board cloud API", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("reads board status from the same-origin proxy", async () => {
    const status = { id: "board", online: true, connection: "cloud" };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => status,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchBoardStatus()).resolves.toEqual(status);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/board",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("posts exact volume and brightness values", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
    vi.stubGlobal("fetch", fetchMock);

    await updateBoardSettings({ volume: 65, brightness: 40 });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/board",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ volume: 65, brightness: 40 }),
      }),
    );
  });

  it("surfaces proxy errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ error: "Board offline" }),
    }));

    await expect(fetchBoardStatus()).rejects.toThrow("Board offline");
  });

  it("routes clock and wallpaper commands through the cloud proxy", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
    vi.stubGlobal("fetch", fetchMock);

    await updateClockAppearance({ style: "minimal", color: "#06b6d4", mode: "dark" });
    await setBoardWallpaper("wallpaper-1");
    await syncBoardWallpapers();

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/board", expect.objectContaining({
      body: JSON.stringify({ command: "clock", style: "minimal", color: "#06b6d4", mode: "dark" }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/board", expect.objectContaining({
      body: JSON.stringify({ command: "wallpaper", action: "set", wallpaper_id: "wallpaper-1" }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/board", expect.objectContaining({
      body: JSON.stringify({ command: "wallpaper", action: "sync" }),
    }));
  });
});

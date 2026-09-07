import {
  deleteCloudWallpaper,
  fetchCloudWallpapers,
  uploadCloudWallpaper,
} from "./wallpaper-api";

describe("wallpaper cloud API", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("maps backend metadata to dashboard wallpapers", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        data: [{
          id: "wp-1",
          name: "desk.jpg",
          url: "https://gateway.example/uploads/wallpapers/bg.jpg",
          thumbnail_url: "https://gateway.example/uploads/wallpapers/thumb.jpg",
          width: 320,
          height: 240,
          size_bytes: 1234,
          created_at: "2026-09-07T00:00:00Z",
        }],
      }),
    }));

    const result = await fetchCloudWallpapers();
    expect(result[0]).toMatchObject({ id: "wp-1", filename: "desk.jpg", size: 1234 });
  });

  it("uploads and deletes through same-origin server routes", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ data: {} }) });
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["image"], "desk.jpg", { type: "image/jpeg" });

    await uploadCloudWallpaper(file);
    await deleteCloudWallpaper("wp-1");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/wallpapers", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/wallpapers?id=wp-1", { method: "DELETE" });
  });
});

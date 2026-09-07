import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { setBoardWallpaper } from "@/lib/board-api";
import { fetchCloudWallpapers } from "@/lib/wallpaper-api";

import WallpaperPage from "./page";

vi.mock("@/lib/board-api", () => ({
  setBoardWallpaper: vi.fn(),
  syncBoardWallpapers: vi.fn(),
}));
vi.mock("@/lib/wallpaper-api", () => ({
  deleteCloudWallpaper: vi.fn(),
  fetchCloudWallpapers: vi.fn(),
  uploadCloudWallpaper: vi.fn(),
}));

describe("WallpaperPage", () => {
  beforeEach(() => {
    vi.mocked(fetchCloudWallpapers).mockResolvedValue([{
      id: "wp-1",
      filename: "desk.jpg",
      url: "https://gateway.example/uploads/wallpapers/bg.jpg",
      width: 320,
      height: 240,
      size: 1234,
      created_at: "2026-09-07T00:00:00Z",
    }]);
    vi.mocked(setBoardWallpaper).mockResolvedValue();
  });

  it("loads wallpaper metadata and applies a selection through cloud APIs", async () => {
    render(<WallpaperPage />);
    fireEvent.click(await screen.findByText("desk.jpg"));

    await waitFor(() => expect(setBoardWallpaper).toHaveBeenCalledWith("wp-1"));
    expect(await screen.findByText(/queued on the connected board/)).toBeInTheDocument();
    expect(screen.queryByTitle("Fixed ESP32 device IP")).not.toBeInTheDocument();
  });
});

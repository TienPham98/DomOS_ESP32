import { renderHook, waitFor } from "@testing-library/react";

import { useBoard } from "./use-board";

const board = {
  id: "es3c28p-01",
  name: "Desk Dom",
  board: "ES3C28P",
  mac: "B8:1F:3F:C3:97:54",
  firmware: "0.5.0",
  online: true,
  connection: "cloud" as const,
  free_heap: 120000,
  storage_used: 100,
  storage_total: 1000,
};

describe("useBoard", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("fetches status through the same-origin cloud proxy", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => board });
    vi.stubGlobal("fetch", fetchMock);

    const { result, unmount } = renderHook(() => useBoard());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/board",
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(result.current.board?.connection).toBe("cloud");
    expect(result.current.deviceList[0].firmware).toBe("0.5.0");
    expect(result.current.logs).toHaveLength(0);
    expect(result.current.telemetry[0].heap).toBe(120000);
    unmount();
  });

  it("exposes connection failures without stale board data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("board offline")));
    const { result, unmount } = renderHook(() => useBoard());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("board offline");
    expect(result.current.board).toBeNull();
    unmount();
  });
});

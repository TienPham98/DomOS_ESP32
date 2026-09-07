import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { fetchBoardStatus, updateBoardSettings } from "@/lib/board-api";

import SettingsPage from "./page";

vi.mock("@/lib/board-api", () => ({
  fetchBoardStatus: vi.fn(),
  updateBoardSettings: vi.fn(),
}));

const connectedBoard = {
  id: "board-1",
  name: "Dom",
  board: "ES3C28P",
  mac: "board-1",
  firmware: "0.3.5",
  online: true,
  connection: "cloud" as const,
  volume: 80,
  brightness: 75,
};

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.mocked(fetchBoardStatus).mockResolvedValue(connectedBoard);
    vi.mocked(updateBoardSettings).mockResolvedValue();
  });

  it("does not expose network configuration on the dashboard", async () => {
    render(<SettingsPage />);
    await screen.findByText("Board connected");

    expect(screen.queryByRole("heading", { name: "Network" })).not.toBeInTheDocument();
    expect(screen.queryByText("Wi-Fi SSID")).not.toBeInTheDocument();
    expect(screen.queryByText("MQTT Broker URI")).not.toBeInTheDocument();
    expect(screen.queryByText("API Base URL")).not.toBeInTheDocument();
    expect(screen.queryByText("mDNS Hostname")).not.toBeInTheDocument();
  });

  it("keeps the remaining settings sections available", async () => {
    render(<SettingsPage />);
    await screen.findByText("Board connected");

    expect(screen.getByText("Display")).toBeInTheDocument();
    expect(screen.getByText("Storage")).toBeInTheDocument();
    expect(screen.getByText("About")).toBeInTheDocument();
  });

  it("loads and saves volume and brightness through the cloud proxy", async () => {
    render(<SettingsPage />);
    await screen.findByText("Board connected");

    fireEvent.change(screen.getByRole("slider", { name: "Volume" }), {
      target: { value: "65" },
    });
    fireEvent.change(screen.getByRole("slider", { name: "Brightness" }), {
      target: { value: "40" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));

    await waitFor(() =>
      expect(updateBoardSettings).toHaveBeenCalledWith({ volume: 65, brightness: 40 }),
    );
    expect(await screen.findByText("Volume and brightness updated successfully!")).toBeInTheDocument();
  });
});

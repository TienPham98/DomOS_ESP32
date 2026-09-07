import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { updateClockAppearance } from "@/lib/board-api";

import ThemesPage from "./page";

vi.mock("@/lib/board-api", () => ({ updateClockAppearance: vi.fn() }));

describe("ThemesPage", () => {
  beforeEach(() => vi.mocked(updateClockAppearance).mockResolvedValue());

  it("applies clock appearance through the cloud board API", async () => {
    render(<ThemesPage />);
    fireEvent.click(screen.getByRole("button", { name: "Apply to Device" }));

    await waitFor(() => expect(updateClockAppearance).toHaveBeenCalledWith({
      style: "digital",
      color: "#06b6d4",
      mode: "dark",
    }));
    expect(await screen.findByText(/Synced through cloud/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Device IP / Host")).not.toBeInTheDocument();
  });
});

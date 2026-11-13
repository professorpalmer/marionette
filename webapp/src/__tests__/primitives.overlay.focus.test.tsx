import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { Menu } from "@base-ui/react/menu";
import { describe, expect, it, vi } from "vitest";
import PilotPicker from "../components/PilotPicker";
import { MenuPanel, MenuRoot, pointAnchor } from "../components/ui/overlay";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: { ...actual.api, swapPilot: vi.fn().mockResolvedValue({}), modelCatalog: vi.fn().mockResolvedValue({ all: [] }) },
  };
});

function PointMenu() {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  return <>
    <button type="button" onContextMenu={(event) => { event.preventDefault(); setAt({ x: 40, y: 40 }); }}>Row</button>
    <MenuRoot open={at !== null} onOpenChange={(open) => { if (!open) setAt(null); }}>
      {at && (
        <MenuPanel anchor={pointAnchor(at.x, at.y)} label="Row actions">
          <Menu.Item>First</Menu.Item>
          <Menu.Item>Second</Menu.Item>
        </MenuPanel>
      )}
    </MenuRoot>
  </>;
}

describe("overlay module focus rules", () => {
  it("closes a point-anchored menu on Tab and returns focus to the row", async () => {
    render(<PointMenu />);
    const row = screen.getByRole("button", { name: "Row" });
    row.focus();
    fireEvent.contextMenu(row);
    const menu = await screen.findByRole("menu", { name: "Row actions" });
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement!, { key: "Tab" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(row).toHaveFocus());
  });

  it("closes the pilot model picker on Escape typed in its filter", async () => {
    render(<PilotPicker config={{
      driver: "anthropic:claude-sonnet-4-6",
      reach: "cloud",
      budget: 1,
      models: ["anthropic:claude-sonnet-4-6", "openrouter:deepseek/deepseek-v4.1-flash"],
      reasoning_effort: "low",
    }} />);
    const trigger = screen.getByRole("button", { name: "claude-sonnet-4-6" });
    trigger.focus();
    fireEvent.click(trigger);
    const filter = await screen.findByRole("combobox", { name: "Search models or providers" });
    await waitFor(() => expect(filter).toHaveFocus());
    fireEvent.keyDown(filter, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Pilot model picker" })).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

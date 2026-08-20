import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import WindowGlassSettings from "../components/WindowGlassSettings";
import { TRANSLUCENCY_STORAGE_KEY } from "../lib/windowGlass";

afterEach(() => {
  localStorage.removeItem(TRANSLUCENCY_STORAGE_KEY);
  document.documentElement.removeAttribute("data-marionette-glass");
  delete (window as { harnessIPC?: unknown }).harnessIPC;
});

describe("WindowGlassSettings", () => {
  it("hides the row when the desktop bridge is missing", async () => {
    const { container } = render(<WindowGlassSettings />);
    await waitFor(() => {
      expect(container.querySelector("[data-testid='window-glass-settings']")).toBeNull();
    });
  });

  it("explains when Windows is below the glass floor", async () => {
    (window as { harnessIPC?: unknown }).harnessIPC = {
      translucency: {
        get: async () => ({
          state: { intensity: 0, fade: 0, mode: "clear", material: "popover" },
          capabilities: {
            translucencySupported: true,
            glassSupported: false,
            isWindows: true,
            windowsBuild: 19045,
            materials: ["under-window", "popover", "titlebar"],
          },
        }),
        capabilities: async () => ({
          translucencySupported: true,
          glassSupported: false,
          isWindows: true,
          windowsBuild: 19045,
          materials: ["under-window", "popover", "titlebar"],
        }),
      },
    };
    render(<WindowGlassSettings />);
    expect(await screen.findByTestId("window-glass-settings")).toBeInTheDocument();
    expect(screen.getByText(/Windows 11 22H2/i)).toBeInTheDocument();
    expect(screen.getByText(/build 19045/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /glass window/i })).toBeNull();
  });

  it("toggles glass on and paints the root attribute", async () => {
    const setCalls: unknown[] = [];
    (window as { harnessIPC?: unknown }).harnessIPC = {
      translucency: {
        get: async () => ({
          state: { intensity: 0, fade: 0, mode: "glass", material: "under-window" },
          capabilities: {
            translucencySupported: true,
            glassSupported: true,
            isWindows: false,
            materials: ["under-window", "popover", "titlebar", "header"],
          },
        }),
        set: async (state: unknown) => {
          setCalls.push(state);
          return state;
        },
        capabilities: async () => ({
          translucencySupported: true,
          glassSupported: true,
          isWindows: false,
          materials: ["under-window", "popover", "titlebar", "header"],
        }),
      },
    };
    render(<WindowGlassSettings />);
    const toggle = await screen.findByRole("button", { name: /glass window/i });
    await waitFor(() => {
      expect(toggle).toHaveTextContent(/off/i);
    });
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(setCalls.length).toBeGreaterThan(0);
      expect(document.documentElement.hasAttribute("data-marionette-glass")).toBe(true);
    });
  });
});

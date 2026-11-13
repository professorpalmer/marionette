import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import UpdateBanner from "../components/UpdateBanner";
import {
  parseUpdateAvailability,
  updatePillLabel,
  updatePillTitle,
} from "../lib/updateAvailability";

describe("parseUpdateAvailability", () => {
  it("is null when nothing is available", () => {
    expect(parseUpdateAvailability({ available: false, downloaded: false })).toBeNull();
    expect(parseUpdateAvailability(null)).toBeNull();
    expect(parseUpdateAvailability("yes")).toBeNull();
  });

  it("calls a source run behind its branch at the same version 'commits'", () => {
    // The v0.9.663 case: one commit behind main, the tip still says 0.9.663.
    const update = parseUpdateAvailability({
      available: true, downloaded: false, behind: 1, branch: "main", current: "0.9.663", latest: "0.9.663",
    });
    expect(update).toEqual({ kind: "commits", behind: 1, branch: "main", version: "0.9.663" });
    expect(updatePillLabel(update!)).toBe("1 new commit");
    expect(updatePillTitle(update!)).toBe("1 commit behind main. Version v0.9.663 does not change. Click to pull and relaunch.");
  });

  it("ignores a leading v when it compares versions", () => {
    const update = parseUpdateAvailability({ available: true, behind: 3, current: "v0.9.663", latest: "0.9.663" });
    expect(update?.kind).toBe("commits");
    expect(updatePillLabel(update!)).toBe("3 new commits");
  });

  it("calls a different version at the tip a 'release'", () => {
    const update = parseUpdateAvailability({ available: true, behind: 4, branch: "main", current: "0.9.663", latest: "0.9.664" });
    expect(update).toEqual({ kind: "release", version: "0.9.664", behind: 4, branch: "main" });
    expect(updatePillLabel(update!)).toBe("v0.9.664 ready");
  });

  it("calls a packaged installer a 'release' even without a version string", () => {
    expect(parseUpdateAvailability({ available: true, downloaded: true, behind: 0 })?.kind).toBe("release");
    expect(parseUpdateAvailability({ available: true, behind: 2, packagedAvailable: true, current: "0.9.663", latest: "0.9.663" })?.kind).toBe("release");
    expect(updatePillLabel({ kind: "release", version: "", behind: 0, branch: "main" })).toBe("update ready");
  });
});

describe("UpdateBanner with new commits only", () => {
  afterEach(() => {
    delete (window as any).harnessIPC;
    vi.restoreAllMocks();
  });

  it("publishes the commits to the pill and renders no banner", async () => {
    (window as any).harnessIPC = {
      updates: {
        check: vi.fn().mockResolvedValue({
          available: true, downloaded: false, behind: 1, branch: "main", current: "0.9.663", latest: "0.9.663",
        }),
        onAvailable: vi.fn(() => () => {}),
        onProgress: vi.fn(() => () => {}),
      },
    };
    const onAvailabilityChange = vi.fn();
    render(<UpdateBanner onAvailabilityChange={onAvailabilityChange} />);
    await vi.waitFor(() => expect(onAvailabilityChange).toHaveBeenCalledWith({
      kind: "commits", behind: 1, branch: "main", version: "0.9.663",
    }));
    expect(screen.queryByTestId("update-banner")).toBeNull();
    expect(screen.queryByText(/is ready/)).toBeNull();
  });
});

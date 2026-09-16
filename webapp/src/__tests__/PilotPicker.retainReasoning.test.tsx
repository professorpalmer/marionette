import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PilotPicker from "../components/PilotPicker";
import { getSessionCache, updateSessionCache, type SessionCache } from "../lib/sessionCache";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      swapPilot: vi.fn().mockResolvedValue({}),
      setPilotPreferences: vi.fn().mockResolvedValue({}),
    },
  };
});

vi.mock("../lib/sessionCache", () => ({
  getSessionCache: vi.fn(),
  updateSessionCache: vi.fn(),
}));

const getCache = vi.mocked(getSessionCache);
const updateCache = vi.mocked(updateSessionCache);

function cacheOf(sessionId: string, retain = false): SessionCache {
  return {
    session_id: sessionId,
    state: "unsupported",
    enabled: false,
    retain_reasoning: retain,
    reason: "This provider has no bounded cache refresh capability.",
    refreshes: 0,
    max_refreshes: 3,
    idle_seconds: 3600,
    max_spend_usd: 5,
    reserved_usd: 0,
  };
}

const sonnetConfig = {
  driver: "anthropic:claude-sonnet-4-6",
  reach: "cloud",
  budget: 1,
  models: ["anthropic:claude-sonnet-4-6"],
  reasoning_effort: "low" as const,
};

describe("PilotPicker thinking retention", () => {
  beforeEach(() => {
    getCache.mockReset();
    updateCache.mockReset();
  });

  it("keeps thinking retention out of the picker when there is no session", async () => {
    render(<PilotPicker config={sonnetConfig} />);
    fireEvent.click(screen.getByTitle("Reasoning effort (Low)"));
    expect(screen.queryByLabelText("Keep thinking blocks")).toBeNull();
    expect(getCache).not.toHaveBeenCalled();
  });

  it("hides thinking retention when the cache preference cannot load", async () => {
    getCache.mockRejectedValueOnce(new Error("unavailable"));
    render(<PilotPicker sessionId="s1" config={sonnetConfig} />);
    fireEvent.click(screen.getByTitle("Reasoning effort (Low)"));
    await waitFor(() => expect(getCache).toHaveBeenCalledWith("s1"));
    expect(screen.queryByLabelText("Keep thinking blocks")).toBeNull();
  });

  it("toggles thinking retention from the reasoning menu", async () => {
    getCache.mockResolvedValueOnce(cacheOf("s1", false));
    updateCache.mockResolvedValueOnce(cacheOf("s1", true));
    render(<PilotPicker sessionId="s1" config={sonnetConfig} />);
    fireEvent.click(screen.getByTitle("Reasoning effort (Low)"));
    const box = await screen.findByLabelText("Keep thinking blocks");
    expect(box).not.toBeChecked();
    fireEvent.click(box);
    await waitFor(() => {
      expect(updateCache).toHaveBeenCalledWith("s1", { action: "preferences", retain_reasoning: true });
    });
  });

  it("hides thinking retention when reasoning effort is none", async () => {
    getCache.mockResolvedValue(cacheOf("s1", true));
    render(<PilotPicker sessionId="s1" config={{ ...sonnetConfig, reasoning_effort: "none" }} />);
    fireEvent.click(screen.getByTitle("Reasoning effort (None)"));
    expect(screen.queryByLabelText("Keep thinking blocks")).toBeNull();
    expect(getCache).not.toHaveBeenCalled();
  });
});

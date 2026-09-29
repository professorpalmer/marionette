import { describe, expect, it } from "vitest";
import type { Config } from "../lib/api";
import { configForActiveSession, knownSessionPilot, pickerConfig, rememberSessionPilot, seedSessionPilots, sessionPilotFields } from "../lib/sessionConfig";

const cfg = (sessionId: string): Config => ({
  session_id: sessionId,
  driver: "openrouter:moonshotai/kimi-k3",
  reach: "cloud",
  budget: 1,
  models: ["openrouter:moonshotai/kimi-k3", "anthropic:claude-opus-4-8"],
  repo: "/Users/cary/Projects/demo",
  workers_ready: true,
  pilot_ready: true,
});

describe("configForActiveSession", () => {
  it("keeps composer models and repo while the fetched session_id lags the click", () => {
    const received = cfg("sess-running");
    const visible = configForActiveSession(received, "sess-home-new");
    expect(visible).not.toBeNull();
    expect(visible?.models).toEqual(received.models);
    expect(visible?.repo).toBe(received.repo);
    expect(visible?.workers_ready).toBe(true);
  });

  it("clears session-owned fields instead of relabeling the previous session's driver", () => {
    const received = { ...cfg("sess-running"), reasoning_effort: "high" as const, swarm_reasoning_effort: "max" as const };
    const visible = configForActiveSession(received, "sess-home-new");
    expect(visible?.driver).toBe("");
    expect(visible?.reasoning_effort).toBeUndefined();
    expect(visible?.swarm_reasoning_effort).toBeUndefined();
    expect(visible?.session_id).not.toBe("sess-home-new");
    expect(visible?.session_id).not.toBe("sess-running");
  });

  it("gives pickers a loading config while pending and refuses a foreign driver", () => {
    const pending = configForActiveSession(cfg("sess-running"), "sess-home-new");
    expect(pickerConfig(pending, "sess-home-new")?.driver).toBe("");
    expect(pickerConfig(cfg("sess-running"), "sess-home-new")).toBeNull();
    const own = cfg("sess-home-new");
    expect(pickerConfig(own, "sess-home-new")).toBe(own);
    const midSwitch = { ...cfg("sess-home-new"), driver: "" };
    expect(pickerConfig(midSwitch, "sess-home-new")).toBe(midSwitch);
    expect(pickerConfig(null, "sess-home-new")).toBeNull();
  });

  it("shows the returning session's own last model instead of blanking", () => {
    const own = { ...cfg("sess-b"), driver: "anthropic:claude-opus-4-8", reasoning_effort: "high" as const };
    const known = sessionPilotFields(own);
    const visible = configForActiveSession(cfg("sess-a"), "sess-b", known);
    expect(visible?.driver).toBe("anthropic:claude-opus-4-8");
    expect(visible?.reasoning_effort).toBe("high");
    expect(visible?.session_id).toBe("sess-b");
    expect(pickerConfig(visible, "sess-b")?.driver).toBe("anthropic:claude-opus-4-8");
    expect(sessionPilotFields({ ...own, driver: "" })).toBeNull();
    expect(sessionPilotFields({ ...own, session_id: null })).toBeNull();
  });

  it("uses a matching payload as-is", () => {
    const received = cfg("sess-home-new");
    expect(configForActiveSession(received, "sess-home-new")).toBe(received);
  });

  it("stays blank only when nothing has loaded yet", () => {
    expect(configForActiveSession(null, "sess-home-new")).toBeNull();
  });
});

describe("known session pilots", () => {
  it("seeds a never-opened session from the session list", () => {
    seedSessionPilots([{ id: "seed-a", pilot_preferences: { driver: "anthropic:claude-opus-4-8", reasoning_effort: "high" } }]);
    const visible = configForActiveSession(cfg("sess-other"), "seed-a", knownSessionPilot("seed-a"));
    expect(visible?.driver).toBe("anthropic:claude-opus-4-8");
    expect(visible?.reasoning_effort).toBe("high");
  });

  it("never lets a list row overwrite a fresher config answer", () => {
    rememberSessionPilot("seed-b", { driver: "openrouter:moonshotai/kimi-k3" });
    seedSessionPilots([{ id: "seed-b", pilot_preferences: { driver: "anthropic:claude-opus-4-8" } }]);
    expect(knownSessionPilot("seed-b")?.driver).toBe("openrouter:moonshotai/kimi-k3");
  });

  it("ignores rows without a stored driver", () => {
    seedSessionPilots([{ id: "seed-c" }, { id: "seed-d", pilot_preferences: {} }]);
    expect(knownSessionPilot("seed-c")).toBeNull();
    expect(knownSessionPilot("seed-d")).toBeNull();
  });
});

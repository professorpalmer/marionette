import { describe, expect, it } from "vitest";
import type { Config } from "../lib/api";
import { configForActiveSession } from "../lib/sessionConfig";

const cfg = (sessionId: string): Config => ({
  session_id: sessionId,
  driver: "openrouter:moonshotai/kimi-k3",
  reach: "cloud",
  budget: 1,
  models: ["openrouter:moonshotai/kimi-k3", "anthropic:claude-opus-4-8"],
  repo: "/Users/cary/Projects/pentest-playbook-kit",
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
    expect(visible?.session_id).toBe("sess-home-new");
  });

  it("uses a matching payload as-is", () => {
    const received = cfg("sess-home-new");
    expect(configForActiveSession(received, "sess-home-new")).toBe(received);
  });

  it("stays blank only when nothing has loaded yet", () => {
    expect(configForActiveSession(null, "sess-home-new")).toBeNull();
  });
});

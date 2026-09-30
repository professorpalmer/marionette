import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import SwarmReasoningPicker from "../components/SwarmReasoningPicker";
import type { Config } from "../lib/api";

vi.mock("../lib/api", () => ({ api: { setPilotPreferences: vi.fn() } }));

// renderToString runs no effects, so this is exactly the first paint after a
// session-switch remount: it must show the configured level, not "Medium".
describe("SwarmReasoningPicker first paint", () => {
  it("paints the configured worker reasoning level", () => {
    const html = renderToString(<SwarmReasoningPicker config={{ swarm_reasoning_effort: "high" } as Config} sessionId="s" />);
    expect(html).toContain("High");
    expect(html).not.toContain("Medium");
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import EmptySessionStarters, { STARTER_PROMPTS, recentSessionRows } from "../components/conversation/EmptySessionStarters";
import TranscriptEmptyState, { emptySessionContext } from "../components/conversation/TranscriptEmptyState";
import { api, type Session } from "../lib/api";
import { COMPOSER_PREFILL_EVENT, OPEN_ONBOARDING_EVENT, SWITCH_SESSION_EVENT, sessionAge, workspaceName } from "../lib/sessionNav";

function session(id: string, title: string, created: number, extra: Partial<Session> = {}): Session {
  return { id, title, created, ...extra };
}

afterEach(() => vi.restoreAllMocks());

describe("empty session context", () => {
  it("needs a model only when no key is ready and the pilot is not local", () => {
    expect(emptySessionContext({ repo: "/w/proj", driver: "qwen3-coder-30b", pilot_ready: false })).toEqual({ workspace: "proj", needsModel: true });
    expect(emptySessionContext({ repo: "/w/proj", driver: "local:qwen", pilot_ready: false }).needsModel).toBe(false);
    expect(emptySessionContext({ repo: "/w/proj", driver: "openrouter:x", pilot_ready: true }).needsModel).toBe(false);
    expect(emptySessionContext(null).needsModel).toBe(false);
  });

  it("calls the durable Home workspace Home, not its folder name", () => {
    expect(workspaceName("/Users/a/.pmharness/home")).toBe("Home");
    expect(emptySessionContext({ repo: "/Users/a/.pmharness/home", driver: "x", pilot_ready: true }).workspace).toBe("");
    expect(workspaceName("C:\\code\\api\\")).toBe("api");
  });

  it("shows Connect a model instead of the heading when no model can run", () => {
    const opened = vi.fn();
    window.addEventListener(OPEN_ONBOARDING_EVENT, opened);
    render(<TranscriptEmptyState transcriptStale={false} itemCount={0} context={{ workspace: "proj", needsModel: true }} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect a model" }));
    expect(opened).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/What should we do/)).toBeNull();
    window.removeEventListener(OPEN_ONBOARDING_EVENT, opened);
  });
});

describe("EmptySessionStarters", () => {
  it("fills the composer and never sends", async () => {
    vi.spyOn(api, "sessionsBank").mockResolvedValue([]);
    const prefill = vi.fn();
    window.addEventListener(COMPOSER_PREFILL_EVENT, prefill);
    render(<EmptySessionStarters sessionId="s0" showStarters />);
    fireEvent.click(screen.getByRole("button", { name: STARTER_PROMPTS[1].label }));
    expect((prefill.mock.calls[0][0] as CustomEvent).detail).toEqual({ text: STARTER_PROMPTS[1].prompt });
    window.removeEventListener(COMPOSER_PREFILL_EVENT, prefill);
  });

  it("lists recent sessions and asks the rail to switch", async () => {
    const now = Date.now() / 1000;
    vi.spyOn(api, "sessionsBank").mockResolvedValue([
      session("s0", "This one", now),
      session("s1", "Fix flaky test", now - 120, { workspace_root: "/w/api" }),
      session("s2", "New session", now - 60),
      session("s3", "Old work", now - 3 * 86400, { archived: true }),
    ]);
    const switched = vi.fn();
    window.addEventListener(SWITCH_SESSION_EVENT, switched);
    render(<EmptySessionStarters sessionId="s0" showStarters={false} />);
    const row = await screen.findByRole("button", { name: /Fix flaky test/ });
    expect(row.textContent).toContain("api");
    expect(row.textContent).toContain("2 min ago");
    expect(screen.queryByRole("button", { name: STARTER_PROMPTS[0].label })).toBeNull();
    fireEvent.click(row);
    expect((switched.mock.calls[0][0] as CustomEvent).detail).toEqual({ sessionId: "s1" });
    window.removeEventListener(SWITCH_SESSION_EVENT, switched);
  });

  it("keeps at most four reopenable rows, newest first", () => {
    const rows = Array.from({ length: 7 }, (_, i) => session(`s${i}`, `Task ${i}`, i));
    expect(recentSessionRows(rows, "s6").map((r) => r.id)).toEqual(["s5", "s4", "s3", "s2"]);
  });

  it("shows no recent block when the bank fails", async () => {
    vi.spyOn(api, "sessionsBank").mockRejectedValue(new Error("offline"));
    render(<EmptySessionStarters showStarters />);
    await waitFor(() => expect(api.sessionsBank).toHaveBeenCalled());
    expect(screen.queryByText("Recent sessions")).toBeNull();
  });
});

describe("sessionAge", () => {
  it("reads like a sidebar", () => {
    const now = 1_000_000_000_000;
    expect(sessionAge(now / 1000 - 10, now)).toBe("Just now");
    expect(sessionAge(now / 1000 - 3 * 3600, now)).toBe("3 h ago");
    expect(sessionAge(now / 1000 - 86400, now)).toBe("Yesterday");
  });
});

import { describe, expect, it, vi } from "vitest";
import {
  displaySessionListTitle,
  refreshSessionRowsAfterFirstSend,
  SESSIONS_CHANGED_EVENT,
} from "../lib/sessionTitle";

describe("displaySessionListTitle", () => {
  it("shows a title the user typed even when it reads like activity", () => {
    expect(displaySessionListTitle("Planning Q4 launch", true)).toBe("Planning Q4 launch");
    expect(displaySessionListTitle("Planning Q4 launch")).toBe("Untitled");
  });

  it("hides activity headlines and Stopped. from the session list", () => {
    expect(displaySessionListTitle("Explored 1 search")).toBe("Untitled");
    expect(displaySessionListTitle("Stopped.")).toBe("Untitled");
    expect(displaySessionListTitle("Fix the bug")).toBe("Fix the bug");
  });
});

describe("refreshSessionRowsAfterFirstSend", () => {
  it("asks the sidebar to refetch rows after the server names the session", () => {
    vi.useFakeTimers();
    const seen: string[] = [];
    const on = (e: Event) => seen.push(e.type);
    window.addEventListener(SESSIONS_CHANGED_EVENT, on);
    refreshSessionRowsAfterFirstSend(1500);
    vi.advanceTimersByTime(1499);
    expect(seen).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(seen).toEqual([SESSIONS_CHANGED_EVENT]);
    window.removeEventListener(SESSIONS_CHANGED_EVENT, on);
    vi.useRealTimers();
  });
});

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ProviderKeyBanner from "../components/ProviderKeyBanner";
import ConversationHeader, { projectRepeatsTitle } from "../components/conversation/ConversationHeader";
import {
  MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX,
  TITLEBAR_CHROME_PAD_X_PX,
  TITLEBAR_TRAFFIC_PAD_PX,
  TITLEBAR_TRAFFIC_PAD_SM_PX,
  titlebarLeftPad,
} from "../lib/titlebarSafe";

function paddingLeftPx(el: Element): number {
  const style = (el as HTMLElement).style.paddingLeft;
  return parseFloat(style || getComputedStyle(el).paddingLeft || "0");
}

function paddingRightPx(el: Element): number {
  const style = (el as HTMLElement).style.paddingRight;
  return parseFloat(style || getComputedStyle(el).paddingRight || "0");
}

// The smallest root font-size the responsive `html { font-size: clamp(...) }`
// can reach; rem padding written at this size is what used to slide content
// back under the traffic lights.
const SMALLEST_ROOT_FONT_SIZE = "11.5px";

describe("titlebar-safe clearance", () => {
  it("ProviderKeyBanner keeps fixed px traffic pad when root font shrinks", () => {
    document.documentElement.style.fontSize = "16px";
    render(<ProviderKeyBanner onAddKey={vi.fn()} />);
    const banner = screen.getByTestId("provider-key-banner");
    expect(banner.className).not.toMatch(/\bpl-24\b/);
    expect(paddingLeftPx(banner)).toBe(TITLEBAR_TRAFFIC_PAD_PX);

    document.documentElement.style.fontSize = SMALLEST_ROOT_FONT_SIZE;
    expect(paddingLeftPx(banner)).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    expect(paddingLeftPx(banner)).toBeGreaterThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
  });

  it("ConversationHeader shows the session title, project, and branch, not the turn state", () => {
    render(
      <ConversationHeader
        pillStatus="investigating"
        heading={{ title: "Fix the calc bug", project: "proj", branch: "master" }}
      />,
    );
    expect(screen.getByTestId("header-session-title").textContent).toBe("Fix the calc bug");
    expect(screen.getByTestId("header-project").textContent).toBe("proj");
    expect(screen.getByTestId("header-branch").textContent).toBe("master");
    expect(screen.queryByText(/Investigating|Still working|Done/)).toBeNull();
  });

  it("ConversationHeader owns the project: a click opens the folder menu", () => {
    render(
      <ConversationHeader
        pillStatus="done"
        heading={{ title: "Fix the calc bug", project: "proj", branch: "master" }}
      />,
    );
    const project = screen.getByTestId("header-project");
    expect(project).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(project);
    expect(project).toHaveAttribute("aria-expanded", "true");
    // jsdom has no width, so the narrow-header rule hides the chip from role queries.
    expect(screen.getByText(/Open Folder/).closest('[role="menuitem"]')).toBeInTheDocument();
    expect(screen.queryByText("Local")).toBeNull();
    expect(screen.getAllByText("master")).toHaveLength(1);
  });

  it("ConversationHeader does not repeat a project name that equals the title", () => {
    render(
      <ConversationHeader
        pillStatus="done"
        heading={{ title: "proj", project: "proj", branch: "master" }}
      />,
    );
    // The title says "proj" once; the folder menu trigger stays, as an icon.
    expect(screen.getAllByText("proj")).toHaveLength(1);
    const project = screen.getByTestId("header-project");
    expect(project.textContent).toBe("");
    expect(project).toHaveAttribute("aria-label", "Switch folder (proj)");
    fireEvent.click(project);
    expect(screen.getByText(/Open Folder/).closest('[role="menuitem"]')).toBeInTheDocument();
  });

  it("ConversationHeader truncates the title first, not the project or branch", () => {
    // flex-shrink spread the overflow over all three, so a short project name
    // ("proj2") lost 3px at 900-1024px while the title had room to give.
    render(
      <ConversationHeader
        pillStatus="done"
        heading={{ title: "Read calc.py and say in one sentence what it does", project: "proj2", branch: "master" }}
      />,
    );
    const title = screen.getByTestId("header-session-title");
    expect(title.className).toMatch(/\bmin-w-0\b/);
    expect(title.className).toMatch(/\btruncate\b/);
    for (const slot of [screen.getByTestId("header-project-slot"), screen.getByTestId("header-branch")]) {
      expect(slot.className).toMatch(/\bshrink-0\b/);
      expect(slot.className).toMatch(/\bmax-w-\[30%\]/);
    }
    // A long branch still truncates inside its cap.
    expect(screen.getByText("master").className).toMatch(/\btruncate\b/);
  });

  it("projectRepeatsTitle ignores case and spaces, and never matches an empty title", () => {
    expect(projectRepeatsTitle("proj", "proj")).toBe(true);
    expect(projectRepeatsTitle(" Proj ", "proj")).toBe(true);
    expect(projectRepeatsTitle("Fix the calc bug", "proj")).toBe(false);
    expect(projectRepeatsTitle("", "")).toBe(false);
  });

  it("ConversationHeader keeps the error pill", () => {
    render(<ConversationHeader pillStatus="error" detail="Provider key rejected" />);
    expect(screen.getByText("Error")).toBeInTheDocument();
  });

  it("ConversationHeader does not advertise session cache in the brand strip", () => {
    render(<ConversationHeader pillStatus="done" />);
    expect(screen.queryByText(/Cache:/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /keep warm/i })).toBeNull();
  });

  it("ConversationHeader clears the traffic lights at every root font-size", () => {
    document.documentElement.style.fontSize = "16px";
    render(<ConversationHeader pillStatus="idle" />);
    const header = screen.getByTestId("conversation-header");
    expect(header.className).not.toMatch(/\bpx-6\b/);
    // The header can be the topmost row (no banners, rail collapsed), so the
    // narrower chrome pad would leave the brand text under the traffic lights.
    expect(paddingLeftPx(header)).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    expect(paddingRightPx(header)).toBe(TITLEBAR_CHROME_PAD_X_PX);

    document.documentElement.style.fontSize = SMALLEST_ROOT_FONT_SIZE;
    expect(paddingLeftPx(header)).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    expect(paddingLeftPx(header)).toBeGreaterThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
  });

  it("the traffic-light pads are wider than the traffic lights themselves", () => {
    // Generous clearance past the lights (~50px / ~42px), not a tight skim.
    expect(TITLEBAR_TRAFFIC_PAD_PX).toBe(120);
    expect(TITLEBAR_TRAFFIC_PAD_SM_PX).toBe(112);
    expect(TITLEBAR_TRAFFIC_PAD_PX).toBeGreaterThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
    expect(TITLEBAR_TRAFFIC_PAD_SM_PX).toBeGreaterThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
    expect(TITLEBAR_CHROME_PAD_X_PX).toBeLessThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
  });
});

describe("titlebar traffic-light pad by strip position", () => {
  it("clears the traffic lights only where a strip actually meets them", () => {
    // Top-left of the window (left rail collapsed): full clearance.
    expect(titlebarLeftPad({ left: 0, top: 0 })).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    // Partly overlapping: content still starts past the lights.
    expect(60 + titlebarLeftPad({ left: 60, top: 0 })).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    // Beside an open left rail, or below the title bar: ordinary chrome pad.
    expect(titlebarLeftPad({ left: 190, top: 0 })).toBe(TITLEBAR_CHROME_PAD_X_PX);
    expect(titlebarLeftPad({ left: 0, top: 48 })).toBe(TITLEBAR_CHROME_PAD_X_PX);
  });

  it("ConversationHeader beside an open left rail has no traffic-light gap", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      { left: 190, top: 0, right: 900, bottom: 30, width: 710, height: 30, x: 190, y: 0, toJSON: () => ({}) } as DOMRect,
    );
    try {
      render(<ConversationHeader pillStatus="idle" />);
      expect(paddingLeftPx(screen.getByTestId("conversation-header"))).toBe(TITLEBAR_CHROME_PAD_X_PX);
    } finally {
      rect.mockRestore();
    }
  });
});
